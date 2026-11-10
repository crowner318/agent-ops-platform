package local.agentstorage;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.support.TransactionTemplate;

@Repository
public class CaseRepository {
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final TransactionTemplate transactions;
    public CaseRepository(JdbcTemplate jdbc, ObjectMapper mapper, TransactionTemplate transactions) {
        this.jdbc=jdbc;this.mapper=mapper;this.transactions=transactions;
        jdbc.execute("CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,service TEXT NOT NULL,environment TEXT NOT NULL,summary TEXT NOT NULL,verified INTEGER NOT NULL,source_job_id TEXT,payload TEXT NOT NULL)");
        jdbc.execute("CREATE TABLE IF NOT EXISTS case_tombstones(id TEXT PRIMARY KEY)");
    }
    public List<Map<String,Object>> list(String service, String environment) {
        var predicates = new ArrayList<String>(); var args = new ArrayList<Object>();
        if (service != null) { predicates.add("service=?"); args.add(Values.id(service)); }
        if (environment != null) { predicates.add("environment=?"); args.add(Values.id(environment)); }
        String sql = "SELECT payload FROM cases" + (predicates.isEmpty() ? "" : " WHERE " + String.join(" AND ",predicates)) + " ORDER BY rowid DESC LIMIT 1000";
        return jdbc.query(sql,(result,index) -> decode(result.getString(1)),args.toArray());
    }
    private Map<String,Object> decode(String value) {
        try { return mapper.readValue(value,new TypeReference<>(){}); }
        catch (Exception e) { throw new ApiException(503,"案例内容无法读取。"); }
    }
    public Map<String,Object> save(Map<String,Object> value) {
        String id = Values.id(value.get("id")), service=Values.id(value.get("service")), environment=Values.id(value.get("environment"));
        String summary = Values.text(value.get("summary"),60000,false);
        if (!(value.get("verified") instanceof Boolean verified)) throw new ApiException(400,"案例verified必须明确为布尔值。");
        Object source = value.get("source_job_id"); if (source != null) Values.id(source);
        String payload;
        try { payload = mapper.writeValueAsString(value); Values.bytes(payload,262144); }
        catch (com.fasterxml.jackson.core.JsonProcessingException e) { throw new ApiException(400,"案例内容格式无效。"); }
        return transactions.execute(status -> {
            if (jdbc.queryForObject("SELECT count(*) FROM case_tombstones WHERE id=?",Integer.class,id) > 0) throw new ApiException(409,"该案例已永久删除，不能由历史同步重新创建。");
            jdbc.update("INSERT INTO cases(id,service,environment,summary,verified,source_job_id,payload) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET service=excluded.service,environment=excluded.environment,summary=excluded.summary,verified=excluded.verified,source_job_id=excluded.source_job_id,payload=excluded.payload",id,service,environment,summary,verified ? 1 : 0,source,payload);
            return value;
        });
    }
    public void delete(String id) {
        Values.id(id);
        transactions.executeWithoutResult(status -> {
            int exists = jdbc.queryForObject("SELECT count(*) FROM cases WHERE id=?",Integer.class,id);
            int tombstone = jdbc.queryForObject("SELECT count(*) FROM case_tombstones WHERE id=?",Integer.class,id);
            if (exists == 0 && tombstone == 0) throw new ApiException(404,"案例不存在。");
            jdbc.update("INSERT OR IGNORE INTO case_tombstones(id) VALUES(?)",id);
            jdbc.update("DELETE FROM cases WHERE id=?",id);
        });
    }
    public void importTombstone(String id) {
        Values.id(id);
        transactions.executeWithoutResult(status -> {
            jdbc.update("INSERT OR IGNORE INTO case_tombstones(id) VALUES(?)",id);
            jdbc.update("DELETE FROM cases WHERE id=?",id);
        });
    }
    public List<String> tombstones() { return jdbc.queryForList("SELECT id FROM case_tombstones ORDER BY rowid",String.class); }
}
