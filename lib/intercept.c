/*
 * intercept.{so,dylib} -- LD_PRELOAD / DYLD_INSERT_LIBRARIES shim that
 * intercepts HTTP calls to configured hostnames and pipes them to a local
 * handler script over a freshly-bound loopback TCP socket.
 *
 * Routing strategy (works on Linux + macOS without loopback aliases or root):
 *   getaddrinfo(host)        -> 127.0.0.<2+idx>:<caller's port>
 *   connect(127.0.0.<2+idx>) -> bind ephemeral, fork handler, redirect there
 *
 * Why sentinel IPs (and not port-encoding): some HTTP clients (curl) ignore
 * the port returned from getaddrinfo and substitute the URL's port at connect
 * time. So encoding the route in the *port* breaks them. Encoding it in the
 * *IP* survives — the IP is what curl preserves all the way to connect().
 *
 * Why this works on macOS without `ifconfig lo0 alias 127.0.0.X`: we never
 * actually connect or bind to 127.0.0.X. The userspace connect() interposer
 * catches the call before any packet leaves; the kernel never sees the
 * sentinel address.
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if !defined(__APPLE__)
#include <dlfcn.h>
#endif

#define MAX_ROUTES        64
#define HANDLER_TIMEOUT   30   /* seconds the handler waits on accept() */

#define CONFIG_PATH_DEFAULT "/etc/intercept/routes.conf"

struct route {
    char host[256];
    char methods[64];       /* comma-separated allow-list, "" = all */
    char handler[512];
    uint32_t sentinel_ip;   /* network byte order: 127.0.0.<2+idx> */
};

static struct route g_routes[MAX_ROUTES];
static int g_route_count = 0;
static int g_initialized = 0;

#if !defined(__APPLE__)
static int (*real_connect)(int, const struct sockaddr *, socklen_t) = NULL;
static int (*real_getaddrinfo)(const char *, const char *,
                               const struct addrinfo *,
                               struct addrinfo **) = NULL;
#endif

/*
 * Calling the real symbol differs by platform:
 *   - Linux: dlsym(RTLD_NEXT, ...) lookup once at init.
 *   - macOS: dyld interposition only redirects calls from *other* images,
 *            so a plain call to connect()/getaddrinfo() from inside this
 *            dylib hits libc directly. No dlsym dance needed.
 */
static int call_real_connect(int fd, const struct sockaddr *a, socklen_t l) {
#if defined(__APPLE__)
    return connect(fd, a, l);
#else
    return real_connect(fd, a, l);
#endif
}

static int call_real_getaddrinfo(const char *node, const char *service,
                                 const struct addrinfo *hints,
                                 struct addrinfo **res) {
#if defined(__APPLE__)
    return getaddrinfo(node, service, hints, res);
#else
    return real_getaddrinfo(node, service, hints, res);
#endif
}

static void dbg(const char *fmt, ...) {
    const char *d = getenv("INTERCEPT_DEBUG");
    if (!d || d[0] == 0 || d[0] == '0') return;
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "[intercept] ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
}

static void trim(char *s) {
    size_t n = strlen(s);
    while (n > 0 && (s[n-1] == '\n' || s[n-1] == '\r' ||
                     s[n-1] == ' '  || s[n-1] == '\t')) {
        s[--n] = 0;
    }
}

/* A "method token" is a non-empty string of [A-Z], comma, or '*' only.
 * Used to detect an optional method allow-list between host and handler. */
static int is_method_token(const char *s) {
    if (!s || !*s) return 0;
    for (const char *q = s; *q; q++) {
        if (!((*q >= 'A' && *q <= 'Z') || *q == ',' || *q == '*')) return 0;
    }
    return 1;
}

static void load_config(void) {
    const char *path = getenv("INTERCEPT_CONFIG");
    if (!path) path = CONFIG_PATH_DEFAULT;
    FILE *f = fopen(path, "r");
    if (!f) {
        dbg("no config at %s", path);
        return;
    }
    char line[1024];
    while (fgets(line, sizeof(line), f) && g_route_count < MAX_ROUTES) {
        char *p = line;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '#' || *p == '\n' || *p == 0) continue;

        /* Format:  <host>  [<METHODS>]  <handler-cmd...>
         * Detect METHODS by trying a 3-token parse first; if the second token
         * is all uppercase, commas, or asterisks, it's the allow-list,
         * else fall back to the
         * 2-token form (no methods, handler is everything after the host). */
        char host[256] = {0}, first[64] = {0}, rest[512] = {0};
        int n = sscanf(p, "%255s %63s %511[^\n]", host, first, rest);
        if (n < 2) continue;

        struct route *r = &g_routes[g_route_count];
        memset(r, 0, sizeof(*r));

        if (n == 3 && is_method_token(first)) {
            strncpy(r->methods, first, sizeof(r->methods)-1);
            trim(rest);
            strncpy(r->handler, rest, sizeof(r->handler)-1);
        } else if (n == 3) {
            char joined[512];
            snprintf(joined, sizeof(joined), "%s %s", first, rest);
            trim(joined);
            strncpy(r->handler, joined, sizeof(r->handler)-1);
        } else {
            trim(first);
            strncpy(r->handler, first, sizeof(r->handler)-1);
        }
        if (r->handler[0] == 0) continue;
        strncpy(r->host, host, sizeof(r->host)-1);
        /* Skip 127.0.0.1 to avoid clashing with real local services. */
        r->sentinel_ip = htonl(0x7F000002 + g_route_count);
        g_route_count++;
    }
    fclose(f);
    dbg("loaded %d route(s) from %s", g_route_count, path);
    for (int i = 0; i < g_route_count; i++) {
        struct in_addr a = { .s_addr = g_routes[i].sentinel_ip };
        char ip[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &a, ip, sizeof(ip));
        dbg("  %s [%s] -> %s via %s",
            g_routes[i].host,
            g_routes[i].methods[0] ? g_routes[i].methods : "*",
            ip, g_routes[i].handler);
    }
}

static void init_once(void) {
    if (g_initialized) return;
    g_initialized = 1;
#if !defined(__APPLE__)
    real_connect = dlsym(RTLD_NEXT, "connect");
    real_getaddrinfo = dlsym(RTLD_NEXT, "getaddrinfo");
#endif
    load_config();
}

__attribute__((constructor))
static void init_ctor(void) { init_once(); }

static struct route *find_route_by_host(const char *host) {
    for (int i = 0; i < g_route_count; i++) {
        if (strcasecmp(g_routes[i].host, host) == 0) return &g_routes[i];
    }
    return NULL;
}

static struct route *find_route_by_ip(uint32_t ip_net) {
    for (int i = 0; i < g_route_count; i++) {
        if (g_routes[i].sentinel_ip == ip_net) return &g_routes[i];
    }
    return NULL;
}

static int do_getaddrinfo(const char *node, const char *service,
                          const struct addrinfo *hints, struct addrinfo **res) {
    init_once();
    if (node) {
        struct route *r = find_route_by_host(node);
        if (r) {
            struct in_addr a = { .s_addr = r->sentinel_ip };
            char ipstr[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &a, ipstr, sizeof(ipstr));
            dbg("getaddrinfo: %s -> %s (intercept)", node, ipstr);
            /* Pass the caller's service through unchanged: clients like curl
             * trust the IP from getaddrinfo but override the port from the
             * URL, so we route on the IP and let the port fall where it may. */
            struct addrinfo h;
            if (hints) h = *hints; else memset(&h, 0, sizeof(h));
            h.ai_family = AF_INET;  /* force v4 even for AF_UNSPEC callers */
            return call_real_getaddrinfo(ipstr, service, &h, res);
        }
    }
    return call_real_getaddrinfo(node, service, hints, res);
}

static int spawn_handler_and_connect(int sockfd, struct route *r,
                                     sa_family_t caller_family) {
    int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) return -1;
    int one = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in la = {0};
    la.sin_family = AF_INET;
    la.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    la.sin_port = 0;
    if (bind(listen_fd, (struct sockaddr *)&la, sizeof(la)) < 0 ||
        listen(listen_fd, 1) < 0) {
        close(listen_fd);
        return -1;
    }
    socklen_t slen = sizeof(la);
    getsockname(listen_fd, (struct sockaddr *)&la, &slen);
    uint16_t port = ntohs(la.sin_port);
    dbg("spawning handler for %s on 127.0.0.1:%u", r->host, port);

    /* Double-fork: grandchild reparents to init, no zombies in the caller. */
    pid_t pid = fork();
    if (pid < 0) { close(listen_fd); return -1; }
    if (pid == 0) {
        pid_t pid2 = fork();
        if (pid2 < 0) _exit(1);
        if (pid2 > 0) _exit(0);

        /* grandchild */
#if defined(__APPLE__)
        unsetenv("DYLD_INSERT_LIBRARIES");
#else
        unsetenv("LD_PRELOAD");
#endif
        setenv("INTERCEPT_HOST", r->host, 1);
        if (r->methods[0]) {
            setenv("INTERCEPT_METHODS", r->methods, 1);
        } else {
            unsetenv("INTERCEPT_METHODS");
        }

        /* Bound the wait so a caller that dies between listen() and connect()
         * can't leave us blocked forever in accept(). SIGALRM defaults to
         * killing the process, which is exactly what we want. */
        alarm(HANDLER_TIMEOUT);

        int conn = accept(listen_fd, NULL, NULL);
        close(listen_fd);
        if (conn < 0) _exit(1);
        alarm(0);

        dup2(conn, 0);
        dup2(conn, 1);
        if (conn > 1) close(conn);

        execl("/bin/sh", "sh", "-c", r->handler, (char *)NULL);
        _exit(127);
    }

    close(listen_fd);
    waitpid(pid, NULL, 0);  /* reap first child; grandchild is orphaned */

    /* Build the redirect target to match the *socket's* family. If the caller
     * gave us a v4-mapped IPv6 sockaddr (Java does this), the socket is
     * AF_INET6 and won't accept an AF_INET destination -- redirect to
     * ::ffff:127.0.0.1:<ephem> instead. */
    if (caller_family == AF_INET6) {
        struct sockaddr_in6 t6 = {0};
        t6.sin6_family = AF_INET6;
        t6.sin6_port = htons(port);
        t6.sin6_addr.s6_addr[10] = 0xff;
        t6.sin6_addr.s6_addr[11] = 0xff;
        t6.sin6_addr.s6_addr[12] = 127;
        t6.sin6_addr.s6_addr[15] = 1;  /* ::ffff:127.0.0.1 */
        return call_real_connect(sockfd, (struct sockaddr *)&t6, sizeof(t6));
    }
    struct sockaddr_in target = {0};
    target.sin_family = AF_INET;
    target.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    target.sin_port = htons(port);
    return call_real_connect(sockfd, (struct sockaddr *)&target, sizeof(target));
}

static int do_connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    init_once();
    if (addr && addr->sa_family == AF_INET &&
        addrlen >= (socklen_t)sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        struct route *r = find_route_by_ip(sin->sin_addr.s_addr);
        if (r) {
            dbg("connect: intercept %s (caller's port %u)",
                r->host, ntohs(sin->sin_port));
            return spawn_handler_and_connect(sockfd, r, addr->sa_family);
        }
    }
    /* Java's HttpClient (and some other v6-preferring stacks) convert IPv4
     * addrinfo results into ::ffff:a.b.c.d IPv4-mapped IPv6 sockaddrs and
     * connect via AF_INET6. Detect those and route on the embedded v4. */
    if (addr && addr->sa_family == AF_INET6 &&
        addrlen >= (socklen_t)sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)addr;
        if (IN6_IS_ADDR_V4MAPPED(&sin6->sin6_addr)) {
            uint32_t v4 = ((const uint32_t *)&sin6->sin6_addr)[3];
            struct route *r = find_route_by_ip(v4);
            if (r) {
                dbg("connect: intercept %s (v4-mapped v6, port %u)",
                    r->host, ntohs(sin6->sin6_port));
                return spawn_handler_and_connect(sockfd, r, addr->sa_family);
            }
        }
    }
    return call_real_connect(sockfd, addr, addrlen);
}

#if defined(__APPLE__)

#define DYLD_INTERPOSE(_repl, _orig)                                    \
    __attribute__((used)) static struct {                               \
        const void *r;                                                  \
        const void *o;                                                  \
    } _interpose_##_orig                                                \
        __attribute__((section("__DATA,__interpose"))) = {              \
        (const void *)(unsigned long)&_repl,                            \
        (const void *)(unsigned long)&_orig                             \
    }

static int my_connect(int s, const struct sockaddr *a, socklen_t l) {
    return do_connect(s, a, l);
}
static int my_getaddrinfo(const char *n, const char *s,
                          const struct addrinfo *h, struct addrinfo **r) {
    return do_getaddrinfo(n, s, h, r);
}
DYLD_INTERPOSE(my_connect, connect);
DYLD_INTERPOSE(my_getaddrinfo, getaddrinfo);

#else  /* Linux: classic LD_PRELOAD symbol shadowing */

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    return do_connect(sockfd, addr, addrlen);
}

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
    return do_getaddrinfo(node, service, hints, res);
}

#endif
