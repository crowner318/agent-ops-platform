package local.agentstorage;

import java.io.IOException;
import java.net.ServerSocket;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.time.Duration;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceClientConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import redis.embedded.RedisServer;
import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/** Actual SQLite file + an actual Redis executable on an isolated port. No Redis mock. */
@SpringBootTest
@AutoConfigureMockMvc
class StorageIntegrationTest {
    static final String TOKEN = "local-test-storage-token-000000000000";
    static final Path DIR;
    static final int REDIS_PORT;
    static final RedisServer REDIS;
    static {
        try {
            DIR=Path.of("target","integration-" + UUID.randomUUID()).toAbsolutePath(); Files.createDirectories(DIR);
            try(var socket=new ServerSocket(0)) { REDIS_PORT=socket.getLocalPort(); }
            REDIS=RedisServer.newRedisServer().port(REDIS_PORT).setting("bind 127.0.0.1")
                .setting("appendonly no").setting("save \"\"").setting("dir \"" + DIR.toString().replace('\\','/') + "\"").build();
            REDIS.start();
        } catch(IOException e) { throw new ExceptionInInitializerError(e); }
    }
    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry properties) {
        properties.add("storage.sqlite-path",()->DIR.resolve("test.sqlite3").toString());
        properties.add("storage.service-token",()->TOKEN);
        properties.add("spring.data.redis.host",()->"127.0.0.1");
        properties.add("spring.data.redis.port",()->REDIS_PORT);
        properties.add("spring.data.redis.password",()->"");
    }
    @AfterAll static void closeRedis() throws IOException { REDIS.stop(); }
    @Autowired MockMvc mvc;
    @Autowired ObjectMapper mapper;
    @Autowired JobRepository jobs;
    @Autowired DispatchRepository dispatch;
    @Autowired CaseRepository cases;
    @Autowired ReportStore reports;
    @Autowired StringRedisTemplate redis;
    @Autowired JdbcTemplate jdbc;

    Map<String,Object> job(String id) {
        return new LinkedHashMap<>(Map.of("id",id,"agent","ops","title","本地Java集成测试", "mode","demo","status","completed","created_at","2026-11-09T10:00:00+08:00","updated_at","2026-11-09T10:00:00+08:00","params","{}","source_key",("%064x").formatted(id.hashCode() & 0xffffffffL)));
    }
    String json(Object value) throws Exception { return mapper.writeValueAsString(value); }

    @Test void tokenIsRequiredAndNeverEchoed() throws Exception {
        mvc.perform(get("/v1/jobs")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.error").exists());
        mvc.perform(get("/%76%31/jobs")).andExpect(status().isUnauthorized());
        mvc.perform(get("/v1/jobs").header("X-Storage-Token","wrong-secret")).andExpect(status().isUnauthorized()).andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("wrong-secret"))));
        mvc.perform(get("/v1/jobs").header("X-Storage-Token",TOKEN,TOKEN)).andExpect(status().isUnauthorized());
        mvc.perform(get("/health")).andExpect(status().isOk()).andExpect(jsonPath("$.sqlite.available").value(true)).andExpect(jsonPath("$.redis.available").value(true));
    }
    @Test void createsPatchesAndReadsActualSqlite() throws Exception {
        mvc.perform(post("/v1/jobs").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("job",job("crud")))))
            .andExpect(status().isCreated()).andExpect(jsonPath("$.job.id").value("crud"));
        mvc.perform(patch("/v1/jobs/crud").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("values",Map.of("status","blocked","updated_at","now")))))
            .andExpect(status().isOk()).andExpect(jsonPath("$.job.status").value("blocked"));
        assertEquals("blocked",jdbc.queryForObject("SELECT status FROM jobs WHERE id='crud'",String.class));
        assertTrue(Files.size(DIR.resolve("test.sqlite3")) > 0);
        mvc.perform(post("/v1/jobs").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("job",job("crud")))))
            .andExpect(status().isConflict());
    }
    @Test void reportsAreAtomicRedisPayloadWithoutExpiry() throws Exception {
        jobs.create(job("redis-report"));
        var payload=Map.of("json",Map.of("answer","中文报告","deployment_approved",false),"markdown","# 中文报告\n没有执行生产操作");
        mvc.perform(post("/v1/reports/redis-report").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(payload)))
            .andExpect(status().isOk()).andExpect(jsonPath("$.backend").value("redis"));
        mvc.perform(get("/v1/reports/redis-report").header("X-Storage-Token",TOKEN))
            .andExpect(status().isOk()).andExpect(jsonPath("$.json.answer").value("中文报告")).andExpect(jsonPath("$.markdown").value("# 中文报告\n没有执行生产操作"));
        assertEquals(-1L,redis.getExpire("agent:reports:v1:redis-report"));
        String actual=redis.opsForValue().get("agent:reports:v1:redis-report");
        assertTrue(actual.contains("markdown") && actual.contains("中文报告"));
        mvc.perform(delete("/v1/reports/redis-report").header("X-Storage-Token",TOKEN)).andExpect(status().isOk());
        assertNull(redis.opsForValue().get("agent:reports:v1:redis-report"));
        mvc.perform(get("/v1/reports/redis-report").header("X-Storage-Token",TOKEN)).andExpect(status().isNotFound());
    }
    @Test void purgeRequiresConfirmationAndTrashThenRemovesRedisAndSensitiveMetadata() throws Exception {
        jobs.create(job("purge")); reports.put("purge",Map.of("json",Map.of("private","must-remove"),"markdown","private text"));
        mvc.perform(post("/v1/jobs/purge/purge").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("confirmation","wrong")))).andExpect(status().isBadRequest());
        mvc.perform(post("/v1/jobs/purge/purge").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("confirmation","purge")))).andExpect(status().isConflict());
        jobs.patch("purge",Map.of("deleted_at","2026-11-09","logs","[\"private-log\"]","report_path","private-path"));
        mvc.perform(post("/v1/jobs/purge/purge").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("confirmation","purge"))))
            .andExpect(status().isOk()).andExpect(jsonPath("$.job.purged").value(true)).andExpect(jsonPath("$.job.params").value("{}"));
        assertNull(redis.opsForValue().get("agent:reports:v1:purge"));
        assertNull(jobs.get("purge").get("report_path")); assertEquals("[]",jobs.get("purge").get("logs"));
        assertNotNull(jobs.get("purge").get("source_key")); assertTrue(jobs.list(true).stream().anyMatch(row->row.get("id").equals("purge")));
        assertFalse(jobs.list(false).stream().anyMatch(row->row.get("id").equals("purge")));
        assertEquals(409,assertThrows(ApiException.class,()->jobs.patch("purge",Map.of("deleted_at","restore"))).status());
        mvc.perform(post("/v1/reports/purge").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("json",Map.of(),"markdown","resurrect"))))
            .andExpect(status().isConflict());
    }
    @Test void redisFailureCannotEraseJobAndRetryCanFinish() {
        jobs.create(job("failure")); jobs.patch("failure",Map.of("deleted_at","deleted","report_path","still-needed"));
        ReportStore unavailable=new ReportStore(redis,mapper) { @Override public void delete(String id) { throw new ApiException(503,"Redis unavailable"); } };
        assertEquals(503,assertThrows(ApiException.class,()->jobs.purge("failure",unavailable)).status());
        assertNull(jobs.get("failure").get("purged_at")); assertEquals("still-needed",jobs.get("failure").get("report_path"));
        jobs.purge("failure",reports); assertNotNull(jobs.get("failure").get("purged_at"));
    }
    @Test void caseDeleteIsRealAndTombstonePreventsResync() throws Exception {
        var item=Map.<String,Object>of("id","case-permanent","service","billing-api","environment","prod-eu","summary","必须移除的案例正文","verified",true);
        mvc.perform(post("/v1/cases").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("case",item)))).andExpect(status().isOk());
        mvc.perform(delete("/v1/cases/case-permanent").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("confirmation","case-permanent")))).andExpect(status().isOk());
        assertEquals(0,jdbc.queryForObject("SELECT count(*) FROM cases WHERE id='case-permanent'",Integer.class));
        assertTrue(cases.tombstones().contains("case-permanent"));
        assertEquals(409,assertThrows(ApiException.class,()->cases.save(item)).status());
        mvc.perform(get("/v1/case-tombstones").header("X-Storage-Token",TOKEN)).andExpect(status().isOk()).andExpect(jsonPath("$.ids").isArray());
    }
    @Test void runningJobsCannotBeTrashedOrPurgedAndRecoveryIsExplicit() {
        var input=job("active");input.put("status","running");jobs.create(input);
        assertEquals(409,assertThrows(ApiException.class,()->jobs.patch("active",Map.of("deleted_at","now"))).status());
        assertEquals(409,assertThrows(ApiException.class,()->jobs.purge("active",reports)).status());
        assertEquals(1,jobs.recover("service restarted","now"));assertEquals("interrupted",jobs.get("active").get("status"));
    }
    @Test void identifiersAndUnknownPatchFieldsCannotBecomeSql() throws Exception {
        assertThrows(ApiException.class,()->jobs.get("../private"));
        jobs.create(job("bad-fields"));
        mvc.perform(patch("/v1/jobs/bad-fields").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content("{\"values\":{\"purged_at\":\"bypass\"}}"))
            .andExpect(status().isBadRequest());
        mvc.perform(post("/v1/jobs").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content("{\"job\":{},\"job\":{}}"))
            .andExpect(status().isBadRequest());
        assertEquals("completed",jobs.get("bad-fields").get("status"));
    }
    @Test void migrationTombstoneCarriesNoDeletedContentAndPreventsRecreation() throws Exception {
        var tombstone=Map.of("id","old-purged","agent","ops","source_key","a".repeat(64),"purged_at","2026-11-09T10:00:00+08:00");
        mvc.perform(post("/v1/jobs/import-tombstone").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("job",tombstone))))
            .andExpect(status().isOk()).andExpect(jsonPath("$.job.purged").value(true));
        assertEquals("{}",jobs.get("old-purged").get("params"));
        assertEquals(409,assertThrows(ApiException.class,()->jobs.create(job("old-purged"))).status());
    }
    @Test void actualRedisConnectionFailureReturns503AndLeavesSqliteRecord() throws Exception {
        int emptyPort;
        try(var socket=new ServerSocket(0)) { emptyPort=socket.getLocalPort(); }
        var client=LettuceClientConfiguration.builder().commandTimeout(Duration.ofMillis(200)).shutdownTimeout(Duration.ZERO).build();
        var factory=new LettuceConnectionFactory(new RedisStandaloneConfiguration("127.0.0.1",emptyPort),client);
        factory.afterPropertiesSet(); factory.start();
        try {
            ReportStore disconnected=new ReportStore(new StringRedisTemplate(factory),mapper);
            assertFalse(disconnected.healthy());
            assertEquals(503,assertThrows(ApiException.class,()->disconnected.put("network-failure",Map.of("json",Map.of(),"markdown","text"))).status());
            assertEquals(503,assertThrows(ApiException.class,()->disconnected.get("network-failure")).status());
            jobs.create(job("network-failure")); jobs.patch("network-failure",Map.of("deleted_at","deleted","report_path","retain-path"));
            assertEquals(503,assertThrows(ApiException.class,()->jobs.purge("network-failure",disconnected)).status());
            assertNull(jobs.get("network-failure").get("purged_at"));
            assertEquals("retain-path",jobs.get("network-failure").get("report_path"));
        } finally { factory.destroy(); }
    }
    @Test void caseTombstoneMigrationIsAuthenticatedIdempotentAndRemovesPendingOldContent() throws Exception {
        var item=Map.<String,Object>of("id","legacy-deleted-case","service","billing-api","environment","test","summary","待清理的旧案例内容","verified",true);
        cases.save(item);
        mvc.perform(post("/v1/cases/import-tombstone").contentType(MediaType.APPLICATION_JSON).content(json(Map.of("id","legacy-deleted-case"))))
            .andExpect(status().isUnauthorized());
        assertEquals(1,jdbc.queryForObject("SELECT count(*) FROM cases WHERE id='legacy-deleted-case'",Integer.class));
        for (String id: java.util.List.of("legacy-deleted-case","legacy-deleted-case","deleted-in-empty-store")) {
            mvc.perform(post("/v1/cases/import-tombstone").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(Map.of("id",id))))
                .andExpect(status().isOk()).andExpect(jsonPath("$.imported").value(true));
        }
        assertEquals(0,jdbc.queryForObject("SELECT count(*) FROM cases WHERE id='legacy-deleted-case'",Integer.class));
        assertTrue(cases.tombstones().contains("deleted-in-empty-store"));
        assertEquals(409,assertThrows(ApiException.class,()->cases.save(item)).status());
    }

    private static final String DISPATCH_NOW = "2026-11-09T10:00:00+08:00";

    private Map<String,Object> queued(String id, String agent) {
        var row = job(id);
        row.put("status", "queued");
        row.put("agent", agent);
        if (agent.equals("release")) row.put("review_id", "review-" + id);
        return row;
    }

    private Map<String,Object> opsAction() {
        return Map.of("action", "start", "params", Map.of("service", "checkout", "environment", "demo",
            "start", "2026-11-09T09:30:00+08:00", "end", DISPATCH_NOW, "question", "为何请求超时？", "workflow", "single"));
    }

    @Test void dispatchHttpExposesOnlyOpaquePendingIdsAndAllowsClaimBeforePublish() throws Exception {
        var body = Map.of("id", "event-dispatch-http", "task_id", "dispatch-http", "action", opsAction(),
            "job", queued("dispatch-http", "ops"), "updated_at", DISPATCH_NOW);
        mvc.perform(post("/v1/dispatch").contentType(MediaType.APPLICATION_JSON).content(json(body))).andExpect(status().isUnauthorized());
        for (int attempt = 0; attempt < 2; attempt++) {
            mvc.perform(post("/v1/dispatch").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(body)))
                .andExpect(status().isOk()).andExpect(jsonPath("$.job.status").value("queued"));
        }
        assertTrue(dispatch.pending().contains(Map.of("id", "event-dispatch-http", "task_id", "dispatch-http")));
        var claim = Map.of("worker_id", "worker-http", "updated_at", DISPATCH_NOW);
        mvc.perform(post("/v1/dispatch/event-dispatch-http/claim").header("X-Storage-Token",TOKEN).contentType(MediaType.APPLICATION_JSON).content(json(claim)))
            .andExpect(status().isOk()).andExpect(jsonPath("$.claimed").value(true)).andExpect(jsonPath("$.action.params.question").value("为何请求超时？"));
        assertFalse((Boolean)dispatch.claim("event-dispatch-http", "worker-duplicate", DISPATCH_NOW).get("claimed"));
        dispatch.published("event-dispatch-http");
        assertEquals("running", jobs.get("dispatch-http").get("status"));
        assertEquals(409, assertThrows(ApiException.class, () -> dispatch.finish("event-dispatch-http", "worker-duplicate", DISPATCH_NOW)).status());
        jobs.patch("dispatch-http", Map.of("status", "completed"));
        dispatch.finish("event-dispatch-http", "worker-http", DISPATCH_NOW);
        dispatch.finish("event-dispatch-http", "worker-http", DISPATCH_NOW);
        dispatch.published("event-dispatch-http");
        assertEquals("completed", jobs.get("dispatch-http").get("status"));
        assertNull(jdbc.queryForObject("SELECT action_json FROM dispatch_events WHERE id='event-dispatch-http'", String.class));
    }

    @Test void concurrentClaimsReturnExecutionPayloadExactlyOnce() throws Exception {
        dispatch.enqueue("event-dispatch-race", "dispatch-race", opsAction(), queued("dispatch-race", "ops"), DISPATCH_NOW);
        var go = new CountDownLatch(1);
        var results = new java.util.ArrayList<CompletableFuture<Map<String,Object>>>();
        for (String worker : java.util.List.of("worker-race-a", "worker-race-b")) {
            results.add(CompletableFuture.supplyAsync(() -> {
                try { go.await(); }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new IllegalStateException(error); }
                return dispatch.claim("event-dispatch-race", worker, DISPATCH_NOW);
            }));
        }
        go.countDown();
        var first = results.getFirst().get(10, TimeUnit.SECONDS);
        var second = results.getLast().get(10, TimeUnit.SECONDS);
        assertNotEquals(first.get("claimed"), second.get("claimed"));
        assertEquals("running", jobs.get("dispatch-race").get("status"));
        String owner = Boolean.TRUE.equals(first.get("claimed")) ? "worker-race-a" : "worker-race-b";
        dispatch.finish("event-dispatch-race", owner, DISPATCH_NOW);
        assertEquals("interrupted", jobs.get("dispatch-race").get("status"));
    }

    @Test void aSecondReviewSubmissionAndAnOldFinishCannotRunTheReviewAgain() {
        String taskId = "dispatch-review";
        dispatch.enqueue("event-dispatch-release", taskId, Map.of("action", "start", "params", Map.of("change_id", "release-demo")),
            queued(taskId, "release"), DISPATCH_NOW);
        dispatch.claim("event-dispatch-release", "worker-start", DISPATCH_NOW);
        jobs.patch(taskId, Map.of("status", "waiting_human"));
        var review = Map.<String,Object>of("action", "review", "decision", "acknowledge", "reviewer", "演示人员", "comment", "", "review_digest", "b".repeat(64));
        dispatch.enqueue("event-dispatch-review", taskId, review, null, DISPATCH_NOW);
        assertEquals(409, assertThrows(ApiException.class, () -> dispatch.enqueue("event-dispatch-duplicate-review", taskId, review, null, DISPATCH_NOW)).status());
        assertTrue((Boolean) dispatch.claim("event-dispatch-review", "worker-review", DISPATCH_NOW).get("claimed"));
        dispatch.finish("event-dispatch-release", "worker-start", DISPATCH_NOW);
        assertEquals("running", jobs.get(taskId).get("status"));
        jobs.patch(taskId, Map.of("status", "review_acknowledged"));
        dispatch.finish("event-dispatch-review", "worker-review", DISPATCH_NOW);
        assertEquals("review_acknowledged", jobs.get(taskId).get("status"));
        assertNull(jdbc.queryForObject("SELECT action_json FROM dispatch_events WHERE id='event-dispatch-review'", String.class));
    }

    @Test void restartPreservesPendingWorkInterruptsStaleClaimAndPurgesOutboxData() {
        dispatch.enqueue("event-dispatch-stale", "dispatch-stale", opsAction(), queued("dispatch-stale", "ops"), DISPATCH_NOW);
        dispatch.enqueue("event-dispatch-pending", "dispatch-pending", opsAction(), queued("dispatch-pending", "ops"), DISPATCH_NOW);
        jobs.create(queued("dispatch-old-local", "ops"));
        dispatch.claim("event-dispatch-stale", "worker-stale", DISPATCH_NOW);
        assertEquals(1, jobs.recover("旧任务中断", DISPATCH_NOW));
        assertEquals("interrupted", jobs.get("dispatch-old-local").get("status"));
        assertEquals("queued", jobs.get("dispatch-pending").get("status"));
        assertEquals("running", jobs.get("dispatch-stale").get("status"));
        assertEquals(1, dispatch.recover("2026-11-09T02:00:01Z", "2026-11-09T02:02:00Z"));
        assertEquals("interrupted", jobs.get("dispatch-stale").get("status"));
        assertFalse((Boolean)dispatch.claim("event-dispatch-stale", "worker-new", DISPATCH_NOW).get("claimed"));
        assertTrue(dispatch.pending().contains(Map.of("id", "event-dispatch-pending", "task_id", "dispatch-pending")));
        jobs.patch("dispatch-stale", Map.of("deleted_at", DISPATCH_NOW));
        jobs.purge("dispatch-stale", reports);
        assertEquals("", jdbc.queryForObject("SELECT fingerprint FROM dispatch_events WHERE id='event-dispatch-stale'", String.class));
        assertNull(jdbc.queryForObject("SELECT action_json FROM dispatch_events WHERE id='event-dispatch-stale'", String.class));
        assertFalse((Boolean)dispatch.claim("event-dispatch-stale", "worker-new", DISPATCH_NOW).get("claimed"));
        dispatch.claim("event-dispatch-pending", "worker-pending", DISPATCH_NOW);
        dispatch.finish("event-dispatch-pending", "worker-pending", DISPATCH_NOW);
    }

    @Test void outboxWriteFailureRollsBackBothJobCreationAndReviewTransition() {
        var waiting = queued("dispatch-tx-review", "release");
        waiting.put("status", "waiting_human");
        jobs.create(waiting);
        jdbc.execute("""
            CREATE TRIGGER reject_dispatch_test BEFORE INSERT ON dispatch_events
            WHEN NEW.id IN ('event-tx-start','event-tx-review')
            BEGIN SELECT RAISE(ABORT, 'test injected outbox write failure'); END
            """);
        try {
            assertThrows(org.springframework.dao.DataAccessException.class,
                () -> dispatch.enqueue("event-tx-start", "dispatch-tx-start", opsAction(), queued("dispatch-tx-start", "ops"), DISPATCH_NOW));
            assertEquals(404, assertThrows(ApiException.class, () -> jobs.get("dispatch-tx-start")).status());
            var review = Map.<String,Object>of("action", "review", "decision", "acknowledge", "reviewer", "演示人员", "comment", "", "review_digest", "c".repeat(64));
            assertThrows(org.springframework.dao.DataAccessException.class,
                () -> dispatch.enqueue("event-tx-review", "dispatch-tx-review", review, null, DISPATCH_NOW));
            assertEquals("waiting_human", jobs.get("dispatch-tx-review").get("status"));
        } finally {
            jdbc.execute("DROP TRIGGER reject_dispatch_test");
        }
    }
}
