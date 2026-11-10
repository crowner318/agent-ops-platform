package local.agentstorage;

import java.util.Map;
import org.springframework.dao.DataAccessException;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.resource.NoResourceFoundException;

@RestControllerAdvice
public class ApiErrors {
    @ExceptionHandler(ApiException.class)
    ResponseEntity<?> known(ApiException error) { return ResponseEntity.status(error.status()).body(Map.of("error", error.getMessage())); }
    @ExceptionHandler({IllegalArgumentException.class, HttpMessageNotReadableException.class})
    ResponseEntity<?> invalid(Exception error) { return ResponseEntity.badRequest().body(Map.of("error", "输入JSON、字段或参数无效。")); }
    @ExceptionHandler(NoResourceFoundException.class)
    ResponseEntity<?> missing(Exception error) { return ResponseEntity.status(404).body(Map.of("error", "存储接口不存在。")); }
    @ExceptionHandler(DataAccessException.class)
    ResponseEntity<?> database(Exception error) { return ResponseEntity.status(503).body(Map.of("error", "存储服务暂不可用，数据未被替换为内存记录。")); }
    @ExceptionHandler(Exception.class)
    ResponseEntity<?> unexpected(Exception error) { return ResponseEntity.status(500).body(Map.of("error", "存储操作未完成，请检查服务状态。")); }
}
