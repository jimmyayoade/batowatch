// SPDX-License-Identifier: GPL-3.0-or-later
// bw-flows: count network traffic per connection for BatoWatch, on kernels
// without connection tracking (Batocera 43 has no usable netfilter).
//
// Reads packet headers from a raw packet socket (AF_PACKET, TPACKET_V3 ring
// shared with the kernel, the way libpcap and tcpdump do it). It only reads;
// it never sends, drops or changes a packet.
//
//   bw-flows [-i SECONDS]   one JSON line per interval on stdout (default 1)
//   bw-flows -t SECONDS     test: run, then print the top flows and CPU use
//
// JSON line: {"time":T,"packets":N,"drops":N,"flows":[[iface,dir,proto,
//            src,sport,dst,dport,packets,bytes,last_seen],...]}
// Counters are totals since the flow was first seen; flows idle for
// IDLE_SECONDS are forgotten.

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <linux/filter.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define SNAPLEN       128          // bytes of each packet copied: headers only
#define BLOCK_SIZE    (1 << 20)
#define BLOCK_COUNT   8
#define FRAME_SIZE    2048
#define BLOCK_TIMEOUT 100          // ms before the kernel hands over a part-full block
#define TABLE_SIZE    (1 << 15)    // flow slots (power of two)
#define IDLE_SECONDS  120

struct key {
    uint32_t ifindex;
    uint8_t dir;                   // 0 = in, 1 = out
    uint8_t proto;
    uint8_t family;                // 4 or 6
    uint8_t pad;
    uint16_t sport, dport;
    uint8_t src[16], dst[16];
};

struct flow {
    struct key k;
    uint64_t packets, bytes;
    time_t last;
    uint8_t used;
};

static struct flow *table, *spare;
static uint32_t nflows;
static uint64_t table_full;
static volatile sig_atomic_t stop;

static void on_signal(int sig) { (void)sig; stop = 1; }

static uint32_t hash_key(const struct key *k)
{
    const uint8_t *p = (const uint8_t *)k;
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < sizeof *k; i++)
        h = (h ^ p[i]) * 16777619u;
    return h;
}

static struct flow *find(struct flow *t, const struct key *k, int create)
{
    uint32_t i = hash_key(k) & (TABLE_SIZE - 1);
    for (uint32_t n = 0; n < TABLE_SIZE; n++, i = (i + 1) & (TABLE_SIZE - 1)) {
        if (!t[i].used) {
            if (!create)
                return NULL;
            if (nflows >= TABLE_SIZE * 3 / 4) {
                table_full++;
                return NULL;
            }
            t[i].used = 1;
            t[i].k = *k;
            t[i].packets = t[i].bytes = 0;
            nflows++;
            return &t[i];
        }
        if (memcmp(&t[i].k, k, sizeof *k) == 0)
            return &t[i];
    }
    return NULL;
}

// Drop flows idle for IDLE_SECONDS by copying the live ones into a fresh table.
static void expire(time_t now)
{
    memset(spare, 0, sizeof(struct flow) * TABLE_SIZE);
    uint32_t kept = 0;
    struct flow *old = table;
    table = spare;
    spare = old;
    nflows = 0;
    for (uint32_t i = 0; i < TABLE_SIZE; i++) {
        if (old[i].used && now - old[i].last < IDLE_SECONDS) {
            struct flow *f = find(table, &old[i].k, 1);
            if (f) {
                *f = old[i];
                kept++;
            }
        }
    }
    nflows = kept;
}

static void count(const uint8_t *l3, uint32_t caplen, uint16_t ethertype,
                  uint32_t ifindex, uint8_t pkttype, uint32_t len, time_t now)
{
    struct key k;
    const uint8_t *l4;
    uint32_t l4len;

    memset(&k, 0, sizeof k);
    if (ethertype == ETH_P_IP && caplen >= 20) {
        uint32_t ihl = (l3[0] & 0x0f) * 4u;
        k.family = 4;
        k.proto = l3[9];
        memcpy(k.src, l3 + 12, 4);
        memcpy(k.dst, l3 + 16, 4);
        l4 = l3 + ihl;
        l4len = caplen > ihl ? caplen - ihl : 0;
    } else if (ethertype == ETH_P_IPV6 && caplen >= 40) {
        k.family = 6;
        k.proto = l3[6];
        memcpy(k.src, l3 + 8, 16);
        memcpy(k.dst, l3 + 24, 16);
        l4 = l3 + 40;
        l4len = caplen - 40;
    } else {
        return;
    }
    if ((k.proto == IPPROTO_TCP || k.proto == IPPROTO_UDP) && l4len >= 4) {
        k.sport = (uint16_t)(l4[0] << 8 | l4[1]);
        k.dport = (uint16_t)(l4[2] << 8 | l4[3]);
    }
    k.ifindex = ifindex;
    k.dir = pkttype == PACKET_OUTGOING;

    struct flow *f = find(table, &k, 1);
    if (f) {
        f->packets++;
        f->bytes += len;
        f->last = now;
    }
}

static const char *ifname(uint32_t ifindex, char *buf)
{
    if (!if_indextoname(ifindex, buf))
        snprintf(buf, IF_NAMESIZE, "if%u", ifindex);
    return buf;
}

static void addr_str(const struct key *k, const uint8_t *a, char *buf, size_t n)
{
    inet_ntop(k->family == 4 ? AF_INET : AF_INET6, a, buf, (socklen_t)n);
}

static const char *proto_name(uint8_t p, char *buf)
{
    switch (p) {
    case IPPROTO_TCP: return "tcp";
    case IPPROTO_UDP: return "udp";
    case IPPROTO_ICMP: return "icmp";
    case IPPROTO_ICMPV6: return "icmp6";
    default: snprintf(buf, 8, "%u", p); return buf;
    }
}

static void report_json(time_t now, uint64_t packets, uint64_t drops)
{
    char ifb[IF_NAMESIZE], src[INET6_ADDRSTRLEN], dst[INET6_ADDRSTRLEN], pb[8];
    int first = 1;

    printf("{\"time\":%lld,\"packets\":%llu,\"drops\":%llu,\"table_full\":%llu,\"flows\":[",
           (long long)now, (unsigned long long)packets, (unsigned long long)drops,
           (unsigned long long)table_full);
    for (uint32_t i = 0; i < TABLE_SIZE; i++) {
        const struct flow *f = &table[i];
        if (!f->used)
            continue;
        addr_str(&f->k, f->k.src, src, sizeof src);
        addr_str(&f->k, f->k.dst, dst, sizeof dst);
        printf("%s[\"%s\",\"%s\",\"%s\",\"%s\",%u,\"%s\",%u,%llu,%llu,%lld]",
               first ? "" : ",", ifname(f->k.ifindex, ifb), f->k.dir ? "out" : "in",
               proto_name(f->k.proto, pb), src, f->k.sport, dst, f->k.dport,
               (unsigned long long)f->packets, (unsigned long long)f->bytes,
               (long long)f->last);
        first = 0;
    }
    printf("]}\n");
    fflush(stdout);
}

static int by_bytes(const void *a, const void *b)
{
    const struct flow *x = *(const struct flow *const *)a, *y = *(const struct flow *const *)b;
    return x->bytes < y->bytes ? 1 : x->bytes > y->bytes ? -1 : 0;
}

static void report_test(double wall, uint64_t packets, uint64_t drops)
{
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    double cpu = (double)ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 +
                 (double)ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6;
    printf("%llu packets in %.1fs (%.0f/s), CPU %.2fs = %.2f%% of one core, "
           "kernel drops %llu, flows %u\n",
           (unsigned long long)packets, wall, packets / wall, cpu, 100 * cpu / wall,
           (unsigned long long)drops, nflows);

    struct flow **list = malloc(sizeof *list * (nflows + 1));
    uint32_t n = 0;
    for (uint32_t i = 0; i < TABLE_SIZE && list; i++)
        if (table[i].used)
            list[n++] = &table[i];
    if (!list)
        return;
    qsort(list, n, sizeof *list, by_bytes);

    char ifb[IF_NAMESIZE], src[INET6_ADDRSTRLEN + 8], dst[INET6_ADDRSTRLEN + 8], pb[8];
    char a[INET6_ADDRSTRLEN];
    printf("%-11s%-4s%-6s%-42s%-42s%8s%10s\n", "iface", "dir", "proto", "from", "to",
           "pkts", "kbit/s");
    for (uint32_t i = 0; i < n && i < 20; i++) {
        const struct flow *f = list[i];
        addr_str(&f->k, f->k.src, a, sizeof a);
        snprintf(src, sizeof src, "%s:%u", a, f->k.sport);
        addr_str(&f->k, f->k.dst, a, sizeof a);
        snprintf(dst, sizeof dst, "%s:%u", a, f->k.dport);
        printf("%-11s%-4s%-6s%-42s%-42s%8llu%10.1f\n", ifname(f->k.ifindex, ifb),
               f->k.dir ? "out" : "in", proto_name(f->k.proto, pb), src, dst,
               (unsigned long long)f->packets, f->bytes * 8 / wall / 1000);
    }
    free(list);
}

static void usage(void)
{
    fprintf(stderr, "usage: bw-flows [-i SECONDS] | -t SECONDS\n");
    exit(2);
}

int main(int argc, char **argv)
{
    int interval = 1, test = 0, opt;
    while ((opt = getopt(argc, argv, "i:t:")) != -1) {
        if (opt == 'i')
            interval = atoi(optarg);
        else if (opt == 't')
            test = atoi(optarg);
        else
            usage();
    }
    if (interval < 1)
        usage();

    table = calloc(TABLE_SIZE, sizeof *table);
    spare = calloc(TABLE_SIZE, sizeof *spare);
    if (!table || !spare) {
        perror("calloc");
        return 1;
    }

    // SOCK_DGRAM ("cooked"): the kernel strips the link header, so data
    // starts at the IP header on Ethernet and tun (tailscale0) alike.
    int fd = socket(AF_PACKET, SOCK_DGRAM, htons(ETH_P_ALL));
    if (fd < 0) {
        perror("socket(AF_PACKET)");
        return 1;
    }

    // A one-instruction socket filter that keeps the first SNAPLEN bytes of
    // every packet (what tcpdump -s does), so payloads are never copied.
    struct sock_filter snap[] = { BPF_STMT(BPF_RET | BPF_K, SNAPLEN) };
    struct sock_fprog prog = { .len = 1, .filter = snap };
    if (setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &prog, sizeof prog) < 0) {
        perror("SO_ATTACH_FILTER");
        return 1;
    }

    int version = TPACKET_V3;
    if (setsockopt(fd, SOL_PACKET, PACKET_VERSION, &version, sizeof version) < 0) {
        perror("PACKET_VERSION");
        return 1;
    }
    struct tpacket_req3 req = {
        .tp_block_size = BLOCK_SIZE,
        .tp_block_nr = BLOCK_COUNT,
        .tp_frame_size = FRAME_SIZE,
        .tp_frame_nr = BLOCK_SIZE / FRAME_SIZE * BLOCK_COUNT,
        .tp_retire_blk_tov = BLOCK_TIMEOUT,
    };
    if (setsockopt(fd, SOL_PACKET, PACKET_RX_RING, &req, sizeof req) < 0) {
        perror("PACKET_RX_RING");
        return 1;
    }
    uint8_t *ring = mmap(NULL, (size_t)BLOCK_SIZE * BLOCK_COUNT, PROT_READ | PROT_WRITE,
                         MAP_SHARED | MAP_LOCKED, fd, 0);
    if (ring == MAP_FAILED)
        ring = mmap(NULL, (size_t)BLOCK_SIZE * BLOCK_COUNT, PROT_READ | PROT_WRITE,
                    MAP_SHARED, fd, 0);
    if (ring == MAP_FAILED) {
        perror("mmap");
        return 1;
    }

    // Stop when BatoWatch does, even if it dies without stopping us.
    prctl(PR_SET_PDEATHSIG, SIGTERM);
    if (getppid() == 1)
        return 0;

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, on_signal);

    uint32_t lo = if_nametoindex("lo");
    uint64_t packets = 0, drops = 0;
    struct timespec t0, tnow;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    double next_report = interval;     // seconds since start (monotonic)
    unsigned block = 0;

    while (!stop) {
        struct tpacket_block_desc *bd =
            (struct tpacket_block_desc *)(ring + (size_t)block * BLOCK_SIZE);
        if (!(bd->hdr.bh1.block_status & TP_STATUS_USER)) {
            struct pollfd pfd = { .fd = fd, .events = POLLIN | POLLERR };
            poll(&pfd, 1, 250);
        } else {
            time_t now = time(NULL);
            struct tpacket3_hdr *p =
                (struct tpacket3_hdr *)((uint8_t *)bd + bd->hdr.bh1.offset_to_first_pkt);
            for (uint32_t i = 0; i < bd->hdr.bh1.num_pkts; i++) {
                const struct sockaddr_ll *sll =
                    (const struct sockaddr_ll *)((uint8_t *)p + TPACKET_ALIGN(sizeof *p));
                if ((uint32_t)sll->sll_ifindex != lo) {
                    packets++;
                    count((uint8_t *)p + p->tp_net, p->tp_snaplen, ntohs(sll->sll_protocol),
                          (uint32_t)sll->sll_ifindex, sll->sll_pkttype, p->tp_len, now);
                }
                p = (struct tpacket3_hdr *)((uint8_t *)p + p->tp_next_offset);
            }
            __atomic_store_n(&bd->hdr.bh1.block_status, TP_STATUS_KERNEL, __ATOMIC_RELEASE);
            block = (block + 1) % BLOCK_COUNT;
        }

        clock_gettime(CLOCK_MONOTONIC, &tnow);
        double wall = (double)(tnow.tv_sec - t0.tv_sec) + (tnow.tv_nsec - t0.tv_nsec) / 1e9;
        struct tpacket_stats_v3 st;
        socklen_t sl = sizeof st;

        if (test) {
            if (wall >= test) {
                if (getsockopt(fd, SOL_PACKET, PACKET_STATISTICS, &st, &sl) == 0)
                    drops += st.tp_drops;
                report_test(wall, packets, drops);
                break;
            }
            continue;
        }
        if (wall >= next_report) {
            time_t now = time(NULL);
            if (getsockopt(fd, SOL_PACKET, PACKET_STATISTICS, &st, &sl) == 0)
                drops += st.tp_drops;      // reading resets the kernel's counters
            expire(now);
            report_json(now, packets, drops);
            next_report += interval;
            if (next_report < wall)        // fell behind (suspend): don't burst
                next_report = wall + interval;
        }
    }
    munmap(ring, (size_t)BLOCK_SIZE * BLOCK_COUNT);
    close(fd);
    return 0;
}
