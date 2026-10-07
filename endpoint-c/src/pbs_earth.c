/*
 * pbs_earth.c - Endpoint B (Earth, Mission Control) of the PBS ION demo.
 *
 * Binds to an ION endpoint (default ipn:23.1) through ION's bp API, sends
 * envelopes from a JSON-lines plan with bp_send(), receives bundles with
 * bp_receive(), runs the PBS receive path on every payload, and writes the
 * JSON-lines log of docs/LOG-SCHEMA.md. Usage: pbs_earth --help, and
 * endpoint-c/README.md.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#include "earth.h"
#include "json.h"
#include "pbs.h"

#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <openssl/evp.h>

#include "bp.h"

/* The installed ION headers default to SPACE_ORDER 2; the Makefile passes
 * the value ION was built with. A mismatch changes ION's memory layout. */
_Static_assert(WORD_SIZE == sizeof(void *), "SPACE_ORDER does not match this machine");

#define MAX_MAP    16
#define MAX_COUNTS 64

typedef struct {
    char source_id[PBS_SOURCE_ID_LEN + 1];
    char eid[EARTH_EID_MAX];
} map_entry_t;

typedef struct {
    char key[64];
    long n;
} counter_t;

static struct {
    /* configuration */
    const char  *mode;
    const char  *eid;
    const char  *dest;
    const char  *source_id;
    const char  *plan_path;
    const char  *log_path;
    const char  *state_path;
    const char  *scenario;
    double       duration_s, linger_s, idle_s;
    long         expect;
    int64_t      t0_us;
    map_entry_t  map[MAX_MAP];
    size_t       nmap;
    int          argc;
    char       **argv;
    /* state */
    earth_logctx_t lc;
    BpSAP        sap;
    Sdr          sdr;
    FILE        *log;
    pthread_mutex_t log_mx;
    pthread_mutex_t sdr_mx;
    volatile sig_atomic_t stop;
    int          receiving;
    pbs_policy_t pol;
    pbs_replay_t *rep;
    earth_idset_t ids;
    /* under log_mx */
    counter_t    counts[MAX_COUNTS];
    size_t       ncounts;
    long         received;
    int64_t      last_rx_us;
} G;

static void usage(FILE *f)
{
    fputs(
        "usage: pbs_earth [options] [run|recv|send]\n"
        "\n"
        "  run   receive, and execute the plan if --plan is given (default)\n"
        "  recv  receive only\n"
        "  send  execute the plan only; opens the endpoint with bp_open_source(),\n"
        "        so a separate receiving process may hold it\n"
        "\n"
        "options:\n"
        "  --eid EID          own endpoint (default ipn:23.1)\n"
        "  --dest EID         default destination (default ipn:99.1)\n"
        "  --source-id ID     PBS Source ID (default Earth-MCC)\n"
        "  --plan FILE        JSON-lines plan of envelopes to send\n"
        "  --log FILE         JSON-lines log (docs/LOG-SCHEMA.md), appended\n"
        "                     (default standard output)\n"
        "  --scenario LABEL   value of the \"scenario\" field of every record\n"
        "  --state FILE       keep the Sequence and security_sequence counters in FILE,\n"
        "                     so that several invocations continue one counter\n"
        "  --t0-us US         plan time origin, Unix epoch microseconds (default: start)\n"
        "  --duration S       stop S seconds after t0\n"
        "  --linger S         after the plan, keep receiving S seconds\n"
        "  --idle S           after the plan, stop when nothing arrived for S seconds\n"
        "  --expect N         stop after N received bundle payloads\n"
        "                     (run/recv without any limit: until SIGINT or SIGTERM)\n"
        "  --map SRC=EID      Source ID to EID entry for the bundle source check\n"
        "                     (default Rover-Alpha=ipn:99.1 and Earth-MCC=ipn:23.1)\n"
        "  --node-dir DIR     ION node working directory (sets ION_NODE_WDNAME)\n"
        "  --log-hex-max N    log envelope_hex for envelopes of at most N bytes\n"
        "                     (default 4096; 65580 logs every envelope)\n"
        "\n"
        "ION_NODE_LIST_DIR must be set as for every other process of the node.\n"
        "Keys are the demo TEST KEYS derived from public labels; NOT FOR OPERATIONAL USE.\n",
        f);
}

static void handle_signal(int sig)
{
    (void)sig;
    G.stop = 1;
    if (G.receiving && G.sap)
        bp_interrupt(G.sap);
}

static void count_locked(const char *key)
{
    for (size_t i = 0; i < G.ncounts; i++)
        if (strcmp(G.counts[i].key, key) == 0) {
            G.counts[i].n++;
            return;
        }
    if (G.ncounts < MAX_COUNTS) {
        snprintf(G.counts[G.ncounts].key, sizeof G.counts[0].key, "%s", key);
        G.counts[G.ncounts++].n = 1;
    }
}

/* Write one record and update the run_end counters. */
static void emit(const char *line, const char *count1, const char *count2)
{
    pthread_mutex_lock(&G.log_mx);
    fputs(line, G.log);
    fputc('\n', G.log);
    fflush(G.log);
    if (count1)
        count_locked(count1);
    if (count2)
        count_locked(count2);
    pthread_mutex_unlock(&G.log_mx);
}

static void report_error(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

static void report_error(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("pbs_earth: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    pthread_mutex_lock(&G.log_mx);
    count_locked("x_error");
    pthread_mutex_unlock(&G.log_mx);
}

static const char *map_lookup(const uint8_t sid[PBS_SOURCE_ID_LEN])
{
    uint8_t raw[PBS_SOURCE_ID_LEN];
    for (size_t i = 0; i < G.nmap; i++)
        if (pbs_source_id_encode(G.map[i].source_id, raw) == PBS_E_OK &&
            memcmp(raw, sid, PBS_SOURCE_ID_LEN) == 0)
            return G.map[i].eid;
    return NULL;
}

static int add_map(const char *spec)
{
    const char *eq = strchr(spec, '=');
    if (!eq || G.nmap >= MAX_MAP || (size_t)(eq - spec) > PBS_SOURCE_ID_LEN ||
        strlen(eq + 1) >= EARTH_EID_MAX)
        return -1;
    map_entry_t *m = &G.map[G.nmap++];
    memcpy(m->source_id, spec, (size_t)(eq - spec));
    m->source_id[eq - spec] = '\0';
    snprintf(m->eid, sizeof m->eid, "%s", eq + 1);
    return 0;
}

/* Sleep until Unix time t_us or until stop; returns 0 when reached. */
static int sleep_until(int64_t t_us)
{
    while (!G.stop) {
        int64_t now = earth_now_us();
        if (now >= t_us)
            return 0;
        int64_t d = t_us - now;
        if (d > 50000)
            d = 50000;
        struct timespec ts = {(time_t)(d / 1000000), (long)(d % 1000000) * 1000};
        nanosleep(&ts, NULL);
    }
    return -1;
}

/* ---- Receive ------------------------------------------------------------------- */

/* Copy up to cap bytes of the ZCO into buf and hash all of it. */
static int read_zco(SdrObject adu, uint8_t *buf, size_t cap, size_t *avail, size_t *total,
                    uint8_t sha[32])
{
    int rc = -1;
    EVP_MD_CTX *md = EVP_MD_CTX_new();
    char chunk[65536];
    pthread_mutex_lock(&G.sdr_mx);
    if (!md || EVP_DigestInit_ex(md, EVP_sha256(), NULL) != 1 || sdr_begin_xn(G.sdr) < 0)
        goto out;
    vast len = zco_source_data_length(G.sdr, adu);
    ZcoReader reader;
    zco_start_receiving(adu, &reader);
    size_t got = 0, want = len < 0 ? 0 : (size_t)len;
    *avail = want < cap ? want : cap;
    *total = want;
    int ok = 1;
    while (ok && got < want) {
        size_t n = want - got;
        if (n > sizeof chunk)
            n = sizeof chunk;
        vast r = zco_receive_source(G.sdr, &reader, (vast)n, chunk);
        if (r <= 0) {
            ok = 0;
            break;
        }
        if (got < *avail) {
            size_t c = (size_t)r;
            if (got + c > *avail)
                c = *avail - got;
            memcpy(buf + got, chunk, c);
        }
        EVP_DigestUpdate(md, chunk, (size_t)r);
        got += (size_t)r;
    }
    if (sdr_end_xn(G.sdr) < 0)
        ok = 0;
    unsigned int ml = 0;
    if (ok && EVP_DigestFinal_ex(md, sha, &ml) == 1 && ml == 32)
        rc = 0;
out:
    pthread_mutex_unlock(&G.sdr_mx);
    EVP_MD_CTX_free(md);
    return rc;
}

static void process_delivery(BpDelivery *dlv, int64_t recv_us, uint8_t *buf, size_t cap,
                             jw_t *w)
{
    size_t avail = 0, total = 0;
    uint8_t sha[32];
    if (read_zco(dlv->adu, buf, cap, &avail, &total, sha) < 0) {
        report_error("cannot read a delivered payload from ION (ZCO)");
        return;
    }
    pbs_rx_t rx;
    pbs_receive_ex(&G.pol, G.rep, buf, avail, total, recv_us, &rx);

    earth_bundle_meta_t meta;
    memset(&meta, 0, sizeof meta);
    meta.have_bundle = 1;
    meta.bundle_source_eid = dlv->bundleSourceEid;
    meta.creation_ms = (uint64_t)dlv->bundleCreationTime.msec;
    meta.creation_count = dlv->bundleCreationTime.count;
    meta.ttl_field_ms = dlv->timeToLive;
    meta.admin_record = dlv->adminRecord != 0;
    if (rx.have_header) {
        meta.expected_eid = map_lookup(rx.hdr.source_id);
        if (rx.reason != PBS_R_MAGIC && rx.reason != PBS_R_CRC) {
            meta.have_duplicates = 1;
            meta.duplicates_before = earth_idset_add(&G.ids, &rx.hdr);
        }
    }
    earth_log_recv(w, &G.lc, &rx, buf, avail, sha, recv_us, &meta);
    char key[64];
    snprintf(key, sizeof key, "recv:%s:%s", rx.accepted ? "ACCEPT" : "REJECT",
             pbs_reason_str(rx.reason));
    emit(jw_cstr(w), key, NULL);

    /* An accepted command envelope: one exec record per Command frame. */
    if (rx.accepted && rx.n_commands)
        for (unsigned i = 0; i < rx.mux.count; i++)
            if (rx.mux.frames[i].type == PBS_FRAME_COMMAND) {
                earth_log_exec(w, &G.lc, &rx, buf, i, earth_now_us());
                emit(jw_cstr(w), "exec", NULL);
            }

    pthread_mutex_lock(&G.log_mx);
    G.received++;
    G.last_rx_us = recv_us;
    pthread_mutex_unlock(&G.log_mx);
}

static void *receiver(void *arg)
{
    (void)arg;
    size_t cap = PBS_MAX_ENVELOPE + 1;
    uint8_t *buf = malloc(cap);
    jw_t w;
    jw_init(&w);
    if (!buf) {
        report_error("out of memory");
        G.stop = 1;
        return NULL;
    }
    while (!G.stop) {
        BpDelivery dlv;
        memset(&dlv, 0, sizeof dlv);
        if (bp_receive(G.sap, &dlv, BP_BLOCKING) < 0) {
            report_error("bp_receive failed");
            G.stop = 1;
            break;
        }
        int64_t recv_us = earth_now_us();
        if (dlv.result == BpPayloadPresent && dlv.adu)
            process_delivery(&dlv, recv_us, buf, cap, &w);
        int stopped = dlv.result == BpEndpointStopped;
        bp_release_delivery(&dlv, 1);
        if (stopped) {
            report_error("endpoint %s stopped by ION", G.eid);
            G.stop = 1;
            break;
        }
        pthread_mutex_lock(&G.log_mx);
        long r = G.received;
        pthread_mutex_unlock(&G.log_mx);
        if (G.expect > 0 && r >= G.expect)
            G.stop = 1;
    }
    jw_free(&w);
    free(buf);
    return NULL;
}

/* ---- Send ---------------------------------------------------------------------- */

/* Hands it->env to ION. Returns the bp_send() return value, or 0 with
 * *fail = "ZCO_FAILED" when ION could not store the payload (bp_send() not
 * called: *called = 0). */
static int send_item(earth_item_t *it, int64_t *t_us, int *called, int64_t *t_return,
                     const char **fail)
{
    *fail = NULL;
    *called = 0;
    *t_return = 0;
    pthread_mutex_lock(&G.sdr_mx);
    if (sdr_begin_xn(G.sdr) < 0) {
        pthread_mutex_unlock(&G.sdr_mx);
        *fail = "ZCO_FAILED";
        *t_us = earth_now_us();
        return 0;
    }
    SdrObject extent = sdr_malloc(G.sdr, it->len ? it->len : 1);
    if (extent)
        sdr_write(G.sdr, extent, (char *)it->env, it->len);
    if (sdr_end_xn(G.sdr) < 0 || !extent) {
        pthread_mutex_unlock(&G.sdr_mx);
        *fail = "ZCO_FAILED";
        *t_us = earth_now_us();
        return 0;
    }
    /* Non-blocking ZCO creation (attendant NULL): fails instead of waiting
     * when ZCO space is exhausted. */
    SdrObject zco = ionCreateZco(ZcoSdrSource, extent, 0, (vast)it->len,
                                 (unsigned char)it->class_of_service, 0, ZcoOutbound, NULL);
    pthread_mutex_unlock(&G.sdr_mx);
    if (zco == 0 || zco == (SdrObject)ERROR) {
        pthread_mutex_lock(&G.sdr_mx);
        if (sdr_begin_xn(G.sdr) >= 0) {
            sdr_free(G.sdr, extent);
            sdr_end_xn(G.sdr);
        }
        pthread_mutex_unlock(&G.sdr_mx);
        *fail = "ZCO_FAILED";
        *t_us = earth_now_us();
        return 0;
    }
    SdrObject newBundle = 0;
    *called = 1;
    *t_us = earth_now_us();
    int rc = bp_send(G.sap, it->dest, NULL, (int)it->lifespan_s, it->class_of_service,
                     NoCustodyRequested, 0, 0, NULL, zco, &newBundle);
    *t_return = earth_now_us();
    if (rc < 1) {
        pthread_mutex_lock(&G.sdr_mx);
        if (sdr_begin_xn(G.sdr) >= 0) {
            zco_destroy(G.sdr, zco);
            sdr_end_xn(G.sdr);
        }
        pthread_mutex_unlock(&G.sdr_mx);
        *fail = "BP_SEND_FAILED";
    }
    return rc;
}

static void log_run_start(const earth_ctx_t *ctx, size_t nentries)
{
    jw_t w;
    jw_init(&w);
    earth_log_begin(&w, &G.lc, "run_start", earth_now_us());
    jw_kstr(&w, "impl", EARTH_IMPL);
    jw_knull(&w, "pbs_link_version");
    jw_kstr(&w, "ion_version", getIonVersionNbr());
    jw_kstr(&w, "source_id", G.source_id);
    jw_kstr(&w, "dest_eid", G.dest);
    jw_kint(&w, "pid", (int64_t)getpid());
    jw_key(&w, "argv");
    jw_arr_begin(&w);
    for (int i = 0; i < G.argc; i++)
        jw_str(&w, G.argv[i]);
    jw_arr_end(&w);
    jw_key(&w, "keys");
    jw_arr_begin(&w);
    for (size_t i = 0; i < ctx->nkeys; i++)
        jw_str(&w, ctx->keys[i].key_id);
    jw_arr_end(&w);
    jw_kstr(&w, "x_keys_note", "PBS-ION-DEMO TEST KEYS derived from public labels; NOT FOR "
                               "OPERATIONAL USE");
    jw_kstr(&w, "x_mode", G.mode);
    jw_key(&w, "x_source_id_eid_map");
    jw_obj_begin(&w);
    for (size_t i = 0; i < G.nmap; i++)
        jw_kstr(&w, G.map[i].source_id, G.map[i].eid);
    jw_obj_end(&w);
    jw_kstr(&w, "x_plan", G.plan_path);
    jw_kuint(&w, "x_plan_items", nentries);
    jw_kstr(&w, "x_state", G.state_path);
    jw_kint(&w, "x_t0_us", G.t0_us);
    jw_knum(&w, "x_duration_s", G.duration_s);
    jw_knum(&w, "x_linger_s", G.linger_s);
    jw_knum(&w, "x_idle_s", G.idle_s);
    jw_kuint(&w, "x_log_hex_max", earth_log_hex_max());
    jw_kint(&w, "x_expect", G.expect);
    jw_kstr(&w, "x_ion_node_wdname", getenv("ION_NODE_WDNAME"));
    jw_kstr(&w, "x_ion_node_list_dir", getenv("ION_NODE_LIST_DIR"));
    jw_obj_end(&w);
    emit(jw_cstr(&w), NULL, NULL);
    jw_free(&w);
}

static int cmp_counter(const void *a, const void *b)
{
    return strcmp(((const counter_t *)a)->key, ((const counter_t *)b)->key);
}

static void log_run_end(size_t nentries, size_t done)
{
    jw_t w;
    jw_init(&w);
    earth_log_begin(&w, &G.lc, "run_end", earth_now_us());
    pthread_mutex_lock(&G.log_mx);
    qsort(G.counts, G.ncounts, sizeof G.counts[0], cmp_counter);
    jw_key(&w, "counts");
    jw_obj_begin(&w);
    for (size_t i = 0; i < G.ncounts; i++)
        jw_kint(&w, G.counts[i].key, G.counts[i].n);
    jw_obj_end(&w);
    pthread_mutex_unlock(&G.log_mx);
    jw_kuint(&w, "x_plan_items", nentries);
    jw_kuint(&w, "x_plan_items_done", done);
    jw_obj_end(&w);
    pthread_mutex_lock(&G.log_mx);
    fputs(jw_cstr(&w), G.log);
    fputc('\n', G.log);
    fflush(G.log);
    pthread_mutex_unlock(&G.log_mx);
    jw_free(&w);
}

static double parse_seconds(const char *s, const char *opt)
{
    char *end;
    double d = strtod(s, &end);
    if (*end || d < 0) {
        fprintf(stderr, "pbs_earth: bad value for %s\n", opt);
        exit(2);
    }
    return d;
}

int main(int argc, char **argv)
{
    memset(&G, 0, sizeof G);
    G.mode = "run";
    G.eid = "ipn:23.1";
    G.dest = "ipn:99.1";
    G.source_id = "Earth-MCC";
    G.t0_us = -1;
    G.argc = argc;
    G.argv = argv;
    int user_map = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        int has = i + 1 < argc;
        if (!strcmp(a, "-h") || !strcmp(a, "--help")) {
            usage(stdout);
            return 0;
        } else if (!strcmp(a, "run") || !strcmp(a, "recv") || !strcmp(a, "send"))
            G.mode = a;
        else if (!strcmp(a, "--eid") && has)
            G.eid = argv[++i];
        else if (!strcmp(a, "--dest") && has)
            G.dest = argv[++i];
        else if (!strcmp(a, "--source-id") && has)
            G.source_id = argv[++i];
        else if (!strcmp(a, "--plan") && has)
            G.plan_path = argv[++i];
        else if (!strcmp(a, "--log") && has)
            G.log_path = argv[++i];
        else if (!strcmp(a, "--state") && has)
            G.state_path = argv[++i];
        else if (!strcmp(a, "--scenario") && has)
            G.scenario = argv[++i];
        else if (!strcmp(a, "--t0-us") && has)
            G.t0_us = strtoll(argv[++i], NULL, 10);
        else if (!strcmp(a, "--duration") && has)
            G.duration_s = parse_seconds(argv[++i], a);
        else if (!strcmp(a, "--linger") && has)
            G.linger_s = parse_seconds(argv[++i], a);
        else if (!strcmp(a, "--idle") && has)
            G.idle_s = parse_seconds(argv[++i], a);
        else if (!strcmp(a, "--expect") && has)
            G.expect = strtol(argv[++i], NULL, 10);
        else if (!strcmp(a, "--map") && has) {
            if (!user_map) {
                G.nmap = 0;
                user_map = 1;
            }
            if (add_map(argv[++i]) < 0) {
                fprintf(stderr, "pbs_earth: bad --map value\n");
                return 2;
            }
        } else if (!strcmp(a, "--node-dir") && has)
            setenv("ION_NODE_WDNAME", argv[++i], 1);
        else if (!strcmp(a, "--log-hex-max") && has) {
            char *end;
            unsigned long long n = strtoull(argv[++i], &end, 10);
            if (*end || n > PBS_MAX_ENVELOPE + 1u) {
                fprintf(stderr, "pbs_earth: bad --log-hex-max value\n");
                return 2;
            }
            earth_log_set_hex_max((size_t)n);
        }
        else {
            usage(stderr);
            return 2;
        }
    }
    if (!user_map) {
        add_map("Rover-Alpha=ipn:99.1");
        add_map("Earth-MCC=ipn:23.1");
    }
    int do_recv = strcmp(G.mode, "send") != 0;
    int do_send = strcmp(G.mode, "recv") != 0;
    if (!strcmp(G.mode, "send") && !G.plan_path) {
        fprintf(stderr, "pbs_earth: send needs --plan\n");
        return 2;
    }
    G.lc.endpoint = "B";
    G.lc.node_eid = G.eid;
    G.lc.scenario = G.scenario;

    earth_ctx_t ctx;
    if (earth_ctx_init(&ctx, G.source_id, G.dest) != 0) {
        fprintf(stderr, "pbs_earth: Source ID longer than 16 bytes or key setup failed\n");
        return 2;
    }
    if (G.state_path) {
        char serr[256];
        if (earth_state_load(&ctx, G.state_path, serr, sizeof serr) < 0) {
            fprintf(stderr, "pbs_earth: %s\n", serr);
            return 2;
        }
    }
    if (pbs_policy_demo(&G.pol) != PBS_E_OK) {
        fprintf(stderr, "pbs_earth: policy setup failed\n");
        return 2;
    }
    G.rep = malloc(sizeof *G.rep);
    if (!G.rep)
        return 2;
    pbs_replay_init(G.rep);
    earth_idset_init(&G.ids);

    earth_plan_t plan;
    memset(&plan, 0, sizeof plan);
    if (do_send && G.plan_path) {
        char perr[256];
        FILE *pf = fopen(G.plan_path, "r");
        if (!pf || earth_plan_load(pf, &plan, perr, sizeof perr) < 0) {
            fprintf(stderr, "pbs_earth: %s: %s\n", G.plan_path, pf ? perr : strerror(errno));
            if (pf)
                fclose(pf);
            return 2;
        }
        fclose(pf);
    }

    G.log = stdout;
    if (G.log_path) {
        G.log = fopen(G.log_path, "a");
        if (!G.log) {
            fprintf(stderr, "pbs_earth: %s: %s\n", G.log_path, strerror(errno));
            return 2;
        }
    }
    pthread_mutex_init(&G.log_mx, NULL);
    pthread_mutex_init(&G.sdr_mx, NULL);

    if (bp_attach() < 0) {
        fprintf(stderr, "pbs_earth: cannot attach to ION (is the node running, and are "
                        "ION_NODE_LIST_DIR and ION_NODE_WDNAME set?)\n");
        return 1;
    }
    G.sdr = bp_get_sdr();
    int orc = do_recv ? bp_open((char *)G.eid, &G.sap) : bp_open_source((char *)G.eid, &G.sap, 0);
    if (orc < 0) {
        fprintf(stderr, "pbs_earth: cannot open endpoint %s\n", G.eid);
        bp_detach();
        return 1;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = handle_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    if (G.t0_us < 0)
        G.t0_us = earth_now_us();
    log_run_start(&ctx, plan.nentries);

    pthread_t rx_thread;
    if (do_recv) {
        G.receiving = 1;
        if (pthread_create(&rx_thread, NULL, receiver, NULL) != 0) {
            report_error("cannot start the receiver thread");
            G.receiving = 0;
            G.stop = 1;
        }
    }

    int64_t end_us = G.duration_s > 0 ? G.t0_us + (int64_t)(G.duration_s * 1e6) : INT64_MAX;
    earth_item_t it;
    if (earth_item_alloc(&it) < 0)
        G.stop = 1;
    jw_t w;
    jw_init(&w);
    size_t done = 0;
    for (size_t i = 0; i < plan.nentries && !G.stop; i++) {
        const earth_plan_entry_t *e = &plan.entries[i];
        int64_t at = G.t0_us + (int64_t)(e->at_s * 1e6);
        if (at >= end_us)
            break;
        if (sleep_until(at) < 0)
            break;
        char err[200];
        int64_t now = earth_now_us();
        int brc = earth_build(&ctx, e->item, now, &it, err, sizeof err);
        if (G.state_path && earth_state_save(&ctx, G.state_path) < 0)
            report_error("cannot write %s", G.state_path);
        done++;
        if (brc < 0) {
            report_error("%s line %ld: %s", G.plan_path, e->line, err);
            continue;
        }
        int64_t t_us = now, t_return = 0;
        int called = 0, rc = 0;
        const char *fail = NULL;
        if (it.send)
            rc = send_item(&it, &t_us, &called, &t_return, &fail);
        earth_log_send(&w, &G.lc, &it, t_us, called, rc, t_return, fail);
        int sent = called && rc == 1;
        char key2[64];
        if (!sent)
            snprintf(key2, sizeof key2, "send:NOT_SENT:%s", it.send ? fail : it.drop_reason);
        emit(jw_cstr(&w), sent ? "send:SENT" : "send:NOT_SENT", sent ? NULL : key2);
    }

    /* After the plan: wait for the stop condition. */
    if (do_recv) {
        int64_t plan_end = earth_now_us();
        int64_t linger_end = G.linger_s > 0 ? plan_end + (int64_t)(G.linger_s * 1e6) : INT64_MAX;
        while (!G.stop) {
            int64_t now = earth_now_us();
            if (now >= end_us || now >= linger_end)
                break;
            if (G.idle_s > 0) {
                pthread_mutex_lock(&G.log_mx);
                int64_t last = G.last_rx_us > plan_end ? G.last_rx_us : plan_end;
                pthread_mutex_unlock(&G.log_mx);
                if (now - last >= (int64_t)(G.idle_s * 1e6))
                    break;
            }
            struct timespec ts = {0, 50000000};
            nanosleep(&ts, NULL);
        }
        G.stop = 1;
        if (G.receiving) {
            bp_interrupt(G.sap);
            pthread_join(rx_thread, NULL);
            G.receiving = 0;
        }
    }

    log_run_end(plan.nentries, done);
    jw_free(&w);
    earth_item_free(&it);
    bp_close(G.sap);
    bp_detach();
    earth_ctx_free(&ctx);
    earth_idset_free(&G.ids);
    earth_plan_free(&plan);
    free(G.rep);
    if (G.log != stdout)
        fclose(G.log);
    return 0;
}
