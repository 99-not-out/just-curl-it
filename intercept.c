/*
 * intercept.so - LD_PRELOAD shim that intercepts HTTP calls to configured hosts
 * and pipes them to a local handler script via a loopback TCP socket.
 *
 * Matching is by hostname (via getaddrinfo). Matched hosts get a fabricated
 * 127.0.0.X address; at connect() time we notice that address, spawn a handler
 * process, and redirect the connect to a just-created loopback listener that
 * the handler accepts. The caller uses a normal AF_INET socket throughout.
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_ROUTES 64
#define CONFIG_PATH_DEFAULT "/etc/intercept/routes.conf"

struct route {
    char host[256];
    char handler[512];
    uint32_t sentinel_ip; /* network byte order */
};

static struct route g_routes[MAX_ROUTES];
static int g_route_count = 0;
static int g_initialized = 0;

static int (*real_connect)(int, const struct sockaddr *, socklen_t) = NULL;
static int (*real_getaddrinfo)(const char *, const char *,
                               const struct addrinfo *,
                               struct addrinfo **) = NULL;

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
        char host[256] = {0}, handler[512] = {0};
        if (sscanf(p, "%255s %511[^\n]", host, handler) != 2) continue;
        trim(handler);
        if (handler[0] == 0) continue;
        strncpy(g_routes[g_route_count].host, host, sizeof(g_routes[0].host)-1);
        strncpy(g_routes[g_route_count].handler, handler, sizeof(g_routes[0].handler)-1);
        /* Assign sentinel IP 127.0.0.(2 + idx) -- skip 127.0.0.1 to avoid clash */
        g_routes[g_route_count].sentinel_ip = htonl(0x7F000002 + g_route_count);
        g_route_count++;
    }
    fclose(f);
    dbg("loaded %d route(s) from %s", g_route_count, path);
    for (int i = 0; i < g_route_count; i++) {
        struct in_addr a = { .s_addr = g_routes[i].sentinel_ip };
        char ip[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &a, ip, sizeof(ip));
        dbg("  %s -> %s via %s", g_routes[i].host, ip, g_routes[i].handler);
    }
}

static void init_once(void) {
    if (g_initialized) return;
    g_initialized = 1;
    real_connect = dlsym(RTLD_NEXT, "connect");
    real_getaddrinfo = dlsym(RTLD_NEXT, "getaddrinfo");
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

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
    init_once();
    if (node) {
        struct route *r = find_route_by_host(node);
        if (r) {
            char ipstr[INET_ADDRSTRLEN];
            struct in_addr a = { .s_addr = r->sentinel_ip };
            inet_ntop(AF_INET, &a, ipstr, sizeof(ipstr));
            dbg("getaddrinfo: %s -> %s (intercept)", node, ipstr);
            /* Force AF_INET so our v4 sentinel is used even if caller asked
             * for AF_UNSPEC (which might otherwise prefer v6). */
            struct addrinfo h;
            if (hints) h = *hints; else memset(&h, 0, sizeof(h));
            h.ai_family = AF_INET;
            return real_getaddrinfo(ipstr, service, &h, res);
        }
    }
    return real_getaddrinfo(node, service, hints, res);
}

static int spawn_handler_and_connect(int sockfd, struct route *r) {
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

    /* Double-fork so the grandchild is reaped by init, no zombies in the
     * caller's process. */
    pid_t pid = fork();
    if (pid < 0) { close(listen_fd); return -1; }
    if (pid == 0) {
        pid_t pid2 = fork();
        if (pid2 < 0) _exit(1);
        if (pid2 > 0) _exit(0);

        /* grandchild */
        unsetenv("LD_PRELOAD");  /* don't intercept the handler's own calls */
        setenv("INTERCEPT_HOST", r->host, 1);

        int conn = accept(listen_fd, NULL, NULL);
        close(listen_fd);
        if (conn < 0) _exit(1);
        dup2(conn, 0);
        dup2(conn, 1);
        if (conn > 1) close(conn);

        execl("/bin/sh", "sh", "-c", r->handler, (char *)NULL);
        _exit(127);
    }

    close(listen_fd);
    /* Reap the first-child (exits immediately); grandchild is orphaned. */
    waitpid(pid, NULL, 0);

    struct sockaddr_in target = {0};
    target.sin_family = AF_INET;
    target.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    target.sin_port = htons(port);
    return real_connect(sockfd, (struct sockaddr *)&target, sizeof(target));
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    init_once();
    if (addr && addr->sa_family == AF_INET &&
        addrlen >= (socklen_t)sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        struct route *r = find_route_by_ip(sin->sin_addr.s_addr);
        if (r) {
            dbg("connect: intercept %s (requested port %d)",
                r->host, ntohs(sin->sin_port));
            return spawn_handler_and_connect(sockfd, r);
        }
    }
    return real_connect(sockfd, addr, addrlen);
}
