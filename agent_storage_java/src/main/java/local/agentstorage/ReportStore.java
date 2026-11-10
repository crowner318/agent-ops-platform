package local.agentstorage;

import java.util.List;
import java.util.Map;
import java.util.Set;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Repository;

@Repository
public class ReportStore {
    private static final int MAX_REPORT = 8 * 1024 * 1024;
    private static final DefaultRedisScript<Long> PUT = new DefaultRedisScript<>("redis.call('SET', KEYS[1], ARGV[1]); return 1",Long.class);
    private static final DefaultRedisScript<Long> DELETE = new DefaultRedisScript<>("return redis.call('DEL', KEYS[1])",Long.class);
    private final StringRedisTemplate redis;
    private final ObjectMapper mapper;
    public ReportStore(StringRedisTemplate redis, ObjectMapper mapper) { this.redis=redis; this.mapper=mapper; }
    private String key(String id) { return "agent:reports:v1:" + Values.id(id); }
    public void put(String id, Map<String,Object> payload) {
        Values.only(payload,Set.of("json","markdown"));
        if (!(payload.get("json") instanceof Map<?,?>) || !(payload.get("markdown") instanceof String)) throw new ApiException(400,"报告需要JSON对象和Markdown正文。");
        String markdown = Values.text(payload.get("markdown"), MAX_REPORT, true);
        Values.bytes(markdown,MAX_REPORT);
        try {
            String json = mapper.writeValueAsString(payload.get("json")); Values.bytes(json,MAX_REPORT);
            String packed = mapper.writeValueAsString(payload);
            Long written = redis.execute(PUT,List.of(key(id)),packed);
            if (!Long.valueOf(1).equals(written)) throw unavailable();
        } catch (DataAccessException e) { throw unavailable(); }
          catch (com.fasterxml.jackson.core.JsonProcessingException e) { throw new ApiException(400,"报告JSON无法序列化。"); }
    }
    public Map<String,Object> get(String id) {
        try {
            String value = redis.opsForValue().get(key(id));
            if (value == null) throw new ApiException(404,"Redis中没有该报告，未回退读取本地副本。");
            Values.bytes(value,MAX_REPORT * 2 + 65536);
            Map<String,Object> payload = mapper.readValue(value,new TypeReference<>(){});
            if (!(payload.get("json") instanceof Map<?,?>) || !(payload.get("markdown") instanceof String)) throw new ApiException(503,"Redis报告格式无效，未回退读取本地副本。");
            return payload;
        } catch (DataAccessException e) { throw unavailable(); }
          catch (com.fasterxml.jackson.core.JsonProcessingException e) { throw new ApiException(503,"Redis报告格式无效，未回退读取本地副本。"); }
    }
    public void delete(String id) {
        try {
            redis.execute(DELETE,List.of(key(id)));
        } catch (DataAccessException e) { throw unavailable(); }
    }
    public boolean healthy() {
        try (var connection = redis.getConnectionFactory().getConnection()) { return "PONG".equals(connection.ping()); }
        catch (RuntimeException e) { return false; }
    }
    private ApiException unavailable() { return new ApiException(503,"Redis暂不可用，报告操作未完成；不会静默写入内存或本地文件。"); }
}
