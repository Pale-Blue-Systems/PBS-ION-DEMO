/*
 * pbs_tool.c - offline command-line access to the Endpoint B library
 * (no ION). Used for the S7 cross-implementation checks.
 *
 *   pbs_tool build [--vectors FILE]
 *   pbs_tool receive [--now-us N] [--fresh]
 *   pbs_tool plan [--now-us N] [--source-id ID] [--dest EID] < plan.jsonl
 *   pbs_tool lifetime TTL TIMESTAMP_US CLOCK_US
 *   pbs_tool crosscheck PEER_LOG [--own-log OWN_LOG] [--out FILE]
 *
 * Input and output are JSON lines; see endpoint-c/README.md.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#include "earth.h"
#include "json.h"
#include "pbs.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const earth_logctx_t TOOL_LC = {"B", NULL, NULL};

static void usage(void)
{
    fputs("usage:\n"
          "  pbs_tool build [--vectors FILE]       build envelopes from JSON-line inputs\n"
          "  pbs_tool receive [--now-us N] [--fresh]\n"
          "                                        run the receive path on hex lines\n"
          "  pbs_tool plan [--now-us N] [--source-id ID] [--dest EID]\n"
          "                                        print what pbs_earth would send\n"
          "  pbs_tool lifetime TTL TIMESTAMP_US CLOCK_US\n"
          "  pbs_tool crosscheck PEER_LOG [--own-log OWN_LOG] [--out FILE]\n"
          "                                        decode the other endpoint's logged\n"
          "                                        envelopes; compare delivered bytes.\n"
          "                                        Exit 0 only when every peer send\n"
          "                                        record was decoded with no mismatch\n"
          "                                        and, with --own-log, every own\n"
          "                                        envelope was delivered byte-identical\n",
          stderr);
}

/* Reads one line of any length; caller frees. NULL at EOF. */
static char *read_line(FILE *f)
{
    size_t cap = 0;
    char *line = NULL;
    ssize_t n = getline(&line, &cap, f);
    if (n < 0) {
        free(line);
        return NULL;
    }
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
        line[--n] = '\0';
    return line;
}

static int blank_or_comment(const char *s)
{
    while (*s == ' ' || *s == '\t')
        s++;
    return *s == '\0' || *s == '#';
}

static void print_with_id(const char *id, const char *record)
{
    if (!id) {
        printf("%s\n", record);
        return;
    }
    jw_t w;
    jw_init(&w);
    jw_str(&w, id);
    printf("{\"id\":%s,%s\n", jw_cstr(&w), record + 1);
    jw_free(&w);
}

static void print_built(const char *id, const uint8_t *b, size_t n, const char *vec_hex)
{
    jw_t w;
    jw_init(&w);
    jw_obj_begin(&w);
    jw_kstr(&w, "id", id);
    jw_khex(&w, "envelope_hex", b, n);
    if (vec_hex) {
        char *h = malloc(2 * n + 1);
        pbs_hex_encode(b, n, h);
        jw_kbool(&w, "matches_vector", strcmp(h, vec_hex) == 0);
        free(h);
    }
    jw_obj_end(&w);
    printf("%s\n", jw_cstr(&w));
    jw_free(&w);
}

static int cmd_build(int argc, char **argv, const earth_ctx_t *ctx)
{
    const char *vectors = NULL;
    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], "--vectors") == 0 && i + 1 < argc)
            vectors = argv[++i];
        else {
            usage();
            return 2;
        }
    }
    uint8_t *out = malloc(PBS_MAX_ENVELOPE);
    char err[200];
    size_t len;
    int failures = 0;
    if (vectors) {
        size_t tl;
        char *text = json_read_file(vectors, &tl);
        json_value_t *root = text ? json_parse(text, tl, err, sizeof err) : NULL;
        free(text);
        if (!root) {
            fprintf(stderr, "%s: cannot read\n", vectors);
            free(out);
            return 2;
        }
        const json_value_t *lists[2] = {json_get(root, "envelopes"),
                                        json_get(json_get(root, "sec_b"), "messages")};
        for (int l = 0; l < 2; l++) {
            for (size_t i = 0; lists[l] && i < lists[l]->count; i++) {
                const json_value_t *e = lists[l]->items[i];
                const json_value_t *in = json_get(e, "inputs");
                if (!in)
                    continue;           /* derived (tampered) vectors */
                const json_value_t *sec = json_get(e, "security");
                const char *id = json_get_str(e, "id");
                if (earth_build_from_inputs(ctx, in, sec, out, PBS_MAX_ENVELOPE, &len, err,
                                            sizeof err) != 0) {
                    fprintf(stderr, "%s: %s\n", id, err);
                    failures++;
                    continue;
                }
                char *h = malloc(2 * len + 1);
                pbs_hex_encode(out, len, h);
                if (strcmp(h, json_get_str(e, "envelope_hex")) != 0)
                    failures++;
                free(h);
                print_built(id, out, len, json_get_str(e, "envelope_hex"));
            }
        }
        json_free(root);
        free(out);
        return failures ? 1 : 0;
    }
    char *line;
    long lineno = 0;
    while ((line = read_line(stdin)) != NULL) {
        lineno++;
        if (blank_or_comment(line)) {
            free(line);
            continue;
        }
        json_value_t *v = json_parse(line, strlen(line), err, sizeof err);
        free(line);
        if (!v) {
            fprintf(stderr, "line %ld: %s\n", lineno, err);
            failures++;
            continue;
        }
        const json_value_t *in = json_get(v, "inputs");
        if (!in)
            in = v;
        const char *id = json_get_str(v, "id");
        char idbuf[32];
        if (!id) {
            snprintf(idbuf, sizeof idbuf, "line-%ld", lineno);
            id = idbuf;
        }
        if (earth_build_from_inputs(ctx, in, json_get(v, "security"), out, PBS_MAX_ENVELOPE,
                                    &len, err, sizeof err) != 0) {
            jw_t w;
            jw_init(&w);
            jw_obj_begin(&w);
            jw_kstr(&w, "id", id);
            jw_kstr(&w, "error", err);
            jw_obj_end(&w);
            printf("%s\n", jw_cstr(&w));
            jw_free(&w);
            failures++;
        } else {
            print_built(id, out, len, NULL);
        }
        json_free(v);
    }
    free(out);
    return failures ? 1 : 0;
}

static int cmd_receive(int argc, char **argv)
{
    int64_t now_default = earth_now_us();
    int fresh = 0;
    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], "--now-us") == 0 && i + 1 < argc)
            now_default = strtoll(argv[++i], NULL, 10);
        else if (strcmp(argv[i], "--fresh") == 0)
            fresh = 1;
        else {
            usage();
            return 2;
        }
    }
    pbs_policy_t pol;
    if (pbs_policy_demo(&pol) != PBS_E_OK)
        return 2;
    pbs_replay_t *rep = malloc(sizeof *rep);
    uint8_t *buf = malloc(PBS_MAX_ENVELOPE + 1024);
    pbs_replay_init(rep);
    jw_t w;
    jw_init(&w);
    char *line, err[200];
    long lineno = 0;
    int bad = 0;
    while ((line = read_line(stdin)) != NULL) {
        lineno++;
        if (blank_or_comment(line)) {
            free(line);
            continue;
        }
        const char *hex = line, *id = NULL;
        int64_t now = now_default;
        json_value_t *v = NULL;
        if (line[0] == '{') {
            v = json_parse(line, strlen(line), err, sizeof err);
            if (!v) {
                fprintf(stderr, "line %ld: %s\n", lineno, err);
                free(line);
                bad++;
                continue;
            }
            hex = json_get_str(v, "hex");
            if (!hex)
                hex = json_get_str(v, "envelope_hex");
            if (!hex)
                hex = json_get_str(v, "input_hex");
            id = json_get_str(v, "id");
            json_get_int(v, "now_us", &now);
        }
        long n = hex ? pbs_hex_decode(hex, buf, PBS_MAX_ENVELOPE + 1024) : -1;
        if (n < 0) {
            fprintf(stderr, "line %ld: bad hex\n", lineno);
            bad++;
        } else {
            if (fresh)
                pbs_replay_init(rep);
            pbs_rx_t rx;
            pbs_receive(&pol, rep, buf, (size_t)n, now, &rx);
            earth_log_recv(&w, &TOOL_LC, &rx, buf, (size_t)n, NULL, now, NULL);
            print_with_id(id, jw_cstr(&w));
        }
        json_free(v);
        free(line);
    }
    jw_free(&w);
    free(buf);
    free(rep);
    return bad ? 1 : 0;
}

static int cmd_plan(int argc, char **argv)
{
    int64_t now = earth_now_us();
    const char *sid = "Earth-MCC", *dest = "ipn:99.1";
    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], "--now-us") == 0 && i + 1 < argc)
            now = strtoll(argv[++i], NULL, 10);
        else if (strcmp(argv[i], "--source-id") == 0 && i + 1 < argc)
            sid = argv[++i];
        else if (strcmp(argv[i], "--dest") == 0 && i + 1 < argc)
            dest = argv[++i];
        else {
            usage();
            return 2;
        }
    }
    earth_ctx_t ctx;
    if (earth_ctx_init(&ctx, sid, dest) != 0) {
        fprintf(stderr, "bad Source ID\n");
        return 2;
    }
    earth_plan_t plan;
    char err[200];
    if (earth_plan_load(stdin, &plan, err, sizeof err) < 0) {
        fprintf(stderr, "plan: %s\n", err);
        earth_ctx_free(&ctx);
        return 1;
    }
    earth_item_t it;
    earth_item_alloc(&it);
    jw_t w;
    jw_init(&w);
    int bad = 0;
    for (size_t i = 0; i < plan.nentries; i++) {
        const earth_plan_entry_t *e = &plan.entries[i];
        int64_t t = now + (int64_t)(e->at_s * 1e6);
        if (earth_build(&ctx, e->item, t, &it, err, sizeof err) != 0) {
            fprintf(stderr, "line %ld: %s\n", e->line, err);
            bad++;
        } else {
            earth_log_plan(&w, &TOOL_LC, &it, t);
            printf("%s\n", jw_cstr(&w));
        }
    }
    jw_free(&w);
    earth_item_free(&it);
    earth_plan_free(&plan);
    earth_ctx_free(&ctx);
    return bad ? 1 : 0;
}

static int cmd_lifetime(int argc, char **argv)
{
    if (argc != 3) {
        usage();
        return 2;
    }
    pbs_lifetime_t lt;
    uint32_t ttl = (uint32_t)strtoul(argv[0], NULL, 10);
    pbs_lifetime(ttl, strtoull(argv[1], NULL, 10), strtoll(argv[2], NULL, 10), &lt);
    jw_t w;
    jw_init(&w);
    jw_obj_begin(&w);
    jw_kstr(&w, "outcome", pbs_lt_outcome_str(lt.outcome));
    if (lt.has_bound) {
        jw_kint(&w, "c_us", lt.c_us);
        jw_kint(&w, "bound_ms", lt.bound_ms);
    }
    if (lt.outcome == PBS_LT_OK || lt.outcome == PBS_LT_NO_EXPIRY)
        jw_kint(&w, "lifespan_s", lt.lifespan_s);
    jw_obj_end(&w);
    printf("%s\n", jw_cstr(&w));
    jw_free(&w);
    return 0;
}

/* ---- crosscheck ------------------------------------------------------------------ */

typedef struct {
    json_value_t **v;
    size_t n;
} jsonl_t;

static int read_jsonl(const char *path, jsonl_t *out)
{
    FILE *f = fopen(path, "r");
    char *line, err[200];
    long lineno = 0;
    memset(out, 0, sizeof *out);
    if (!f) {
        perror(path);
        return -1;
    }
    while ((line = read_line(f)) != NULL) {
        lineno++;
        if (blank_or_comment(line)) {
            free(line);
            continue;
        }
        json_value_t *v = json_parse(line, strlen(line), err, sizeof err);
        free(line);
        if (!v) {
            fprintf(stderr, "%s:%ld: %s\n", path, lineno, err);
            continue;
        }
        json_value_t **nv = realloc(out->v, (out->n + 1) * sizeof *nv);
        if (!nv) {
            json_free(v);
            break;
        }
        out->v = nv;
        out->v[out->n++] = v;
    }
    fclose(f);
    return 0;
}

static void free_jsonl(jsonl_t *j)
{
    for (size_t i = 0; i < j->n; i++)
        json_free(j->v[i]);
    free(j->v);
}

static int is_event(const json_value_t *r, const char *ev)
{
    const char *e = json_get_str(r, "event");
    return e && strcmp(e, ev) == 0;
}

static int same_identity(const json_value_t *a, const json_value_t *b)
{
    return json_equal(json_get(a, "source_id_hex"), json_get(b, "source_id_hex")) &&
           json_equal(json_get(a, "sequence"), json_get(b, "sequence")) &&
           json_equal(json_get(a, "timestamp_us"), json_get(b, "timestamp_us"));
}

/* Compared fields: the Section 4 header fields, length, digest, frame types. */
static const char *const DECODE_KEYS[] = {"source_id",    "source_id_hex", "sequence",
                                          "priority",     "flags",         "timestamp_us",
                                          "ttl_s",        "size",          "crc32",
                                          "header_hex",   "envelope_len",  "envelope_sha256",
                                          "frame_types"};

static int cmd_crosscheck(int argc, char **argv)
{
    const char *peer_path = NULL, *own_path = NULL, *out_path = NULL;
    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], "--own-log") == 0 && i + 1 < argc)
            own_path = argv[++i];
        else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc)
            out_path = argv[++i];
        else if (!peer_path && argv[i][0] != '-')
            peer_path = argv[i];
        else {
            usage();
            return 2;
        }
    }
    if (!peer_path) {
        usage();
        return 2;
    }
    jsonl_t peer, own;
    memset(&own, 0, sizeof own);
    if (read_jsonl(peer_path, &peer) < 0 || (own_path && read_jsonl(own_path, &own) < 0))
        return 2;
    pbs_policy_t pol;
    pbs_policy_demo(&pol);
    pbs_replay_t *rep = malloc(sizeof *rep);
    uint8_t *buf = malloc(PBS_MAX_ENVELOPE + 1);
    char err[200];
    jw_t w, rep_w, mism;
    jw_init(&w);
    jw_init(&rep_w);
    jw_init(&mism);
    long peer_sends = 0, decoded = 0, no_hex = 0, mismatches = 0, rx_counts[PBS_R_COUNT_] = {0};
    long rx_accept = 0, rx_reject = 0;
    jw_arr_begin(&mism);
    for (size_t i = 0; i < peer.n; i++) {
        const json_value_t *r = peer.v[i];
        if (!is_event(r, "send"))
            continue;
        peer_sends++;
        const char *hex = json_get_str(r, "envelope_hex");
        long n = hex ? pbs_hex_decode(hex, buf, PBS_MAX_ENVELOPE + 1) : -1;
        if (n < 0) {
            no_hex++;
            continue;
        }
        decoded++;
        /* Endpoint B's decode, as JSON, compared field by field. */
        jw_reset(&w);
        jw_obj_begin(&w);
        earth_log_envelope_fields(&w, buf, (size_t)n);
        jw_obj_end(&w);
        json_value_t *mine = json_parse(jw_cstr(&w), w.len, err, sizeof err);
        int first = 1;
        for (size_t k = 0; mine && k < sizeof DECODE_KEYS / sizeof DECODE_KEYS[0]; k++) {
            const json_value_t *pv = json_get(r, DECODE_KEYS[k]);
            const json_value_t *mv = json_get(mine, DECODE_KEYS[k]);
            if (json_equal(pv, mv))
                continue;
            if (first) {
                mismatches++;
                first = 0;
            }
            jw_obj_begin(&mism);
            jw_kuint(&mism, "record", i + 1);
            jw_kstr(&mism, "field", DECODE_KEYS[k]);
            jw_obj_end(&mism);
        }
        json_free(mine);
        /* Endpoint B's receive path at the peer's send time, fresh state. */
        int64_t t = 0;
        json_get_int(r, "timestamp_us", &t);
        json_get_int(r, "t_us", &t);
        pbs_replay_init(rep);
        pbs_rx_t rx;
        pbs_receive(&pol, rep, buf, (size_t)n, t, &rx);
        rx_counts[rx.reason]++;
        if (rx.accepted)
            rx_accept++;
        else
            rx_reject++;
    }
    jw_arr_end(&mism);

    long own_sent = 0, delivered = 0, identical = 0, dup = 0;
    jw_t bm;
    jw_init(&bm);
    jw_arr_begin(&bm);
    for (size_t i = 0; own_path && i < own.n; i++) {
        const json_value_t *s = own.v[i];
        const char *res = json_get_str(s, "result");
        if (!is_event(s, "send") || !res || strcmp(res, "SENT") != 0)
            continue;
        own_sent++;
        const json_value_t *hit = NULL;
        long hits = 0;
        for (size_t j = 0; j < peer.n; j++)
            if (is_event(peer.v[j], "recv") && json_get_str(peer.v[j], "source_id_hex") &&
                same_identity(s, peer.v[j])) {
                if (!hit)
                    hit = peer.v[j];
                hits++;
            }
        if (!hit)
            continue;
        delivered++;
        dup += hits - 1;
        if (json_equal(json_get(hit, "envelope_sha256"), json_get(s, "envelope_sha256")) &&
            json_equal(json_get(hit, "header_hex"), json_get(s, "header_hex")))
            identical++;
        else {
            jw_obj_begin(&bm);
            jw_kuint(&bm, "own_record", i + 1);
            jw_obj_end(&bm);
        }
    }
    jw_arr_end(&bm);

    /* Every peer send record decoded (none without envelope_hex), and with
     * --own-log every envelope this endpoint sent found in the peer's recv
     * records, byte-identical. */
    int ok = mismatches == 0 && no_hex == 0 && decoded == peer_sends &&
             (!own_path || (identical == delivered && delivered == own_sent));
    jw_obj_begin(&rep_w);
    jw_kstr(&rep_w, "impl", EARTH_IMPL);
    jw_kstr(&rep_w, "peer_log", peer_path);
    jw_kint(&rep_w, "peer_send_records", peer_sends);
    jw_kint(&rep_w, "decoded", decoded);
    jw_kint(&rep_w, "no_hex", no_hex);
    jw_kint(&rep_w, "decode_mismatch_records", mismatches);
    jw_key(&rep_w, "decode_mismatch");
    jw_raw(&rep_w, jw_cstr(&mism));
    jw_kint(&rep_w, "b_receive_accept", rx_accept);
    jw_kint(&rep_w, "b_receive_reject", rx_reject);
    jw_key(&rep_w, "b_receive_reasons");
    jw_obj_begin(&rep_w);
    for (int k = 0; k < PBS_R_COUNT_; k++)
        if (rx_counts[k])
            jw_kint(&rep_w, pbs_reason_str((pbs_reason_t)k), rx_counts[k]);
    jw_obj_end(&rep_w);
    if (own_path) {
        jw_kstr(&rep_w, "own_log", own_path);
        jw_kint(&rep_w, "own_sent", own_sent);
        jw_kint(&rep_w, "delivered", delivered);
        jw_kint(&rep_w, "byte_identical", identical);
        jw_kint(&rep_w, "peer_duplicates", dup);
        jw_key(&rep_w, "byte_mismatch");
        jw_raw(&rep_w, jw_cstr(&bm));
    }
    jw_kbool(&rep_w, "ok", ok);
    jw_obj_end(&rep_w);
    printf("%s\n", jw_cstr(&rep_w));
    if (out_path) {
        FILE *out = fopen(out_path, "w");
        if (!out || fprintf(out, "%s\n", jw_cstr(&rep_w)) < 0 || fclose(out) != 0) {
            perror(out_path);
            ok = 0;
        }
    }
    jw_free(&w);
    jw_free(&rep_w);
    jw_free(&mism);
    jw_free(&bm);
    free(buf);
    free(rep);
    free_jsonl(&peer);
    free_jsonl(&own);
    return ok ? 0 : 1;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        usage();
        return 2;
    }
    if (strcmp(argv[1], "build") == 0) {
        earth_ctx_t ctx;
        if (earth_ctx_init(&ctx, "Earth-MCC", "ipn:99.1") != 0)
            return 2;
        int rc = cmd_build(argc - 2, argv + 2, &ctx);
        earth_ctx_free(&ctx);
        return rc;
    }
    if (strcmp(argv[1], "receive") == 0)
        return cmd_receive(argc - 2, argv + 2);
    if (strcmp(argv[1], "plan") == 0)
        return cmd_plan(argc - 2, argv + 2);
    if (strcmp(argv[1], "lifetime") == 0)
        return cmd_lifetime(argc - 2, argv + 2);
    if (strcmp(argv[1], "crosscheck") == 0)
        return cmd_crosscheck(argc - 2, argv + 2);
    usage();
    return 2;
}
