package local.agentstorage;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.support.TransactionTemplate;

@Repository
public class JobRepository {
    private static final List<String> COLUMNS = List.of("id","agent","title","mode","status","created_at","updated_at","params","logs","review_id","report_root","report_path","is_history","error","deleted_at","source_key","purged_at");
    private static final Set<String> PATCH = Set.of("status","logs","review_id","report_root","report_path","error","deleted_at","updated_at","source_key");
    private static final Set<String> STATES = Set.of("queued","running","waiting_human","completed","review_acknowledged","blocked","incomplete","failed","interrupted","rejected","changes_requested");
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final TransactionTemplate transactions;
    public JobRepository(JdbcTemplate jdbc, ObjectMapper mapper, TransactionTemplate transactions) {
        this.jdbc = jdbc; this.mapper = mapper; this.transactions = transactions;
        jdbc.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,agent TEXT NOT NULL,title TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,params TEXT NOT NULL,logs TEXT NOT NULL DEFAULT '[]',review_id TEXT,report_root TEXT,report_path TEXT,is_history INTEGER NOT NULL DEFAULT 0,error TEXT,deleted_at TEXT,source_key TEXT,purged_at TEXT)");
        var columns = jdbc.queryForList("PRAGMA table_info(jobs)").stream().map(row -> row.get("name")).toList();
        for (String column : List.of("deleted_at","source_key","purged_at")) if (!columns.contains(column)) jdbc.execute("ALTER TABLE jobs ADD COLUMN " + column + " TEXT");
        jdbc.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_key ON jobs(source_key) WHERE source_key IS NOT NULL");
        jdbc.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)");
    }
    public List<Map<String,Object>> list(boolean includeDeleted) {
        return jdbc.queryForList("SELECT * FROM jobs" + (includeDeleted ? "" : " WHERE deleted_at IS NULL AND purged_at IS NULL") + " ORDER BY created_at DESC LIMIT 10000").stream().map(this::publicRow).toList();
    }
    public Map<String,Object> get(String id) {
        Values.id(id);
        var rows = jdbc.queryForList("SELECT * FROM jobs WHERE id=?", id);
        if (rows.isEmpty()) throw new ApiException(404, "任务不存在。");
        return publicRow(rows.getFirst());
    }
    private Map<String,Object> publicRow(Map<String,Object> row) {
        var result = new LinkedHashMap<>(row);
        result.put("purged", row.get("purged_at") != null);
        return result;
    }
    public synchronized Map<String,Object> create(Map<String,Object> input) {
        Values.only(input, Set.copyOf(COLUMNS));
        var row = new LinkedHashMap<>(input);
        for (String required : List.of("id","agent","title","mode","status","created_at","updated_at","params")) if (!row.containsKey(required)) throw new ApiException(400, "任务必要字段缺失。");
        row.putIfAbsent("logs", "[]"); row.putIfAbsent("is_history", 0);
        if (row.get("purged_at") != null) throw new ApiException(400, "不能创建已永久删除的任务。");
        validate(row);
        List<String> keys = COLUMNS.stream().filter(row::containsKey).toList();
        try {
            jdbc.update("INSERT INTO jobs(" + String.join(",",keys) + ") VALUES(" + String.join(",",keys.stream().map(key -> "?").toList()) + ")", keys.stream().map(row::get).toArray());
        } catch (DataAccessException e) {
            Throwable cause=e;
            while (cause != null) {
                if (cause instanceof java.sql.SQLException sql && sql.getErrorCode() == 19) throw new ApiException(409, "任务编号或来源已存在。");
                cause=cause.getCause();
            }
            throw e;
        }
        return get((String)row.get("id"));
    }
    private void validate(Map<String,Object> row) {
        for (var entry : row.entrySet()) {
            String key = entry.getKey(); Object value = entry.getValue();
            if (Set.of("review_id","report_root","report_path","error","deleted_at","source_key","purged_at").contains(key) && value == null) continue;
            switch (key) {
                case "id", "review_id" -> Values.id(value);
                case "agent" -> { if (!Set.of("ops","release").contains(value)) throw new ApiException(400,"Agent无效。"); }
                case "mode" -> { if (!Set.of("demo","api").contains(value)) throw new ApiException(400,"模型模式无效。"); }
                case "status" -> { if (!(value instanceof String) || !STATES.contains(value)) throw new ApiException(400,"任务状态无效。"); }
                case "params" -> Values.jsonString(value,mapper,true,131072);
                case "logs" -> Values.jsonString(value,mapper,false,262144);
                case "is_history" -> { if (!(value instanceof Number n) || (n.intValue()!=0 && n.intValue()!=1) || n.doubleValue()!=n.intValue()) throw new ApiException(400,"历史标记无效。"); }
                case "source_key" -> { if (!(value instanceof String s) || !s.matches("[a-f0-9]{64}")) throw new ApiException(400,"来源摘要无效。"); }
                default -> Values.text(value, 8192, true);
            }
        }
    }
    public synchronized Map<String,Object> importTombstone(Map<String,Object> input) {
        Values.only(input,Set.of("id","agent","source_key","review_id","purged_at","created_at","updated_at"));
        String id=Values.id(input.get("id"));
        String purged=Values.text(input.get("purged_at"),100,false);
        validate(input);
        if (!input.containsKey("agent") || !input.containsKey("source_key")) throw new ApiException(400,"迁移墓碑必须包含Agent和来源摘要。");
        var existing=jdbc.queryForList("SELECT * FROM jobs WHERE id=? OR source_key=?",id,input.get("source_key"));
        if (!existing.isEmpty()) {
            if (existing.getFirst().get("purged_at") != null) return publicRow(existing.getFirst());
            throw new ApiException(409,"已有未删除任务，不能用迁移墓碑覆盖。");
        }
        jdbc.update("INSERT INTO jobs(id,agent,title,mode,status,created_at,updated_at,params,logs,source_key,review_id,deleted_at,purged_at) VALUES(?,?,'已永久删除','demo','incomplete',?,?,'{}','[]',?,?,?,?)",
            id,input.get("agent"),input.getOrDefault("created_at",purged),input.getOrDefault("updated_at",purged),input.get("source_key"),input.get("review_id"),purged,purged);
        return get(id);
    }
    public synchronized Map<String,Object> patch(String id, Map<String,Object> values) {
        Values.only(values,PATCH); validate(values);
        return transactions.execute(status -> {
            var row = get(id);
            if (row.get("purged_at") != null) throw new ApiException(409,"任务已永久删除，不能修改或恢复。");
            if (values.containsKey("deleted_at") && values.get("deleted_at") != null && Set.of("queued","running").contains(row.get("status"))) throw new ApiException(409,"运行中任务不能移到回收站。");
            if (!values.isEmpty()) {
                var args = new ArrayList<>(values.values()); args.add(id);
                jdbc.update("UPDATE jobs SET " + String.join(",",values.keySet().stream().map(key -> key + "=?").toList()) + " WHERE id=?",args.toArray());
            }
            return get(id);
        });
    }
    public synchronized int recover(String error, String updatedAt) {
        Values.text(error,8192,false); Values.text(updatedAt,100,false);
        return jdbc.update("""
            UPDATE jobs SET status='interrupted',error=?,updated_at=?
            WHERE status IN ('queued','running') AND purged_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM dispatch_events e WHERE e.task_id=jobs.id AND e.finished_at IS NULL)
            """,error,updatedAt);
    }
    public synchronized void storeReport(String id, Map<String,Object> payload, ReportStore reports) {
        var row = get(id);
        if (row.get("purged_at") != null) throw new ApiException(409,"任务已永久删除，不能重新写入报告。");
        reports.put(id,payload);
    }
    public synchronized Map<String,Object> purge(String id, ReportStore reports) {
        return transactions.execute(status -> {
            var row = get(id);
            if (row.get("purged_at") != null) return row;
            if (row.get("deleted_at") == null || Set.of("queued","running").contains(row.get("status"))) throw new ApiException(409,"仅可永久删除回收站中已结束的任务。");
            // Redis first: failed deletion leaves all metadata available for retry.
            // A commit failure after DEL is also retryable because DEL is idempotent.
            reports.delete(id);
            String now = OffsetDateTime.now().toString();
            jdbc.update("UPDATE dispatch_events SET action_json=NULL,fingerprint='',finished_at=coalesce(finished_at,?) WHERE task_id=?",now,id);
            jdbc.update("UPDATE jobs SET purged_at=?,updated_at=?,title='已永久删除',params='{}',logs='[]',report_root=NULL,report_path=NULL,error=NULL WHERE id=?",now,now,id);
            return get(id);
        });
    }
    public boolean healthy() { return jdbc.queryForObject("SELECT 1",Integer.class) == 1; }
}
