import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/** Only the HTTP dependency-health result determines container readiness. */
public final class HealthProbe {
    public static void main(String[] args) throws Exception {
        if (args.length != 1) System.exit(2);
        var request = HttpRequest.newBuilder(URI.create(args[0])).timeout(Duration.ofSeconds(3)).GET().build();
        var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        var response = client.send(request, HttpResponse.BodyHandlers.discarding());
        if (response.statusCode() != 200) System.exit(1);
    }
}
