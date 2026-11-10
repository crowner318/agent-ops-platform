package local.agentstorage;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
public class StorageTokenFilter extends OncePerRequestFilter {
    private final byte[] token;
    public StorageTokenFilter(@Value("${storage.service-token}") String value) {
        if (value == null || value.length() < 24 || value.length() > 512 || !value.chars().allMatch(c -> c >= 33 && c <= 126)) {
            throw new IllegalStateException("STORAGE_SERVICE_TOKEN必须设置为24至512字符的独立ASCII访问令牌。");
        }
        token = value.getBytes(StandardCharsets.UTF_8);
    }
    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain) throws ServletException, IOException {
        response.setHeader("Cache-Control", "no-store");
        response.setHeader("X-Content-Type-Options", "nosniff");
        // Protect decoded/encoded route variants too; only the exact GET health
        // endpoint is public. Prefix matching on a raw URI could bypass auth.
        if (!(request.getMethod().equals("GET") && request.getRequestURI().equals("/health"))) {
            var values = Collections.list(request.getHeaders("X-Storage-Token"));
            if (values.size() != 1 || !MessageDigest.isEqual(token, values.getFirst().getBytes(StandardCharsets.UTF_8))) {
                reject(response, 401, "存储服务访问令牌无效。"); return;
            }
            if (request.getContentLengthLong() > 20L * 1024 * 1024 || request.getHeader("Transfer-Encoding") != null) {
                reject(response, 413, "请求过长或不支持分块提交。"); return;
            }
        }
        chain.doFilter(request, response);
    }
    private void reject(HttpServletResponse response, int status, String error) throws IOException {
        response.setStatus(status); response.setCharacterEncoding("UTF-8"); response.setContentType("application/json");
        response.getWriter().write("{\"error\":\"" + error + "\"}");
    }
}
