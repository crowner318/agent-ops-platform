package local.agentstorage;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

final class Values {
    private static final Pattern ID = Pattern.compile("[A-Za-z0-9_-]{1,80}");
    static String id(Object value) {
        if (!(value instanceof String text) || !ID.matcher(text).matches()) throw new ApiException(400, "编号、服务或环境仅允许1至80个英文字母、数字、下划线或连字符。");
        return text;
    }
    static String text(Object value, int max, boolean empty) {
        if (!(value instanceof String text) || text.length() > max || text.indexOf(0) >= 0 || (!empty && text.isBlank())) throw new ApiException(400, "文本字段为空、过长或类型无效。");
        return text;
    }
    static void only(Map<String, ?> value, Set<String> fields) {
        if (value == null || !fields.containsAll(value.keySet())) throw new ApiException(400, "提交包含未知字段。");
    }
    @SuppressWarnings("unchecked")
    static Map<String,Object> object(Object value) {
        if (!(value instanceof Map<?,?> map) || map.keySet().stream().anyMatch(key -> !(key instanceof String))) {
            throw new ApiException(400,"必要对象缺失或格式无效。");
        }
        return (Map<String,Object>) value;
    }
    static String jsonString(Object value, ObjectMapper mapper, boolean object, int max) {
        String text = text(value, max, true);
        try {
            JsonNode node = mapper.readTree(text);
            if (node == null || (object ? !node.isObject() : !node.isArray())) throw new IllegalArgumentException();
        } catch (Exception e) { throw new ApiException(400, "JSON字符串字段格式无效。"); }
        return text;
    }
    static void confirmation(Map<String, Object> payload, String id) {
        only(payload, Set.of("confirmation"));
        if (!id.equals(payload.get("confirmation"))) throw new ApiException(400, "永久删除需明确确认当前记录编号。");
    }
    static void bytes(String text, int max) {
        if (text.getBytes(StandardCharsets.UTF_8).length > max) throw new ApiException(413, "内容超过存储大小限制。");
    }
    private Values() {}
}
