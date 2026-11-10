package local.agentstorage;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.support.TransactionTemplate;

@Repository
public class DispatchRepository {
    private static final int ACTIVE_LIMIT = 20;
    private static final String INTERRUPTED = "执行已中断。为避免重复调用模型，未自动重试。";
    private final JdbcTemplate jdbc;
    private final JobRepository jobs;
    private final ObjectMapper mapper;
    private final TransactionTemplate transactions;

    public DispatchRepository(JdbcTemplate jdbc, JobRepository jobs, ObjectMapper mapper, TransactionTemplate transactions) {
        this.jdbc = jdbc;
        this.jobs = jobs;
        this.mapper = mapper;
        this.transactions = transactions;
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS dispatch_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES jobs(id),
                fingerprint TEXT NOT NULL,
                action_json TEXT,
                created_at TEXT NOT NULL,
                published_at TEXT,
                worker_id TEXT,
                claimed_ms INTEGER,
                finished_at TEXT
            )
            """);
        jdbc.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_task ON dispatch_events(task_id,finished_at)");
        jdbc.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_pending ON dispatch_events(published_at,finished_at)");
    }

    public Map<String,Object> enqueue(String id, String taskId, Map<String,Object> action, Map<String,Object> job, String updatedAt) {
        Values.id(id);
        Values.id(taskId);
        instantMillis(updatedAt);
        String actionType = Values.text(action.get("action"), 10, false);
        switch (actionType) {
            case "start" -> validateStart(action, taskId, job);
            case "review" -> validateReview(action, job);
            default -> throw new ApiException(400, "分发操作只能启动任务或提交人工复核。");
        }
        String actionJson = encode(action);
        Values.bytes(actionJson, 80 * 1024);
        String fingerprint = fingerprint(job == null ? actionJson : actionJson + "\n" + encode(job));

        synchronized (jobs) {
            return transactions.execute(transaction -> {
                var prior = find(id);
                if (prior != null) {
                    if (taskId.equals(prior.get("task_id")) && fingerprint.equals(prior.get("fingerprint"))) return jobs.get(taskId);
                    throw new ApiException(409, "分发编号已绑定到不同的任务或操作。");
                }
                int active = jdbc.queryForObject("SELECT count(*) FROM jobs WHERE status IN ('queued','running') AND purged_at IS NULL", Integer.class);
                if (active >= ACTIVE_LIMIT) throw new ApiException(409, "待运行任务较多，请稍后提交。");
                if (actionType.equals("start")) {
                    jobs.create(job);
                } else {
                    int changed = jdbc.update("""
                        UPDATE jobs SET status='queued',error=NULL,updated_at=?
                        WHERE id=? AND agent='release' AND review_id IS NOT NULL AND status='waiting_human' AND deleted_at IS NULL AND purged_at IS NULL
                        """, updatedAt, taskId);
                    if (changed == 0) throw new ApiException(409, "任务不再等待人工复核，请刷新报告。");
                    // The prior run has reached waiting_human. Its late finish acknowledgement must not
                    // mark the review's own running state interrupted.
                    jdbc.update("UPDATE dispatch_events SET finished_at=?,action_json=NULL WHERE task_id=? AND finished_at IS NULL", updatedAt, taskId);
                }
                jdbc.update("INSERT INTO dispatch_events(id,task_id,fingerprint,action_json,created_at) VALUES(?,?,?,?,?)",
                    id, taskId, fingerprint, actionJson, updatedAt);
                return jobs.get(taskId);
            });
        }
    }

    public List<Map<String,Object>> pending() {
        return jdbc.queryForList("""
            SELECT e.id,e.task_id FROM dispatch_events e JOIN jobs j ON j.id=e.task_id
            WHERE e.published_at IS NULL AND e.finished_at IS NULL AND j.deleted_at IS NULL AND j.purged_at IS NULL
              AND j.status IN ('queued','running')
            ORDER BY e.rowid LIMIT 100
            """);
    }

    public void published(String id) {
        Values.id(id);
        synchronized (jobs) {
            int changed = jdbc.update("UPDATE dispatch_events SET published_at=coalesce(published_at,?) WHERE id=?", OffsetDateTime.now().toString(), id);
            if (changed == 0) throw new ApiException(404, "分发事件不存在。");
        }
    }

    public Map<String,Object> claim(String id, String workerId, String updatedAt) {
        Values.id(id);
        Values.id(workerId);
        long claimedMillis = instantMillis(updatedAt);
        synchronized (jobs) {
            return transactions.execute(transaction -> {
                var event = find(id);
                if (event == null || event.get("worker_id") != null || event.get("finished_at") != null) return Map.of("claimed", false);
                String taskId = (String) event.get("task_id");
                int changed = jdbc.update("""
                    UPDATE jobs SET status='running',updated_at=?
                    WHERE id=? AND status='queued' AND deleted_at IS NULL AND purged_at IS NULL
                    """, updatedAt, taskId);
                if (changed == 0) return Map.of("claimed", false);
                Map<String,Object> action = decode((String) event.get("action_json"));
                jdbc.update("UPDATE dispatch_events SET worker_id=?,claimed_ms=? WHERE id=?", workerId, claimedMillis, id);
                return Map.of("claimed", true, "task_id", taskId, "action", action);
            });
        }
    }

    public void finish(String id, String workerId, String updatedAt) {
        Values.id(id);
        Values.id(workerId);
        instantMillis(updatedAt);
        synchronized (jobs) {
            transactions.executeWithoutResult(transaction -> {
                var event = find(id);
                if (event == null) throw new ApiException(404, "分发事件不存在。");
                if (!workerId.equals(event.get("worker_id"))) throw new ApiException(409, "仅领取该事件的 worker 可完成执行。");
                if (event.get("finished_at") == null) complete(id, (String)event.get("task_id"), updatedAt);
            });
        }
    }

    public int recover(String before, String updatedAt) {
        long threshold = instantMillis(before);
        instantMillis(updatedAt);
        synchronized (jobs) {
            return transactions.execute(transaction -> {
                var stale = jdbc.queryForList("SELECT id,task_id FROM dispatch_events WHERE finished_at IS NULL AND claimed_ms < ?", threshold);
                for (var event : stale) complete((String)event.get("id"), (String)event.get("task_id"), updatedAt);
                return stale.size();
            });
        }
    }

    private void complete(String id, String taskId, String updatedAt) {
        jdbc.update("UPDATE dispatch_events SET finished_at=?,action_json=NULL WHERE id=?", updatedAt, id);
        jdbc.update("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE id=? AND status='running' AND purged_at IS NULL",
            INTERRUPTED, updatedAt, taskId);
    }

    private Map<String,Object> find(String id) {
        var rows = jdbc.queryForList("SELECT * FROM dispatch_events WHERE id=?", id);
        return rows.isEmpty() ? null : rows.getFirst();
    }

    private static long instantMillis(String value) {
        try {
            return OffsetDateTime.parse(Values.text(value, 100, false)).toInstant().toEpochMilli();
        } catch (DateTimeParseException | ArithmeticException error) {
            throw new ApiException(400, "时间必须为包含偏移量的 ISO 日期时间。");
        }
    }

    private static void validateStart(Map<String,Object> action, String taskId, Map<String,Object> job) {
        Values.only(action, Set.of("action", "params"));
        if (job == null || !taskId.equals(job.get("id")) || !"queued".equals(job.get("status")) || job.get("deleted_at") != null
            || job.get("purged_at") != null || job.get("report_root") != null || job.get("report_path") != null) {
            throw new ApiException(400, "启动事件需要同编号、未删除的新排队任务。");
        }
        Map<String,Object> params = Values.object(action.get("params"));
        if ("ops".equals(job.get("agent"))) {
            Values.only(params, Set.of("service", "environment", "start", "end", "question", "workflow"));
            Values.id(params.get("service"));
            Values.id(params.get("environment"));
            long start = instantMillis(Values.text(params.get("start"), 80, false));
            long end = instantMillis(Values.text(params.get("end"), 80, false));
            if (start > end) throw new ApiException(400, "任务时间范围无效。");
            Values.text(params.get("question"), 32000, false);
            String workflow = Values.text(params.get("workflow"), 10, false);
            if (!Set.of("auto", "single", "swarm").contains(workflow)) throw new ApiException(400, "运维工作流无效。");
        } else if ("release".equals(job.get("agent"))) {
            Values.only(params, Set.of("change_id", "service", "environment"));
            Values.id(params.get("change_id"));
            Values.id(job.get("review_id"));
            if (params.containsKey("service")) Values.id(params.get("service"));
            if (params.containsKey("environment")) Values.id(params.get("environment"));
        } else {
            throw new ApiException(400, "Agent无效。");
        }
    }

    private static void validateReview(Map<String,Object> action, Map<String,Object> job) {
        Values.only(action, Set.of("action", "reviewer", "decision", "comment", "review_digest"));
        if (job != null) throw new ApiException(400, "人工复核使用已有任务，不能附带新任务。");
        Values.text(action.get("reviewer"), 160, false);
        Values.text(action.get("comment"), 4000, true);
        String decision = Values.text(action.get("decision"), 20, false);
        String digest = Values.text(action.get("review_digest"), 64, false);
        if (!Set.of("acknowledge", "request_changes", "reject").contains(decision) || !digest.matches("[a-f0-9]{64}")) {
            throw new ApiException(400, "复核决定或材料指纹无效。");
        }
    }

    private String encode(Map<String,Object> value) {
        try {
            return mapper.writer().with(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS).writeValueAsString(value);
        } catch (JsonProcessingException error) {
            throw new ApiException(400, "分发内容无法编码。");
        }
    }

    private Map<String,Object> decode(String value) {
        try {
            return mapper.readValue(value, new TypeReference<>() {});
        } catch (JsonProcessingException | IllegalArgumentException error) {
            throw new ApiException(503, "分发事件内容无法读取。");
        }
    }

    private static String fingerprint(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }
}
