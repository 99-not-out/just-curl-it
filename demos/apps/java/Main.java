// Demo: drive multiple jci-routed backends from a single Java process.
//
// Run this from inside a `jci use demos` shell. Single-file source-launch
// works on Java 11+:
//
//     /opt/homebrew/opt/openjdk/bin/java Main.java
//
// What this proves: java.net.http.HttpClient resolves via libc's
// getaddrinfo and connects via libc's connect, both interposed by jci.
// No backend client libraries used.
//
// macOS: brew install openjdk    (system /usr/bin/java is hardened-runtime
//                                 and won't load the shim)

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

public class Main {
    static final HttpClient client = HttpClient.newHttpClient();

    public static void main(String[] args) throws Exception {
        section("shim meta");
        get("http://mylocal.shim/routes");

        section("postgres");
        get("http://mylocal.postgres/query?sql=select%20now()%3A%3Atext%2C%20version()");

        section("redis: set + get");
        post("http://mylocal.redis/key/from-java", "hello from java", "text/plain");
        get("http://mylocal.redis/key/from-java");

        section("mongo: insert + find");
        post("http://mylocal.mongo/db/demo/coll/java_users",
             "{\"name\":\"bob\",\"lang\":\"java\"}", "application/json");
        get("http://mylocal.mongo/db/demo/coll/java_users?filter=%7B%7D");

        section("kafka: produce + peek");
        post("http://mylocal.kafka/topic/javademo/records",
             "java-key|hello-from-java", "text/plain");
        get("http://mylocal.kafka/topic/javademo/records?limit=5");

        section("rabbit: publish + get");
        post("http://mylocal.rabbit/queue/javademo/publish",
             "hello from java", "text/plain");
        get("http://mylocal.rabbit/queue/javademo/get");

        section("smtp");
        post("http://mylocal.smtp/send",
             "{\"to\":\"java@example.com\",\"subject\":\"hi from java\","
             + "\"body\":\"sent via jci\"}",
             "application/json");
    }

    static void section(String title) {
        System.out.println("\n=== " + title + " ===");
    }

    static void get(String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder().uri(URI.create(url)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        System.out.println(resp.body());
    }

    static void post(String url, String body, String contentType) throws Exception {
        HttpRequest req = HttpRequest.newBuilder()
            .uri(URI.create(url))
            .header("content-type", contentType)
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        System.out.println(resp.body());
    }
}
