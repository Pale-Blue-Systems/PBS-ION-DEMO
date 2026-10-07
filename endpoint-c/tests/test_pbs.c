/*
 * test_pbs.c - unit tests for Endpoint B.
 *
 *   test_pbs <test-vectors.json> [--report FILE]
 *
 * Runs every entry of docs/test-vectors.json (envelopes, receiver cases,
 * lifetime cases, class-of-service map, test keys, rover policy, SEC-B
 * messages and SEC-B cases) and the negative cases below. --report writes
 * one JSON line per vector check.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#include "earth.h"
#include "json.h"
#include "pbs.h"

#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int g_pass, g_fail;
static FILE *g_report;
static const char *g_group = "";

static void check(int ok, const char *id, const char *what, const char *fmt, ...)
    __attribute__((format(printf, 4, 5)));

static void check(int ok, const char *id, const char *what, const char *fmt, ...)
{
    if (ok) {
        g_pass++;
    } else {
        g_fail++;
        fprintf(stderr, "FAIL [%s] %s %s", g_group, id, what);
        if (fmt) {
            va_list ap;
            va_start(ap, fmt);
            fputs(": ", stderr);
            vfprintf(stderr, fmt, ap);
            va_end(ap);
        }
        fputc('\n', stderr);
    }
    if (g_report && id && id[0] && strncmp(id, "N", 1) != 0) {
        jw_t w;
        jw_init(&w);
        jw_obj_begin(&w);
        jw_kstr(&w, "group", g_group);
        jw_kstr(&w, "id", id);
        jw_kstr(&w, "check", what);
        jw_kbool(&w, "pass", ok);
        jw_obj_end(&w);
        fprintf(g_report, "%s\n", jw_cstr(&w));
        jw_free(&w);
    }
}

#define CHECK(cond, id, what) check((cond), (id), (what), NULL)

static void group(const char *name)
{
    g_group = name;
}

static void summary(const char *name, int p0, int f0)
{
    printf("  %-28s %5d checks passed, %d failed\n", name, g_pass - p0, g_fail - f0);
}

static long hexbuf(const char *hex, uint8_t **out)
{
    size_t n = strlen(hex) / 2;
    *out = malloc(n + 1);
    long r = pbs_hex_decode(hex, *out, n + 1);
    return r;
}

static int hex_eq(const uint8_t *b, size_t n, const char *hex)
{
    if (!hex || strlen(hex) != 2 * n)
        return 0;
    char *h = malloc(2 * n + 1);
    pbs_hex_encode(b, n, h);
    int eq = strcmp(h, hex) == 0;
    free(h);
    return eq;
}

static int64_t gint(const json_value_t *o, const char *k)
{
    int64_t v = INT64_MIN;
    json_get_int(o, k, &v);
    return v;
}

static json_value_t *parse_str(const char *s)
{
    char err[128];
    json_value_t *v = json_parse(s, strlen(s), err, sizeof err);
    if (!v)
        fprintf(stderr, "test JSON error: %s in %s\n", err, s);
    return v;
}

/* ---- Vector groups ------------------------------------------------------------- */

static void t_constants(const json_value_t *root)
{
    int p0 = g_pass, f0 = g_fail;
    group("constants");
    const json_value_t *c = json_get(root, "constants");
    CHECK(gint(c, "header_size") == PBS_HEADER_SIZE, "header_size", "value");
    CHECK(gint(c, "max_payload_size") == PBS_MAX_PAYLOAD, "max_payload_size", "value");
    CHECK(gint(c, "mux_version") == PBS_MUX_VERSION, "mux_version", "value");
    CHECK(gint(c, "replay_window") == PBS_REPLAY_WINDOW, "replay_window", "value");
    CHECK(gint(c, "ion_no_expiry_lifetime_s") == PBS_NO_EXPIRY_LIFETIME_S,
          "ion_no_expiry_lifetime_s", "value");
    CHECK(gint(c, "lifetime_margin_us") == PBS_LIFETIME_MARGIN_US, "lifetime_margin_us",
          "value");
    CHECK(gint(c, "dtn_epoch_offset_ms") == PBS_DTN_EPOCH_OFFSET_MS, "dtn_epoch_offset_ms",
          "value");
    const json_value_t *ex = json_get(c, "creation_time_example");
    CHECK((gint(ex, "creation_time_ms") + PBS_DTN_EPOCH_OFFSET_MS) * 1000 == gint(ex, "c_us"),
          "creation_time_example", "c_us");

    const json_value_t *cc = json_get(root, "crc32_check");
    const char *in = json_get_str(cc, "input_ascii");
    char exp[16];
    snprintf(exp, sizeof exp, "0x%08X", pbs_crc32(in, strlen(in)));
    CHECK(strcmp(exp, json_get_str(cc, "crc32")) == 0, "crc32_check", "CRC-32 of 123456789");
    summary("constants and CRC-32", p0, f0);
}

static void cmp_frames(const char *id, const uint8_t *payload, size_t plen,
                       const json_value_t *frames)
{
    pbs_mux_t *m = malloc(sizeof *m);
    pbs_reason_t r = pbs_mux_parse(payload, plen, m);
    CHECK(r == PBS_R_OK, id, "MUX parse");
    int ok = r == PBS_R_OK && m->count == frames->count && m->padding == 0;
    for (size_t i = 0; ok && i < frames->count; i++) {
        long t = earth_parse_uint(json_get(frames->items[i], "type"));
        ok = (long)m->frames[i].type == t &&
             hex_eq(payload + m->frames[i].value_offset, m->frames[i].length,
                    json_get_str(frames->items[i], "value_hex"));
    }
    CHECK(ok, id, "MUX frames equal mux_frames");
    free(m);
}

static void t_envelopes(const json_value_t *root, const earth_ctx_t *ctx, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("envelopes");
    const json_value_t *arr = json_get(root, "envelopes");
    uint8_t *out = malloc(PBS_MAX_ENVELOPE);
    for (size_t i = 0; i < arr->count; i++) {
        const json_value_t *e = arr->items[i];
        const char *id = json_get_str(e, "id");
        const json_value_t *in = json_get(e, "inputs");
        char err[160];
        size_t len = 0;

        /* Build from the inputs (Source ID from the UTF-8 string). */
        uint8_t sid[16];
        int sid_rc = pbs_source_id_encode(json_get_str(in, "source_id"), sid);
        CHECK(sid_rc == PBS_E_OK && hex_eq(sid, 16, json_get_str(in, "source_id_hex")), id,
              "Source ID encoding equals source_id_hex");
        int rc = earth_build_from_inputs(ctx, in, NULL, out, PBS_MAX_ENVELOPE, &len, err,
                                         sizeof err);
        CHECK(rc == 0, id, "build");
        CHECK(rc == 0 && hex_eq(out, len, json_get_str(e, "envelope_hex")), id,
              "built bytes equal envelope_hex");
        CHECK(rc == 0 && hex_eq(out, PBS_HEADER_SIZE, json_get_str(e, "header_hex")), id,
              "header equals header_hex");
        char crc[16];
        snprintf(crc, sizeof crc, "0x%08X", pbs_get_u32(out + 0x28));
        CHECK(strcmp(crc, json_get_str(e, "crc32")) == 0, id, "CRC32");
        CHECK((int64_t)pbs_get_u32(out + 0x20) == gint(e, "size"), id, "Size");

        /* Parse the vector bytes and compare the fields with the inputs. */
        uint8_t *vb;
        long vl = hexbuf(json_get_str(e, "envelope_hex"), &vb);
        pbs_replay_t rep;
        pbs_replay_init(&rep);
        pbs_rx_t rx;
        pbs_reason_t r = pbs_receive(pol, &rep, vb, (size_t)vl, gint(in, "timestamp_us"), &rx);
        /* Under the rover policy an unsigned envelope with a payload from
         * Earth-MCC (bound to the test keys) is rejected (R8a, A38). */
        int bound = strcmp(json_get_str(in, "source_id"), "Earth-MCC") == 0;
        pbs_reason_t want = bound && gint(e, "size") > 0 ? PBS_R_SEC_MISSING : PBS_R_OK;
        check(r == want, id, "receive at Timestamp under the rover policy", "got %s, expected %s",
              pbs_reason_str(r), pbs_reason_str(want));
        CHECK(rx.hdr.magic == 0x10 && rx.hdr.priority == gint(in, "priority") &&
                  rx.hdr.flags == gint(in, "flags") && rx.hdr.sequence == gint(in, "sequence") &&
                  (int64_t)rx.hdr.timestamp_us == gint(in, "timestamp_us") &&
                  rx.hdr.ttl == (uint32_t)gint(in, "ttl_s") &&
                  hex_eq(rx.hdr.source_id, 16, json_get_str(in, "source_id_hex")) &&
                  rx.hdr.reserved03 == 0 && rx.hdr.reserved06 == 0,
              id, "parsed fields equal inputs");
        CHECK(hex_eq(vb + PBS_HEADER_SIZE, (size_t)vl - PBS_HEADER_SIZE,
                     json_get_str(in, "payload_hex")),
              id, "payload equals payload_hex");
        char disp[64];
        pbs_source_id_display(rx.hdr.source_id, disp, sizeof disp);
        CHECK(strcmp(disp, json_get_str(in, "source_id")) == 0, id, "Source ID display");
        const json_value_t *mf = json_get(e, "mux_frames");
        if (mf)
            cmp_frames(id, vb + PBS_HEADER_SIZE, (size_t)vl - PBS_HEADER_SIZE, mf);
        free(vb);
    }
    free(out);
    summary("envelopes (ENV-*)", p0, f0);
}

static void t_receiver_cases(const json_value_t *root, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("receiver_cases");
    const json_value_t *arr = json_get(root, "receiver_cases");
    for (size_t i = 0; i < arr->count; i++) {
        const json_value_t *c = arr->items[i];
        const char *id = json_get_str(c, "id");
        const json_value_t *ex = json_get(c, "expected");
        uint8_t *b;
        long n = hexbuf(json_get_str(c, "input_hex"), &b);
        pbs_replay_t rep;
        pbs_replay_init(&rep);
        pbs_rx_t rx;
        pbs_reason_t r = pbs_receive(pol, &rep, b, (size_t)n, gint(c, "now_us"), &rx);
        const char *er = json_get_str(ex, "reason"), *eres = json_get_str(ex, "result");
        check(strcmp(pbs_reason_str(r), er) == 0 &&
                  strcmp(rx.accepted ? "ACCEPT" : "REJECT", eres) == 0,
              id, "result and reason", "got %s, expected %s %s", pbs_reason_str(r), eres, er);
        if (strcmp(id, "RX-13") == 0)
            CHECK(rx.accepted && (rx.hdr.flags & PBS_FLAG_ACK_REQ), id, "ACK_REQ = 1");
        free(b);
    }
    summary("receiver cases (RX-*)", p0, f0);
}

static void t_lifetime(const json_value_t *root)
{
    int p0 = g_pass, f0 = g_fail;
    group("lifetime_cases");
    const json_value_t *arr = json_get(root, "lifetime_cases");
    for (size_t i = 0; i < arr->count; i++) {
        const json_value_t *c = arr->items[i];
        const char *id = json_get_str(c, "id");
        pbs_lifetime_t lt;
        pbs_lifetime((uint32_t)gint(c, "ttl_s"), (uint64_t)gint(c, "timestamp_us"),
                     gint(c, "clock_us"), &lt);
        const char *eo = json_get_str(c, "outcome");
        check(strcmp(pbs_lt_outcome_str(lt.outcome), eo) == 0, id, "outcome", "got %s, expected %s",
              pbs_lt_outcome_str(lt.outcome), eo);
        const json_value_t *b = json_get(c, "spec_bound_ms");
        if (b && b->type == JSON_NUMBER)
            check(lt.has_bound && lt.bound_ms == b->integer, id, "spec_bound_ms",
                  "got %" PRId64, lt.bound_ms);
        else
            CHECK(!lt.has_bound, id, "no bound for TTL 0");
        const json_value_t *cu = json_get(c, "c_us");
        if (cu && cu->type == JSON_NUMBER)
            CHECK(lt.c_us == cu->integer, id, "c_us");
        const json_value_t *ls = json_get(c, "ion_lifespan_s");
        int sends = lt.outcome == PBS_LT_OK || lt.outcome == PBS_LT_NO_EXPIRY;
        if (ls && ls->type == JSON_NUMBER)
            CHECK(sends && lt.lifespan_s == ls->integer, id, "ion_lifespan_s");
        else
            CHECK(!sends, id, "not sent");
    }
    summary("lifetime cases (LT-*)", p0, f0);
}

static void t_cos(const json_value_t *root)
{
    int p0 = g_pass, f0 = g_fail;
    group("cos_map");
    const json_value_t *arr = json_get(root, "cos_map");
    for (size_t i = 0; i < arr->count; i++) {
        const json_value_t *c = arr->items[i];
        char id[32];
        snprintf(id, sizeof id, "priority-%" PRId64, gint(c, "priority"));
        CHECK(pbs_ion_class_of_service((uint8_t)gint(c, "priority")) ==
                  gint(c, "ion_class_of_service"),
              id, "ION class of service");
        CHECK(gint(c, "ion_ordinal") == 0, id, "ordinal 0");
        CHECK(strcmp(pbs_priority_name((uint8_t)gint(c, "priority")), json_get_str(c, "name")) == 0,
              id, "name");
    }
    CHECK(pbs_ion_class_of_service(5) == -1, "priority-5", "reserved priority has no class");
    summary("class of service map", p0, f0);
}

static void t_keys_policy(const json_value_t *root, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("sec_b.test_keys");
    const json_value_t *sb = json_get(root, "sec_b");
    const json_value_t *keys = json_get(sb, "test_keys");
    for (size_t i = 0; i < keys->count; i++) {
        const char *kid = keys->keys[i];
        const json_value_t *k = keys->items[i];
        uint16_t suite = (uint16_t)earth_parse_uint(json_get(k, "suite"));
        pbs_key_t key;
        CHECK(pbs_test_key_derive(kid, suite, &key) == PBS_E_OK, kid, "derive");
        const char *sec = json_get_str(k, suite == PBS_SUITE_ED25519 ? "seed_hex" : "key_hex");
        CHECK(hex_eq(key.secret, 32, sec), kid, "secret equals vector");
        if (suite == PBS_SUITE_ED25519)
            CHECK(hex_eq(key.public_key, 32, json_get_str(k, "public_key_hex")), kid,
                  "public key equals vector");
        int prov = json_get_bool(k, "provisioned_at_rover", 0);
        const pbs_key_t *pk = pbs_policy_find_key(pol, suite, (const uint8_t *)kid, strlen(kid));
        CHECK((pk != NULL) == prov, kid, "provisioned at rover");
        if (pk) {
            uint8_t bs[16];
            pbs_source_id_encode(json_get_str(k, "bound_source_id"), bs);
            CHECK(memcmp(pk->bound_source_id, bs, 16) == 0 &&
                      strcmp(pk->bound_authority, json_get_str(k, "bound_authority")) == 0,
                  kid, "binding");
            if (suite == PBS_SUITE_ED25519)
                CHECK(!pk->has_secret, kid, "rover holds the Ed25519 public key only");
        }
    }
    summary("test keys", p0, f0);

    p0 = g_pass;
    f0 = g_fail;
    group("sec_b.rover_policy");
    const json_value_t *rp = json_get(sb, "rover_policy");
    CHECK(gint(rp, "authority_id_type") == pol->authority_id_type, "rover_policy",
          "authority_id_type");
    CHECK(strcmp(json_get_str(rp, "authority_id"), pol->authority_id) == 0, "rover_policy",
          "authority_id");
    CHECK(gint(rp, "policy_epoch") == pol->policy_epoch, "rover_policy", "policy_epoch");
    CHECK(gint(rp, "replay_window") == PBS_REPLAY_WINDOW, "rover_policy", "replay_window");
    const json_value_t *roles = json_get(rp, "roles");
    CHECK(roles->count == pol->nroles, "rover_policy", "role count");
    for (size_t i = 0; i < roles->count && i < pol->nroles; i++) {
        int ok = strcmp(roles->keys[i], pol->roles[i].name) == 0 &&
                 roles->items[i]->count == pol->roles[i].nops;
        for (size_t j = 0; ok && j < pol->roles[i].nops; j++)
            ok = strcmp(roles->items[i]->items[j]->string, pol->roles[i].ops[j]) == 0;
        CHECK(ok, roles->keys[i], "role operations");
    }
    summary("rover policy", p0, f0);
}

static const json_value_t *find_message(const json_value_t *msgs, const char *id)
{
    for (size_t i = 0; i < msgs->count; i++)
        if (strcmp(json_get_str(msgs->items[i], "id"), id) == 0)
            return msgs->items[i];
    return NULL;
}

static void t_secb_messages(const json_value_t *root, const earth_ctx_t *ctx)
{
    int p0 = g_pass, f0 = g_fail;
    group("sec_b.messages");
    const json_value_t *msgs = json_get(json_get(root, "sec_b"), "messages");
    uint8_t *out = malloc(PBS_MAX_ENVELOPE);
    for (size_t i = 0; i < msgs->count; i++) {
        const json_value_t *m = msgs->items[i];
        const char *id = json_get_str(m, "id");
        const json_value_t *in = json_get(m, "inputs");
        const json_value_t *sec = json_get(m, "security");
        uint8_t *vb;
        long vl = hexbuf(json_get_str(m, "envelope_hex"), &vb);
        CHECK(vl >= PBS_HEADER_SIZE, id, "envelope_hex decodes");
        char crc[16];
        snprintf(crc, sizeof crc, "0x%08X", pbs_get_u32(vb + 0x28));
        CHECK(strcmp(crc, json_get_str(m, "crc32")) == 0, id, "CRC32 field equals crc32");
        if (in) {
            char err[160];
            size_t len = 0;
            int rc = earth_build_from_inputs(ctx, in, sec, out, PBS_MAX_ENVELOPE, &len, err,
                                             sizeof err);
            check(rc == 0, id, "build", "%s", err);
            CHECK(rc == 0 && len == (size_t)vl && memcmp(out, vb, len) == 0, id,
                  "built bytes equal envelope_hex");
            CHECK(hex_eq(vb, PBS_HEADER_SIZE, json_get_str(m, "header_hex")), id,
                  "header equals header_hex");
            CHECK((int64_t)pbs_get_u32(vb + 0x20) == gint(m, "size"), id, "Size");
            cmp_frames(id, vb + PBS_HEADER_SIZE, (size_t)vl - PBS_HEADER_SIZE,
                       json_get(m, "mux_frames"));
        }
        if (sec && json_get_str(sec, "auth_input_hex")) {
            int64_t ail = gint(sec, "auth_input_len");
            CHECK(ail > 0 && ail < vl && hex_eq(vb, (size_t)ail, json_get_str(sec, "auth_input_hex")),
                  id, "auth_input equals envelope[0 : 44 + Size - auth_len]");
            CHECK(hex_eq(vb + ail, (size_t)(vl - ail), json_get_str(sec, "auth_data_hex")), id,
                  "auth_data is the envelope tail");
            const pbs_key_t *sk = earth_find_key(ctx, json_get_str(sec, "signing_key"));
            CHECK(sk && pbs_auth_verify(sk, vb, (size_t)ail, vb + ail, (size_t)(vl - ail)) == 1,
                  id, "auth_data verifies with signing_key");
        }
        free(vb);
    }
    free(out);
    summary("SEC-B messages (CMD-*)", p0, f0);
}

static void t_secb_cases(const json_value_t *root, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("sec_b.cases");
    const json_value_t *sb = json_get(root, "sec_b");
    const json_value_t *msgs = json_get(sb, "messages");
    const json_value_t *cases = json_get(sb, "cases");
    for (size_t i = 0; i < cases->count; i++) {
        const json_value_t *c = cases->items[i];
        const char *id = json_get_str(c, "id");
        const json_value_t *dl = json_get(c, "deliveries");
        pbs_replay_t *rep = malloc(sizeof *rep);
        pbs_replay_init(rep);
        for (size_t d = 0; d < dl->count; d++) {
            const json_value_t *dv = dl->items[d];
            const json_value_t *m = find_message(msgs, json_get_str(dv, "message"));
            char did[48];
            snprintf(did, sizeof did, "%s#%zu", id, d + 1);
            if (!m) {
                CHECK(0, did, "message exists");
                continue;
            }
            uint8_t *b;
            long n = hexbuf(json_get_str(m, "envelope_hex"), &b);
            pbs_rx_t rx;
            pbs_reason_t r = pbs_receive(pol, rep, b, (size_t)n, gint(dv, "now_us"), &rx);
            const json_value_t *ex = json_get(dv, "expected");
            const char *er = json_get_str(ex, "reason"), *eres = json_get_str(ex, "result");
            check(strcmp(pbs_reason_str(r), er) == 0 &&
                      strcmp(rx.accepted ? "ACCEPT" : "REJECT", eres) == 0,
                  did, "result and reason", "%s: got %s, expected %s %s",
                  json_get_str(dv, "message"), pbs_reason_str(r), eres, er);
            free(b);
        }
        free(rep);
    }
    summary("SEC-B cases (SB-*)", p0, f0);
}

/* ---- Negative and additional cases ------------------------------------------------ */

static size_t mk_env(uint8_t *out, uint8_t prio, const char *src, uint64_t ts, uint32_t ttl,
                     const uint8_t *payload, size_t plen)
{
    pbs_header_t h;
    size_t len = 0;
    memset(&h, 0, sizeof h);
    h.priority = prio;
    pbs_source_id_encode(src, h.source_id);
    h.timestamp_us = ts;
    h.ttl = ttl;
    h.sequence = 1;
    if (pbs_envelope_build(&h, payload, plen, out, PBS_MAX_ENVELOPE, &len) != PBS_E_OK)
        return 0;
    return len;
}

static void sign_tail(uint8_t *env, size_t len, const pbs_key_t *k)
{
    int al = pbs_suite_auth_len(k->suite_id);
    size_t ol;
    pbs_auth_compute(k, env, len - (size_t)al, env + len - al, (size_t)al, &ol);
}

/* Security frame value with zeroed nonce and auth_data. */
static size_t sec_value(uint8_t *v, uint8_t ver, uint16_t suite, uint8_t flags, const char *kid,
                        uint8_t nonce_len, uint16_t auth_len, uint64_t seq)
{
    size_t kl = strlen(kid);
    v[0] = ver;
    pbs_put_u16(v + 1, suite);
    v[3] = flags;
    v[4] = (uint8_t)kl;
    v[5] = nonce_len;
    pbs_put_u16(v + 6, auth_len);
    pbs_put_u64(v + 8, seq);
    memcpy(v + 16, kid, kl);
    memset(v + 16 + kl, 0, (size_t)nonce_len + auth_len);
    return 16 + kl + nonce_len + auth_len;
}

static const uint8_t AUTH_FLIGHT[] = {0x01, 0x03, 0x0c, 0x0a, 0x00, 0x00, 0x00, 0x01,
                                      'P', 'B', 'S', '-', 'D', 'E', 'M', 'O', '-', 'M', 'C', 'C',
                                      'F', 'L', 'I', 'G', 'H', 'T', '-', 'O', 'P', 'S'};

#define T0 1767225600000000LL

/* Build: frames + a Security frame with the given fields, sign with k. */
static size_t mk_signed(uint8_t *out, const pbs_frame_in_t *fr, size_t n, uint8_t ver,
                        uint16_t suite, uint8_t flags, const char *kid, uint8_t nonce_len,
                        uint16_t auth_len, uint64_t seq, const pbs_key_t *k, size_t pad)
{
    pbs_frame_in_t all[16];
    uint8_t sv[512];
    uint8_t payload[2048];
    size_t pl = 0;
    memcpy(all, fr, n * sizeof *fr);
    size_t svl = sec_value(sv, ver, suite, flags, kid, nonce_len, auth_len, seq);
    all[n].type = PBS_FRAME_SECURITY;
    all[n].value = sv;
    all[n].length = svl;
    pbs_mux_encode(all, n + 1, payload, sizeof payload - pad, &pl);
    memset(payload + pl, 0, pad);
    pl += pad;
    size_t len = mk_env(out, 1, "Earth-MCC", (uint64_t)T0, 30, payload, pl);
    if (k && pad == 0)
        sign_tail(out, len, k);
    return len;
}

static pbs_reason_t rx1(const pbs_policy_t *pol, const uint8_t *b, size_t n)
{
    pbs_replay_t *rep = malloc(sizeof *rep);
    pbs_replay_init(rep);
    pbs_rx_t rx;
    pbs_reason_t r = pbs_receive(pol, rep, b, n, T0 + 2000000, &rx);
    free(rep);
    return r;
}

static void expect(pbs_reason_t got, pbs_reason_t want, const char *id, const char *what)
{
    check(got == want, id, what, "got %s, expected %s", pbs_reason_str(got), pbs_reason_str(want));
}

static void t_negative(const pbs_policy_t *pol, earth_ctx_t *ctx)
{
    int p0 = g_pass, f0 = g_fail;
    group("negative");
    uint8_t *b = malloc(PBS_MAX_ENVELOPE + 16);
    uint8_t sid[16];
    const pbs_key_t *ed = earth_find_key(ctx, "TEST-ED25519-01");
    const pbs_key_t *hm = earth_find_key(ctx, "TEST-HMAC-01");
    const uint8_t cmd[] = "DRIVE 10";
    pbs_frame_in_t fr[4] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                            {PBS_FRAME_COMMAND, cmd, 8}};
    size_t n;

    /* Source ID length (A4). */
    CHECK(pbs_source_id_encode("0123456789ABCDEF", sid) == PBS_E_OK, "N01", "16-byte Source ID");
    CHECK(pbs_source_id_encode("0123456789ABCDEFG", sid) == PBS_E_SOURCE_ID, "N01",
          "17-byte Source ID refused");
    CHECK(pbs_source_id_encode("Rover-\xc3\x9c-Mond-\xc3\x84X", sid) == PBS_E_SOURCE_ID, "N01",
          "17-byte UTF-8 Source ID refused");
    char disp[64];
    uint8_t bad[16] = {'A', 0xC3, 'B', 0xFF, 0, 'Z'};
    pbs_source_id_display(bad, disp, sizeof disp);
    CHECK(strcmp(disp, "A\xEF\xBF\xBD" "B\xEF\xBF\xBD") == 0, "N01",
          "invalid UTF-8 displayed with U+FFFD, stops at 0x00");

    /* Size above the maximum (A9) and A7/A8 lengths. */
    {
        pbs_header_t h;
        memset(&h, 0, sizeof h);
        h.magic = PBS_MAGIC;
        h.size = PBS_MAX_PAYLOAD + 1;
        h.timestamp_us = (uint64_t)T0;
        pbs_header_write(&h, b);
        pbs_header_seal(b);
        memset(b + 44, 0, PBS_MAX_PAYLOAD + 1);
        pbs_replay_t rep;
        pbs_rx_t rx;
        pbs_replay_init(&rep);
        expect(pbs_receive(pol, &rep, b, 44 + PBS_MAX_PAYLOAD + 1, T0, &rx), PBS_R_LENGTH, "N02",
               "Size 65537 rejected");
        /* Oversize bundle with only the header read (pbs_earth path). */
        expect(pbs_receive_ex(pol, &rep, b, 44, 10000000, T0, &rx), PBS_R_LENGTH, "N02",
               "oversize payload, header only");
        b[0] = 0x11;
        expect(pbs_receive_ex(pol, &rep, b, 44, 10000000, T0, &rx), PBS_R_MAGIC, "N02",
               "oversize payload with bad magic: MAGIC first");
        expect(pbs_receive(pol, &rep, NULL, 0, T0, &rx), PBS_R_LENGTH, "N02", "empty payload");
    }

    /* MUX structure (A13, A14). */
    {
        uint8_t p1[] = {0x01};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p1, 1);
        expect(rx1(pol, b, n), PBS_R_MUX_MALFORMED, "N03", "1-byte container");
        uint8_t p2[] = {0x02};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p2, 1);
        expect(rx1(pol, b, n), PBS_R_MUX_VERSION, "N03", "1-byte container, version 2");
        uint8_t p3[] = {0x01, 0x01, 0x01, 0x00};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p3, sizeof p3);
        expect(rx1(pol, b, n), PBS_R_MUX_MALFORMED, "N04", "truncated frame header");
        uint8_t p4[] = {0x01, 0x00, 0xAA, 0xBB};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p4, sizeof p4);
        expect(rx1(pol, b, n), PBS_R_SEC_MISSING, "N04", "frame_count 0 with padding (R8a)");
        uint8_t p4b[] = {0x01, 0x00};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p4b, sizeof p4b);
        expect(rx1(pol, b, n), PBS_R_OK, "N04", "frame_count 0, no padding (R8b)");
        uint8_t p4c[] = {0x01, 0x01, 0x01, 0x00, 0x01, 'T', 0x00};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p4c, sizeof p4c);
        expect(rx1(pol, b, n), PBS_R_SEC_MISSING, "N04", "one frame and one padding byte (R8a)");
        uint8_t p5[] = {0x01, 0x01, 0x02, 0x00, 0x04, 'P', 'I', 'N', 'G'};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p5, sizeof p5);
        expect(rx1(pol, b, n), PBS_R_SEC_MISSING, "N15", "unprotected command without AUTH");
        /* R8a (digest A38). */
        uint8_t p6[] = {0x01, 0x01, 0x08, 0x00, 0x0A, 0x01, 0x03, 0x01, 0x01, 0, 0, 0, 1, 'M', 'R'};
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p6, sizeof p6);
        expect(rx1(pol, b, n), PBS_R_SEC_MISSING, "N16", "Authority Context without Security frame");
        uint8_t p7[] = {0x01, 0x01, 0x01, 0x00, 0x03, 'T', 'L', 'M'};
        n = mk_env(b, 2, "Earth-MCC", (uint64_t)T0, 60, p7, sizeof p7);
        expect(rx1(pol, b, n), PBS_R_SEC_MISSING, "N17", "unsigned telemetry from Earth-MCC (bound)");
        n = mk_env(b, 2, "Rover-Alpha", (uint64_t)T0, 60, p7, sizeof p7);
        expect(rx1(pol, b, n), PBS_R_OK, "N17", "unsigned telemetry from Rover-Alpha (not bound)");
        n = mk_env(b, 0, "Earth-MCC", (uint64_t)T0, 0, NULL, 0);
        expect(rx1(pol, b, n), PBS_R_OK, "N17", "header-only envelope from Earth-MCC (R6a)");
        n = mk_env(b, 2, "Earth-MCC", (uint64_t)T0, 60, p4b, sizeof p4b);
        expect(rx1(pol, b, n), PBS_R_SEC_MISSING, "N17", "empty container from Earth-MCC");
    }

    /* Security frame structure (R9, R9a, R9b). */
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 0);
    expect(rx1(pol, b, n), PBS_R_OK, "N05", "reference signed command");
    {
        /* Security frame not last. */
        uint8_t sv[128];
        size_t svl = sec_value(sv, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1);
        pbs_frame_in_t f2[3] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                                {PBS_FRAME_SECURITY, sv, svl},
                                {PBS_FRAME_COMMAND, cmd, 8}};
        uint8_t pl[512];
        size_t pll;
        pbs_mux_encode(f2, 3, pl, sizeof pl, &pll);
        n = mk_env(b, 1, "Earth-MCC", (uint64_t)T0, 30, pl, pll);
        expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N05", "Security frame not last");
        /* Two Security frames. */
        pbs_frame_in_t f3[4] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                                {PBS_FRAME_COMMAND, cmd, 8},
                                {PBS_FRAME_SECURITY, sv, svl},
                                {PBS_FRAME_SECURITY, sv, svl}};
        pbs_mux_encode(f3, 4, pl, sizeof pl, &pll);
        n = mk_env(b, 1, "Earth-MCC", (uint64_t)T0, 30, pl, pll);
        sign_tail(b, n, ed);
        expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N06", "two Security frames");
        /* Value shorter than 16 bytes. */
        pbs_frame_in_t f4[3] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                                {PBS_FRAME_COMMAND, cmd, 8},
                                {PBS_FRAME_SECURITY, sv, 15}};
        pbs_mux_encode(f4, 3, pl, sizeof pl, &pll);
        n = mk_env(b, 1, "Earth-MCC", (uint64_t)T0, 30, pl, pll);
        expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N07", "Security value of 15 bytes");
        /* Declared lengths larger than the value. */
        sv[4] = 40;
        pbs_frame_in_t f5[3] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                                {PBS_FRAME_COMMAND, cmd, 8},
                                {PBS_FRAME_SECURITY, sv, svl}};
        pbs_mux_encode(f5, 3, pl, sizeof pl, &pll);
        n = mk_env(b, 1, "Earth-MCC", (uint64_t)T0, 30, pl, pll);
        expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N10", "key_id_len inconsistent with value");
    }
    n = mk_signed(b, fr, 2, 2, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N08", "Security version 2");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 3, "TEST-ED25519-01", 0, 64, 1, ed, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N09", "flags 0x03");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 0, "TEST-ED25519-01", 0, 64, 1, ed, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N09", "flags 0x00");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 1, 64, 1, NULL, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N09", "nonce_len 1");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_HMAC_SHA256, 1, "TEST-HMAC-01", 0, 31, 1, NULL, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N09", "HMAC auth_len 31");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 1, "", 0, 64, 1, NULL, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N09", "key_id_len 0");
    n = mk_signed(b, fr, 2, 1, 0x0001, 1, "TEST-ED25519-01", 0, 64, 1, NULL, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_SUITE_UNKNOWN, "N09", "suite 0x0001");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_HMAC_SHA256, 1, "TEST-ED25519-01", 0, 32, 1, hm, 0);
    expect(rx1(pol, b, n), PBS_R_SEC_KEY_UNKNOWN, "N09",
           "key_id of the other suite (lookup by suite and key_id)");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_HMAC_SHA256, 1, "TEST-HMAC-01", 0, 32, 1, hm, 0);
    expect(rx1(pol, b, n), PBS_R_OK, "N16", "HMAC command");
    b[n - 1] ^= 0x80;
    expect(rx1(pol, b, n), PBS_R_SEC_AUTH_FAIL, "N16", "HMAC tag with one bit flipped");
    n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 2);
    expect(rx1(pol, b, n), PBS_R_SEC_MALFORMED, "N06", "padding after Security frame");

    /* Authority context (R12, R12a). */
    {
        pbs_frame_in_t f2[3] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                                {PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT},
                                {PBS_FRAME_COMMAND, cmd, 8}};
        n = mk_signed(b, f2, 3, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 0);
        expect(rx1(pol, b, n), PBS_R_AUTH_CTX_MALFORMED, "N11", "two Authority Context frames");
        uint8_t a2[sizeof AUTH_FLIGHT];
        memcpy(a2, AUTH_FLIGHT, sizeof a2);
        a2[0] = 2;
        pbs_frame_in_t f3[2] = {{PBS_FRAME_AUTHORITY, a2, sizeof a2}, {PBS_FRAME_COMMAND, cmd, 8}};
        n = mk_signed(b, f3, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 0);
        expect(rx1(pol, b, n), PBS_R_AUTH_CTX_MALFORMED, "N12", "AUTH version 2");
        pbs_frame_in_t f4[2] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, sizeof AUTH_FLIGHT - 1},
                                {PBS_FRAME_COMMAND, cmd, 8}};
        n = mk_signed(b, f4, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 0);
        expect(rx1(pol, b, n), PBS_R_AUTH_CTX_MALFORMED, "N12", "AUTH value one byte short");
        pbs_frame_in_t f5[2] = {{PBS_FRAME_AUTHORITY, AUTH_FLIGHT, 7}, {PBS_FRAME_COMMAND, cmd, 8}};
        n = mk_signed(b, f5, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 1, ed, 0);
        expect(rx1(pol, b, n), PBS_R_AUTH_CTX_MALFORMED, "N12", "AUTH value of 7 bytes");
    }

    /* Authorization (R13). */
    {
        static const struct {
            const char *json;
            pbs_reason_t want;
            const char *what;
        } az[] = {
            {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"authority_id_type\":1}",
             PBS_R_AUTHZ_DENIED, "authority_id_type 1"},
            {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"authority_id\":\"OTHER-MCC\"}",
             PBS_R_AUTHZ_DENIED, "authority not bound to the key"},
            {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"role\":\"GUEST\"}",
             PBS_R_AUTHZ_DENIED, "unknown role"},
            {"{\"kind\":\"command\",\"command\":\"DRIVEX 10\"}", PBS_R_AUTHZ_DENIED,
             "operation DRIVEX"},
            {"{\"kind\":\"command\",\"command\":\"\"}", PBS_R_AUTHZ_DENIED, "empty command"},
            {"{\"kind\":\"command\",\"command\":\" DRIVE\"}", PBS_R_AUTHZ_DENIED,
             "leading space: empty operation"},
            {"{\"kind\":\"command\",\"command\":\"SET_MODE SAFE\"}", PBS_R_OK, "SET_MODE"},
            {"{\"kind\":\"command\",\"command\":\"PING\"}", PBS_R_OK, "PING"},
            {"{\"kind\":\"command\",\"role\":\"SCIENCE-OPS\",\"command\":\"CAPTURE_IMAGE PANCAM\","
             "\"extra_frames\":[{\"type\":2,\"text\":\"DRIVE 1\"}]}",
             PBS_R_AUTHZ_DENIED, "two commands, second not permitted for the role"},
            {"{\"kind\":\"command\",\"role\":\"SCIENCE-OPS\",\"command\":\"CAPTURE_IMAGE PANCAM\","
             "\"extra_frames\":[{\"type\":2,\"text\":\"CAPTURE_IMAGE NAVCAM\"}]}",
             PBS_R_OK, "two permitted commands"},
            {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"source_id\":\"Earth-MCC2\"}",
             PBS_R_AUTHZ_DENIED, "Source ID not bound to the key"},
            {"{\"kind\":\"telemetry\",\"text\":\"TLM SIGNED\",\"sign\":true}", PBS_R_OK,
             "protected telemetry without commands (R11a)"},
            {"{\"kind\":\"telemetry\",\"text\":\"TLM SIGNED\",\"sign\":true,\"suite\":\"hmac\"}",
             PBS_R_OK, "protected telemetry, HMAC"},
        };
        earth_item_t it;
        earth_item_alloc(&it);
        for (size_t i = 0; i < sizeof az / sizeof az[0]; i++) {
            json_value_t *j = parse_str(az[i].json);
            char err[160];
            int rc = earth_build(ctx, j, T0, &it, err, sizeof err);
            check(rc == 0, "N13", az[i].what, "build: %s", err);
            if (rc == 0)
                expect(rx1(pol, it.env, it.len), az[i].want, "N13", az[i].what);
            json_free(j);
        }
        earth_item_free(&it);
    }

    /* Replay window details (digest 6.4). */
    {
        pbs_replay_t *r = malloc(sizeof *r);
        pbs_replay_init(r);
        uint8_t s1[16] = "Earth-MCC", s2[16] = "Other";
        const uint8_t *k1 = (const uint8_t *)"K1", *k2 = (const uint8_t *)"K2";
        CHECK(!pbs_replay_check(r, s1, k1, 2, 0), "N17", "0 rejected on empty state");
        CHECK(pbs_replay_check(r, s1, k1, 2, 100), "N17", "100 accepted");
        pbs_replay_record(r, s1, k1, 2, 100);
        CHECK(!pbs_replay_check(r, s1, k1, 2, 36), "N17", "H-64 rejected");
        CHECK(pbs_replay_check(r, s1, k1, 2, 37), "N17", "H-63 accepted");
        CHECK(!pbs_replay_check(r, s1, k1, 2, 100), "N17", "repeat of H rejected");
        CHECK(pbs_replay_check(r, s2, k1, 2, 100), "N17", "other Source ID has its own state");
        CHECK(pbs_replay_check(r, s1, k2, 2, 100), "N17", "other key_id has its own state");
        pbs_replay_record(r, s1, k1, 2, 37);
        pbs_replay_record(r, s1, k1, 2, 101);
        CHECK(!pbs_replay_check(r, s1, k1, 2, 37), "N17", "37 kept after shift by 1");
        CHECK(!pbs_replay_check(r, s1, k1, 2, 100), "N17", "100 kept after shift by 1");
        pbs_replay_record(r, s1, k1, 2, 165);
        CHECK(!pbs_replay_check(r, s1, k1, 2, 101), "N17", "shift by 64: 101 = H-64 rejected");
        CHECK(pbs_replay_check(r, s1, k1, 2, 102), "N17", "shift by 64: 102 accepted");
        CHECK(pbs_replay_check(r, s1, k1, 2, UINT64_MAX), "N17", "2^64-1 accepted");
        pbs_replay_record(r, s1, k1, 2, UINT64_MAX);
        CHECK(!pbs_replay_check(r, s1, k1, 2, UINT64_MAX), "N17", "2^64-1 then rejected");
        CHECK(!pbs_replay_check(r, s1, k1, 2, 165), "N17", "old values rejected after max");
        /* Fill the table: fail closed. */
        pbs_replay_init(r);
        uint8_t src[16] = {0};
        for (int i = 0; i < PBS_REPLAY_SLOTS; i++) {
            pbs_put_u32(src, (uint32_t)i);
            pbs_replay_record(r, src, k1, 2, 1);
        }
        pbs_put_u32(src, 999999);
        CHECK(!pbs_replay_check(r, src, k1, 2, 1), "N17", "state table full: fail closed");
        free(r);
    }

    /* Replay state is not consumed before authentication, and is consumed
     * before authorization (A22). */
    {
        pbs_replay_t *r = malloc(sizeof *r);
        pbs_rx_t rx;
        pbs_replay_init(r);
        n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 7, ed, 0);
        b[n - 1] ^= 1;
        expect(pbs_receive(pol, r, b, n, T0, &rx), PBS_R_SEC_AUTH_FAIL, "N18", "bad signature, s=7");
        b[n - 1] ^= 1;
        expect(pbs_receive(pol, r, b, n, T0, &rx), PBS_R_OK, "N18",
               "valid s=7 accepted after failed authentication");
        uint8_t a2[sizeof AUTH_FLIGHT];
        memcpy(a2, AUTH_FLIGHT, sizeof a2);
        a2[7] = 9; /* epoch 9 */
        pbs_frame_in_t f2[2] = {{PBS_FRAME_AUTHORITY, a2, sizeof a2}, {PBS_FRAME_COMMAND, cmd, 8}};
        n = mk_signed(b, f2, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 8, ed, 0);
        expect(pbs_receive(pol, r, b, n, T0, &rx), PBS_R_AUTHZ_DENIED, "N19", "epoch 9, s=8");
        n = mk_signed(b, fr, 2, 1, PBS_SUITE_ED25519, 1, "TEST-ED25519-01", 0, 64, 8, ed, 0);
        expect(pbs_receive(pol, r, b, n, T0, &rx), PBS_R_SEC_REPLAY, "N19",
               "s=8 consumed by the unauthorized command");
        free(r);
    }

    /* TTL edge cases (A6, A37). */
    CHECK(!pbs_ttl_expired(30, (uint64_t)T0 + 5000000, T0), "N23", "future Timestamp not expired");
    CHECK(!pbs_ttl_expired(0, 0, INT64_MAX), "N23", "TTL 0 at maximum age");
    CHECK(!pbs_ttl_expired(1, 0, -5), "N23", "negative clock reading");
    CHECK(!pbs_ttl_expired(1, UINT64_MAX, T0), "N23", "Timestamp 2^64-1");
    CHECK(pbs_ttl_expired(4294967295u, 0, INT64_MAX), "N23", "TTL 2^32-1 at maximum age");
    CHECK(!pbs_ttl_expired(4294967295u, 0, 4294967295000000LL), "N23", "TTL 2^32-1 boundary");
    CHECK(pbs_ttl_expired(4294967295u, 0, 4294967295000001LL), "N23", "TTL 2^32-1 + 1 us");

    /* Lifetime edge cases (digest 7.4). */
    {
        pbs_lifetime_t lt;
        pbs_lifetime(1, (uint64_t)T0, T0, &lt);
        CHECK(lt.outcome == PBS_LT_GW_EXPIRED && lt.bound_ms == 0, "N24", "TTL 1 at Timestamp");
        pbs_lifetime(2, (uint64_t)T0, T0, &lt);
        CHECK(lt.outcome == PBS_LT_OK && lt.lifespan_s == 1, "N24", "TTL 2 at Timestamp: 1 s");
        pbs_lifetime(30, UINT64_MAX, T0, &lt);
        CHECK(lt.outcome == PBS_LT_OK && lt.bound_ms == 30000, "N24", "future Timestamp: full TTL");
        pbs_lifetime(30, (uint64_t)T0, T0 + 29001500, &lt);
        CHECK(lt.outcome == PBS_LT_GW_EXPIRED && lt.bound_ms == -2, "N24",
              "floor of a negative bound (-1.5 ms -> -2)");
        CHECK(pbs_floor_div(-1, 1000) == -1 && pbs_floor_div(-1000, 1000) == -1 &&
                  pbs_floor_div(999, 1000) == 0,
              "N24", "floor division");
    }

    free(b);
    summary("negative and edge cases", p0, f0);
}

/* Every single-bit flip and every truncation of a valid command is
 * rejected; seeded random mutation never crashes. */
static void t_mutation(const json_value_t *root, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("mutation");
    const json_value_t *msgs = json_get(json_get(root, "sec_b"), "messages");
    const json_value_t *cmd01 = find_message(msgs, "CMD-01");
    const json_value_t *cmd02 = find_message(msgs, "CMD-02");
    pbs_replay_t *rep = malloc(sizeof *rep);
    pbs_rx_t rx;
    const json_value_t *two[2] = {cmd01, cmd02};
    for (int m = 0; m < 2; m++) {
        uint8_t *b;
        long n = hexbuf(json_get_str(two[m], "envelope_hex"), &b);
        const char *id = json_get_str(two[m], "id");
        /* Every single-bit flip is rejected. A flip of frame_count 3 -> 1
         * leaves the Authority Context frame and padding; R8a rejects it
         * with SEC_MISSING (digest A38). */
        int accepted = 0, with_cmd = 0, total = 0, counts[PBS_R_COUNT_] = {0};
        for (long i = 0; i < n * 8; i++) {
            b[i / 8] ^= (uint8_t)(1u << (i % 8));
            pbs_replay_init(rep);
            pbs_reason_t r = pbs_receive(pol, rep, b, (size_t)n, T0 + 2000000, &rx);
            if (rx.accepted) {
                accepted++;
                if (rx.n_commands)
                    with_cmd++;
            }
            counts[r]++;
            total++;
            b[i / 8] ^= (uint8_t)(1u << (i % 8));
        }
        char what[96];
        snprintf(what, sizeof what, "none of %d single-bit flips accepted with a command", total);
        check(with_cmd == 0, id, what, "%d accepted with a command", with_cmd);
        snprintf(what, sizeof what, "none of %d single-bit flips accepted", total);
        check(accepted == 0, id, what, "%d accepted", accepted);
        printf("    %s: %d single-bit flips:", id, total);
        for (int k = 0; k < PBS_R_COUNT_; k++)
            if (counts[k])
                printf(" %s %d", k == PBS_R_OK ? "accepted" : pbs_reason_str((pbs_reason_t)k),
                       counts[k]);
        printf("\n");
        /* frame_count lowered to each value below 3 (vectors CMD-19, CMD-20). */
        for (uint8_t fc = 0; fc < 3; fc++) {
            uint8_t keep = b[45];
            b[45] = fc;
            pbs_replay_init(rep);
            pbs_reason_t r = pbs_receive(pol, rep, b, (size_t)n, T0 + 1, &rx);
            snprintf(what, sizeof what, "frame_count %u: SEC_MISSING", fc);
            check(r == PBS_R_SEC_MISSING, id, what, "got %s", pbs_reason_str(r));
            b[45] = keep;
        }
        accepted = 0;
        for (long l = 0; l < n; l++) {
            pbs_replay_init(rep);
            pbs_receive(pol, rep, b, (size_t)l, T0 + 2000000, &rx);
            accepted += rx.accepted;
            if (l < PBS_HEADER_SIZE && rx.reason != PBS_R_LENGTH)
                accepted += 1000;
        }
        snprintf(what, sizeof what, "all %ld truncations rejected", n);
        CHECK(accepted == 0, id, what);
        uint8_t *ext = malloc((size_t)n + 1);
        memcpy(ext, b, (size_t)n);
        ext[n] = 0;
        pbs_replay_init(rep);
        expect(pbs_receive(pol, rep, ext, (size_t)n + 1, T0 + 2000000, &rx), PBS_R_LENGTH, id,
               "one byte appended");
        free(ext);
        free(b);
    }

    /* Seeded random mutation of every vector envelope. */
    uint64_t seed = 0x5EEDC0FFEE123457ULL;
    long iters = 0, acc = 0, inv_fail = 0;
    const char *sections[2] = {"envelopes", "receiver_cases"};
    uint8_t *buf = malloc(PBS_MAX_ENVELOPE + 64);
    for (int round = 0; round < 400; round++) {
        for (int s = 0; s < 3; s++) {
            const json_value_t *arr = s < 2 ? json_get(root, sections[s]) : msgs;
            for (size_t i = 0; i < arr->count; i++) {
                const char *hx = json_get_str(arr->items[i], s == 1 ? "input_hex" : "envelope_hex");
                long n = pbs_hex_decode(hx, buf, PBS_MAX_ENVELOPE);
                if (n <= 0)
                    continue;
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                int k = 1 + (int)(seed % 4);
                for (int j = 0; j < k; j++) {
                    seed ^= seed << 13;
                    seed ^= seed >> 7;
                    seed ^= seed << 17;
                    buf[seed % (uint64_t)n] ^= (uint8_t)(seed >> 32);
                }
                size_t len = (size_t)n;
                if ((seed >> 40) % 8 == 0)
                    len = (size_t)((seed >> 44) % (uint64_t)(n + 1));
                if ((seed >> 50) % 4 == 0)
                    pbs_header_seal(buf); /* make the header CRC valid again */
                pbs_replay_init(rep);
                pbs_receive(pol, rep, buf, len, T0 + 2000000, &rx);
                iters++;
                if (rx.accepted) {
                    acc++;
                    if (!(rx.hdr.magic == PBS_MAGIC && rx.hdr.priority <= 4 &&
                          pbs_header_crc(buf) == rx.hdr.crc32 && len == 44u + rx.hdr.size))
                        inv_fail++;
                    if (rx.n_commands && !rx.authenticated)
                        inv_fail++;
                }
            }
        }
    }
    char what[128];
    snprintf(what, sizeof what,
             "%ld random mutations: no crash; every accepted one has valid header, length and "
             "authenticated commands",
             iters);
    check(inv_fail == 0, "fuzz", what, "%ld invariant failures", inv_fail);
    printf("    random mutations: %ld, accepted %ld\n", iters, acc);
    free(buf);
    free(rep);
    summary("bit flips, truncation, fuzz", p0, f0);
}

/* Plan items as pbs_earth builds them, received by the rover policy. */
static void t_plan(earth_ctx_t *ctx, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("plan");
    earth_item_t it;
    earth_item_alloc(&it);
    char err[160];
    pbs_replay_t *rep = malloc(sizeof *rep);
    pbs_replay_init(rep);
    pbs_rx_t rx;
    int64_t now = T0 + 1000000;
    json_value_t *j;

#define BUILD(js)                                                                   \
    (j = parse_str(js), earth_build(ctx, j, now, &it, err, sizeof err) == 0 ? (json_free(j), 0) \
                                                                         : (json_free(j), -1))

    ctx->next_sequence = 1;
    CHECK(BUILD("{\"kind\":\"telemetry\",\"text\":\"TLM BATT=27.9V\"}") == 0, "P01", "telemetry");
    CHECK(it.hdr.priority == 2 && it.hdr.ttl == 60 && it.hdr.sequence == 1 && it.send &&
              it.lt.outcome == PBS_LT_OK && it.lifespan_s == 59 && it.class_of_service == 1,
          "P01", "defaults: priority 2, TTL 60, Sequence 1, lifetime 59 s, class 1");
    expect(pbs_receive(pol, rep, it.env, it.len, now + 1550000, &rx), PBS_R_SEC_MISSING, "P01",
           "unsigned telemetry from Earth-MCC rejected (R8a)");
    CHECK(BUILD("{\"kind\":\"telemetry\",\"text\":\"TLM BATT=27.9V\",\"sign\":true}") == 0, "P01",
          "signed telemetry");
    expect(pbs_receive(pol, rep, it.env, it.len, now + 1550000, &rx), PBS_R_OK, "P01",
           "signed telemetry accepted as protected data (R11a)");
    CHECK(BUILD("{\"kind\":\"telemetry\",\"text\":\"X\",\"priority\":4,\"pad_to\":1000,\"ttl\":86400}") == 0,
          "P02", "bulk telemetry");
    CHECK(it.hdr.size == 1005 && it.hdr.sequence == 3 && it.class_of_service == 0, "P02",
          "pad_to 1000: Size 1005, Sequence 3, class 0");
    CHECK(BUILD("{\"kind\":\"heartbeat\",\"ttl\":0,\"priority\":0}") == 0, "P03", "heartbeat");
    CHECK(it.hdr.size == 0 && it.lt.outcome == PBS_LT_NO_EXPIRY && it.lifespan_s == 2147483647 &&
              it.class_of_service == 2,
          "P03", "TTL 0: no-expiry lifetime 2147483647 s, class 2");
    CHECK(BUILD("{\"kind\":\"status\",\"text\":\"STATUS OK\",\"flags\":1}") == 0, "P04", "status");
    CHECK(it.hdr.flags == 1 && it.env[44 + 2] == PBS_FRAME_STATUS, "P04", "ACK_REQ, Status frame");
    CHECK(BUILD("{\"kind\":\"event\",\"text\":\"EVT\",\"source_id\":\"Seventeen-bytes-x\"}") != 0,
          "P05", "Source ID over 16 bytes refused");
    CHECK(BUILD("{\"kind\":\"bogus\"}") != 0, "P05", "unknown kind refused");

    /* Sequence wrap (PBS-ENV-01 8). */
    ctx->next_sequence = 65535;
    BUILD("{\"kind\":\"heartbeat\"}");
    CHECK(it.hdr.sequence == 65535, "P06", "Sequence 65535");
    BUILD("{\"kind\":\"heartbeat\"}");
    CHECK(it.hdr.sequence == 0 && ctx->next_sequence == 1, "P06", "then 0, then 1");

    /* Commands and S6 fault injection. */
    static const struct {
        const char *json;
        pbs_reason_t want;
        const char *what;
    } cases[] = {
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"label\":\"c1\"}", PBS_R_OK, "valid Ed25519"},
        {"{\"kind\":\"replay\",\"replay_of\":\"c1\"}", PBS_R_SEC_REPLAY, "replay of c1"},
        {"{\"kind\":\"command\",\"command\":\"SET_HEATER ON\",\"suite\":\"hmac\"}", PBS_R_OK,
         "valid HMAC"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"command\"}", PBS_R_SEC_AUTH_FAIL,
         "tamper command"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"priority\"}",
         PBS_R_SEC_AUTH_FAIL, "tamper priority"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"ttl\"}", PBS_R_SEC_AUTH_FAIL,
         "tamper TTL"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"timestamp\"}",
         PBS_R_SEC_AUTH_FAIL, "tamper Timestamp"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"security_sequence\"}",
         PBS_R_SEC_AUTH_FAIL, "tamper security_sequence"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"auth_data\"}",
         PBS_R_SEC_AUTH_FAIL, "tamper auth_data"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"authority\"}",
         PBS_R_SEC_AUTH_FAIL, "tamper Authority Context"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"crc\"}", PBS_R_CRC, "tamper CRC32"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"frame_count\"}",
         PBS_R_SEC_MISSING, "tamper frame_count to 1"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"tamper\":\"frame_count_0\"}",
         PBS_R_SEC_MISSING, "tamper frame_count to 0"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"authority_id\":\"PBS-DEMO-OTHER\"}",
         PBS_R_AUTHZ_DENIED, "authority not authorized"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"signing_key\":\"TEST-ED25519-ROGUE\"}",
         PBS_R_SEC_AUTH_FAIL, "wrong key, Ed25519"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"suite\":\"hmac\",\"signing_key\":\"TEST-HMAC-ROGUE\"}",
         PBS_R_SEC_AUTH_FAIL, "wrong key, HMAC"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"key_id\":\"TEST-ED25519-ROGUE\"}",
         PBS_R_SEC_KEY_UNKNOWN, "unprovisioned key_id"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"unsigned\":true}", PBS_R_SEC_MISSING,
         "unsigned command"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"omit_auth_ctx\":true}",
         PBS_R_AUTH_CTX_MISSING, "no Authority Context"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"role\":\"SCIENCE-OPS\"}",
         PBS_R_AUTHZ_DENIED, "role SCIENCE-OPS issuing DRIVE"},
        {"{\"kind\":\"command\",\"command\":\"CAPTURE_IMAGE NAVCAM\",\"role\":\"SCIENCE-OPS\"}",
         PBS_R_OK, "role SCIENCE-OPS issuing CAPTURE_IMAGE"},
        {"{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"policy_epoch\":2}", PBS_R_AUTHZ_DENIED,
         "policy_epoch 2"},
        {"{\"kind\":\"command\",\"command\":\"PING\",\"suite\":\"0xFF7F\",\"signing_key\":\"TEST-HMAC-01\",\"key_id\":\"TEST-HMAC-01\"}",
         PBS_R_SEC_SUITE_UNKNOWN, "unknown suite"},
        {"{\"kind\":\"command\",\"command\":\"PING\",\"extra_frames\":[{\"type\":\"0x80\",\"hex\":\"deadbeef\"}]}",
         PBS_R_OK, "unknown 0x80 frame inside a protected command"},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        int rc = BUILD(cases[i].json);
        check(rc == 0, "P07", cases[i].what, "build: %s", err);
        if (rc == 0) {
            CHECK(it.send && it.class_of_service == 2 && it.lifespan_s == 29, "P07",
                  "command handed to ION: class 2, lifetime 29 s");
            expect(pbs_receive(pol, rep, it.env, it.len, now + 1550000, &rx), cases[i].want, "P07",
                   cases[i].what);
        }
    }
    /* Security sequence counters per key_id. */
    uint64_t s1, s2;
    BUILD("{\"kind\":\"command\",\"command\":\"PING\"}");
    s1 = it.security_sequence;
    BUILD("{\"kind\":\"command\",\"command\":\"PING\"}");
    s2 = it.security_sequence;
    CHECK(s2 == s1 + 1, "P08", "security_sequence increments per key_id");
    expect(pbs_receive(pol, rep, it.env, it.len, now + 1550000, &rx), PBS_R_OK, "P08",
           "next command accepted");

    /* Expiry: the gateway rule refuses, the override delivers an expired
     * envelope to the receiver (S5/S6 fault injection). */
    CHECK(BUILD("{\"kind\":\"command\",\"command\":\"PING\",\"timestamp_offset_us\":-31000000}") == 0,
          "P09", "expired command built");
    CHECK(!it.send && strcmp(it.drop_reason, "GW_EXPIRED") == 0, "P09",
          "expired command not handed to ION (GW_EXPIRED)");
    CHECK(BUILD("{\"kind\":\"command\",\"command\":\"PING\",\"ttl\":1}") == 0 && !it.send &&
              strcmp(it.drop_reason, "GW_EXPIRED") == 0,
          "P09", "TTL 1 at Timestamp: GW_EXPIRED (1 s margin)");
    CHECK(BUILD("{\"kind\":\"telemetry\",\"text\":\"T\",\"ttl\":2,\"timestamp_offset_us\":-500}") ==
                  0 && !it.send && strcmp(it.drop_reason, "GW_LIFETIME_UNDER_1S") == 0,
          "P09", "TTL 2 with 1.9995 s left: GW_LIFETIME_UNDER_1S");
    CHECK(BUILD("{\"kind\":\"command\",\"command\":\"PING\",\"ttl\":1,\"lifetime_s\":60}") == 0 &&
              it.send && it.lifetime_override && it.lifespan_s == 60,
          "P09", "lifetime override");
    expect(pbs_receive(pol, rep, it.env, it.len, now + 1550000, &rx), PBS_R_TTL_EXPIRED, "P09",
           "expired command rejected at receipt");
    CHECK(BUILD("{\"kind\":\"command\",\"command\":\"PING\",\"label\":\"late\",\"lifetime_s\":60,\"timestamp_offset_us\":-40000000}") == 0,
          "P09", "expired protected command built with override");
    expect(pbs_receive(pol, rep, it.env, it.len, now, &rx), PBS_R_TTL_EXPIRED, "P09",
           "TTL check precedes authentication");
    CHECK(BUILD("{\"kind\":\"telemetry\",\"text\":\"T\",\"ttl\":5}") == 0 && it.lifespan_s == 4,
          "P10", "TTL 5 at Timestamp: lifetime 4 s");
    CHECK(BUILD("{\"kind\":\"telemetry\",\"text\":\"T\",\"ttl\":5,\"timestamp_offset_us\":-2000000}") == 0 &&
              it.lifespan_s == 2,
          "P10", "TTL 5, 2 s old: lifetime 2 s (LT-10)");

    /* Raw bytes. */
    CHECK(BUILD("{\"kind\":\"raw\",\"hex\":\"1000010000010000526f7665722d410000000000000000000006474846204000000000000000001e588721ed\"}") == 0,
          "P11", "raw ENV-01");
    CHECK(it.len == 44 && it.hdr.crc32 == 0x588721ED && it.class_of_service == 2 && it.send &&
              it.lifespan_s == 28,
          "P11", "raw ENV-01 at T+1 s: sent unchanged, lifetime 28 s, class 2");
    CHECK(BUILD("{\"kind\":\"raw\",\"hex\":\"10\"}") != 0, "P11", "short raw item needs lifetime_s");
    CHECK(BUILD("{\"kind\":\"raw\",\"hex\":\"10\",\"lifetime_s\":5,\"class_of_service\":0}") == 0 &&
              it.send && it.class_of_service == 0,
          "P11", "short raw item with lifetime_s and class");
    CHECK(BUILD("{\"kind\":\"replay\",\"replay_of\":\"nope\"}") != 0, "P11", "unknown label");
    CHECK(BUILD("{\"kind\":\"command\",\"command\":\"PING\",\"tamper\":\"bogus\"}") != 0, "P11",
          "unknown tamper mode");
    CHECK(BUILD("{\"kind\":\"telemetry\",\"tamper\":\"command\"}") != 0, "P11",
          "tamper command on telemetry refused");

    free(rep);
    earth_item_free(&it);
    summary("plan items (pbs_earth)", p0, f0);
}

/* Log records parse back as JSON and are valid UTF-8. */
static int utf8_ok(const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        if (*p < 0x80) {
            p++;
            continue;
        }
        int n = (*p >= 0xF0) ? 4 : (*p >= 0xE0) ? 3 : (*p >= 0xC2) ? 2 : 0;
        if (!n)
            return 0;
        for (int i = 1; i < n; i++)
            if ((p[i] & 0xC0) != 0x80)
                return 0;
        p += n;
    }
    return 1;
}

/* docs/LOG-SCHEMA.md field lists. */
static const char *const F_COMMON[] = {"schema", "event", "t_us", "endpoint", "node_eid",
                                       "scenario", NULL};
static const char *const F_ENV[] = {"source_id", "source_id_hex", "sequence", "priority", "flags",
                                    "timestamp_us", "ttl_s", "size", "crc32", "header_hex",
                                    "envelope_len", "envelope_sha256", "envelope_hex",
                                    "frame_types", NULL};
static const char *const F_SEND[] = {"result", "reason", "dest_eid", "lifespan_s",
                                     "lifespan_rule", "class_of_service", "ordinal",
                                     "bp_send_return_us", "label", "commands", "suite_id",
                                     "key_id", "security_sequence", NULL};
static const char *const F_RECV[] = {"result", "reason", "bundle_source_eid",
                                     "bundle_creation_ms", "bundle_creation_count",
                                     "bundle_creation_us", "bundle_ttl_field_ms",
                                     "bundle_admin_record", "expected_eid", "eid_match",
                                     "latency_us", "commands", "suite_id", "key_id",
                                     "security_sequence", "authority_id", "role", "policy_epoch",
                                     NULL};
static const char *const F_EXEC[] = {"source_id_hex", "sequence", "timestamp_us", "command",
                                     "operation", "key_id", "security_sequence", "role", "action",
                                     NULL};

static int in_list(const char *k, const char *const *l)
{
    for (; l && *l; l++)
        if (strcmp(k, *l) == 0)
            return 1;
    return 0;
}

/* Every listed field present; every other key starts with "x_". */
static int schema_ok(const json_value_t *r, const char *const *a, const char *const *b,
                     const char *const *c, char *why, size_t cap)
{
    const char *const *lists[4] = {F_COMMON, a, b, c};
    if (!r || r->type != JSON_OBJECT) {
        snprintf(why, cap, "not an object");
        return 0;
    }
    for (int i = 0; i < 4; i++)
        for (const char *const *k = lists[i]; k && *k; k++)
            if (!json_get(r, *k)) {
                snprintf(why, cap, "missing %s", *k);
                return 0;
            }
    for (size_t i = 0; i < r->count; i++) {
        const char *k = r->keys[i];
        if (strncmp(k, "x_", 2) != 0 && !in_list(k, F_COMMON) && !in_list(k, a) &&
            !in_list(k, b) && !in_list(k, c)) {
            snprintf(why, cap, "unlisted key %s", k);
            return 0;
        }
    }
    snprintf(why, cap, "ok");
    return 1;
}

static void t_logs(earth_ctx_t *ctx, const pbs_policy_t *pol)
{
    int p0 = g_pass, f0 = g_fail;
    group("logs");
    earth_item_t it;
    earth_item_alloc(&it);
    char err[160], why[96];
    jw_t w;
    jw_init(&w);
    const earth_logctx_t lc = {"B", "ipn:23.1", "S6"};
    json_value_t *j = parse_str("{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"label\":\"x\"}");
    earth_build(ctx, j, T0, &it, err, sizeof err);
    json_free(j);
    earth_log_send(&w, &lc, &it, T0, 1, 1, T0 + 120, NULL);
    json_value_t *r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    check(schema_ok(r, F_ENV, F_SEND, NULL, why, sizeof why), "L01", "send record schema", "%s", why);
    CHECK(r && strcmp(json_get_str(r, "schema"), "pbs-ion-demo-log/1") == 0 &&
              strcmp(json_get_str(r, "endpoint"), "B") == 0 &&
              strcmp(json_get_str(r, "scenario"), "S6") == 0 &&
              strcmp(json_get_str(r, "result"), "SENT") == 0 &&
              strcmp(json_get_str(r, "reason"), "OK") == 0 &&
              strcmp(json_get_str(r, "source_id"), "Earth-MCC") == 0 &&
              json_get(r, "ttl_s")->integer == 30 &&
              strcmp(json_get_str(r, "suite_id"), "0xFF02") == 0 &&
              strcmp(json_get_str(r, "key_id"), "TEST-ED25519-01") == 0 &&
              json_get(r, "lifespan_s")->integer == 29 &&
              strcmp(json_get_str(r, "lifespan_rule"), "DTN-MAP-01-6.1") == 0 &&
              json_get(r, "class_of_service")->integer == 2 &&
              json_get(r, "bp_send_return_us")->integer == T0 + 120 &&
              json_get(r, "frame_types")->count == 3 &&
              json_get(r, "frame_types")->items[2]->integer == 10 &&
              strcmp(json_get(r, "commands")->items[0]->string, "DRIVE 10") == 0 &&
              json_get(r, "envelope_hex")->type == JSON_STRING,
          "L01", "send record values");
    json_free(r);
    /* NOT_SENT and ION refusal. */
    j = parse_str("{\"kind\":\"telemetry\",\"text\":\"T\",\"ttl\":1}");
    earth_build(ctx, j, T0, &it, err, sizeof err);
    json_free(j);
    earth_log_send(&w, &lc, &it, T0, 0, 0, 0, NULL);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    check(schema_ok(r, F_ENV, F_SEND, NULL, why, sizeof why), "L01", "NOT_SENT record schema", "%s",
          why);
    CHECK(r && strcmp(json_get_str(r, "result"), "NOT_SENT") == 0 &&
              strcmp(json_get_str(r, "reason"), "GW_EXPIRED") == 0 &&
              json_get(r, "lifespan_s")->type == JSON_NULL &&
              json_get(r, "bp_send_return_us")->type == JSON_NULL &&
              json_get(r, "commands")->type == JSON_NULL &&
              json_get(r, "suite_id")->type == JSON_NULL,
          "L01", "NOT_SENT values (GW_EXPIRED)");
    json_free(r);
    j = parse_str("{\"kind\":\"telemetry\",\"text\":\"T\",\"lifetime_s\":5,\"label\":\"ov\"}");
    earth_build(ctx, j, T0, &it, err, sizeof err);
    json_free(j);
    earth_log_send(&w, &lc, &it, T0, 1, -1, T0 + 5, "BP_SEND_FAILED");
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    CHECK(r && strcmp(json_get_str(r, "result"), "NOT_SENT") == 0 &&
              strcmp(json_get_str(r, "reason"), "BP_SEND_FAILED") == 0 &&
              strcmp(json_get_str(r, "lifespan_rule"), "OVERRIDE") == 0 &&
              json_get(r, "lifespan_s")->integer == 5 &&
              json_get(r, "frame_types")->items[0]->integer == 1,
          "L01", "bp_send failure values (OVERRIDE)");
    json_free(r);
    earth_log_plan(&w, &lc, &it, T0);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    CHECK(r && strcmp(json_get_str(r, "event"), "x_plan") == 0 &&
              strcmp(json_get_str(r, "result"), "TO_SEND") == 0 &&
              json_get(r, "lifespan_s")->integer == 5 &&
              schema_ok(r, F_ENV, F_SEND, NULL, why, sizeof why),
          "L01", "plan dry-run record");
    json_free(r);
    j = parse_str("{\"kind\":\"heartbeat\"}");
    earth_build(ctx, j, T0, &it, err, sizeof err);
    json_free(j);
    earth_log_send(&w, &lc, &it, T0, 1, 1, T0 + 5, NULL);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    CHECK(r && json_get(r, "frame_types")->type == JSON_ARRAY &&
              json_get(r, "frame_types")->count == 0,
          "L01", "Size 0: frame_types []");
    json_free(r);

    /* recv and exec. */
    j = parse_str("{\"kind\":\"command\",\"command\":\"DRIVE 10\",\"label\":\"x\"}");
    earth_build(ctx, j, T0, &it, err, sizeof err);
    json_free(j);
    pbs_replay_t *rep = malloc(sizeof *rep);
    pbs_replay_init(rep);
    pbs_rx_t rx;
    pbs_receive(pol, rep, it.env, it.len, T0 + 1600000, &rx);
    earth_bundle_meta_t meta;
    memset(&meta, 0, sizeof meta);
    meta.have_bundle = 1;
    meta.bundle_source_eid = "ipn:23.1";
    meta.creation_ms = 820540800123ULL;
    meta.creation_count = 4;
    meta.ttl_field_ms = 29000;
    meta.expected_eid = "ipn:23.1";
    meta.have_duplicates = 1;
    earth_log_recv(&w, &lc, &rx, it.env, it.len, NULL, T0 + 1600000, &meta);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    check(schema_ok(r, F_ENV, F_RECV, NULL, why, sizeof why), "L02", "recv record schema", "%s", why);
    CHECK(r && strcmp(json_get_str(r, "result"), "ACCEPT") == 0 &&
              json_get(r, "latency_us")->integer == 1600000 &&
              json_get(r, "bundle_creation_us")->integer == 1767225600123000LL &&
              json_get(r, "bundle_creation_ms")->integer == 820540800123LL &&
              json_get_bool(r, "eid_match", 0) == 1 && json_get_bool(r, "bundle_admin_record", 1) == 0 &&
              json_get(r, "commands")->count == 1 &&
              strcmp(json_get_str(r, "role"), "FLIGHT-OPS") == 0 &&
              strcmp(json_get_str(r, "authority_id"), "PBS-DEMO-MCC") == 0 &&
              json_get(r, "policy_epoch")->integer == 1 &&
              json_get(r, "frame_types")->count == 3,
          "L02", "recv record values");
    json_free(r);
    earth_log_exec(&w, &lc, &rx, it.env, 1, T0 + 1600100);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    check(schema_ok(r, F_EXEC, NULL, NULL, why, sizeof why), "L02", "exec record schema", "%s", why);
    CHECK(r && strcmp(json_get_str(r, "command"), "DRIVE 10") == 0 &&
              strcmp(json_get_str(r, "operation"), "DRIVE") == 0 &&
              strcmp(json_get_str(r, "action"), "LOGGED") == 0,
          "L02", "exec record values");
    json_free(r);

    /* Hostile bytes in key_id: output stays valid JSON/UTF-8; R9 fields logged. */
    uint8_t *b = malloc(PBS_MAX_ENVELOPE);
    memcpy(b, it.env, it.len);
    size_t kid = it.len - 64 - 15;  /* key_id offset within the envelope */
    b[kid] = 0xFF;
    b[kid + 1] = '"';
    b[kid + 2] = 0x01;
    pbs_receive(pol, rep, b, it.len, T0 + 1600000, &rx);
    earth_log_recv(&w, &lc, &rx, b, it.len, NULL, T0 + 1600000, NULL);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    CHECK(r != NULL && utf8_ok(jw_cstr(&w)), "L03", "hostile key_id bytes logged as valid JSON");
    CHECK(r && strcmp(json_get_str(r, "reason"), "SEC_KEY_UNKNOWN") == 0 &&
              json_get(r, "key_id")->type == JSON_STRING &&
              json_get(r, "commands")->type == JSON_NULL &&
              json_get(r, "role")->type == JSON_NULL &&
              json_get(r, "bundle_source_eid")->type == JSON_NULL &&
              schema_ok(r, F_ENV, F_RECV, NULL, why, sizeof why),
          "L03", "reason SEC_KEY_UNKNOWN, key_id logged, later fields null");
    json_free(r);
    /* Header-less record. */
    pbs_receive(pol, rep, b, 10, T0, &rx);
    earth_log_recv(&w, &lc, &rx, b, 10, NULL, T0, NULL);
    r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
    CHECK(r && json_get(r, "source_id")->type == JSON_NULL &&
              json_get(r, "latency_us")->type == JSON_NULL &&
              json_get(r, "frame_types")->type == JSON_NULL &&
              json_get(r, "envelope_len")->integer == 10 &&
              strcmp(json_get_str(r, "reason"), "LENGTH") == 0 &&
              schema_ok(r, F_ENV, F_RECV, NULL, why, sizeof why),
          "L04", "10-byte payload record");
    json_free(r);
    /* Timestamps of 2^63 and above: latency_us is t_us - Timestamp when that
     * fits in 64 bits and null otherwise, never an overflowed value (the
     * subtraction is done in 128 bits). */
    {
        static const uint64_t ts[] = {0x8000000000000000ULL, 0xFFFFFFFFFFFFFFFFULL,
                                      0x7FFFFFFFFFFFFFFFULL};
        for (size_t i = 0; i < sizeof ts / sizeof ts[0]; i++) {
            size_t hn = mk_env(b, 2, "Rover-Alpha", ts[i], 30, NULL, 0);
            pbs_receive(pol, rep, b, hn, T0, &rx);
            earth_log_recv(&w, &lc, &rx, b, hn, NULL, T0, NULL);
            r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
            const json_value_t *lat = r ? json_get(r, "latency_us") : NULL;
            /* T0 - 2^63 and T0 - (2^63 - 1) fit in 64 bits; T0 - (2^64 - 1)
             * does not and is logged as null. */
            int64_t want = i == 0 ? T0 - INT64_MAX - 1 : T0 - INT64_MAX;
            int ok = lat && (i == 1 ? lat->type == JSON_NULL
                                    : lat->type != JSON_NULL && lat->integer == want);
            snprintf(why, sizeof why, "Timestamp 0x%016llx: latency_us %s", (unsigned long long)ts[i],
                     i == 1 ? "null (outside 64 bits)" : "t_us - Timestamp");
            CHECK(ok, "L04", why);
            json_free(r);
        }
    }
    /* Oversize payload: header read, digest supplied by the caller. */
    {
        uint8_t big[PBS_HEADER_SIZE], md[32];
        memcpy(big, it.env, PBS_HEADER_SIZE);
        memset(md, 0xAB, sizeof md);
        pbs_receive_ex(pol, rep, big, PBS_HEADER_SIZE, 100000, T0, &rx);
        earth_log_recv(&w, &lc, &rx, big, PBS_HEADER_SIZE, md, T0, NULL);
        r = json_parse(jw_cstr(&w), w.len, err, sizeof err);
        CHECK(r && json_get(r, "envelope_len")->integer == 100000 &&
                  json_get(r, "envelope_hex")->type == JSON_NULL &&
                  strncmp(json_get_str(r, "envelope_sha256"), "abab", 4) == 0 &&
                  strcmp(json_get_str(r, "reason"), "LENGTH") == 0,
              "L04", "oversize payload record");
        json_free(r);
    }

    /* Duplicate identities (A5). */
    earth_idset_t ids;
    earth_idset_init(&ids);
    pbs_header_t h;
    memset(&h, 0, sizeof h);
    uint32_t dup = 0;
    for (int i = 0; i < 5000; i++) {
        h.sequence = (uint16_t)i;
        dup += earth_idset_add(&ids, &h);
    }
    h.sequence = 42;
    CHECK(dup == 0 && earth_idset_add(&ids, &h) == 1 && earth_idset_add(&ids, &h) == 2, "L05",
          "identity set counts repeats");
    earth_idset_free(&ids);

    for (int i = 0; i < PBS_R_COUNT_; i++)
        CHECK(pbs_reason_from_str(pbs_reason_str((pbs_reason_t)i)) == i, "L06", "reason names");

    /* Counter state across invocations (--state). */
    {
        char path[] = "/tmp/pbs_earth_state_XXXXXX";
        int fd = mkstemp(path);
        if (fd >= 0)
            close(fd);
        remove(path);
        earth_ctx_t a, b2;
        earth_ctx_init(&a, "Earth-MCC", "ipn:99.1");
        earth_ctx_init(&b2, "Earth-MCC", "ipn:99.1");
        CHECK(earth_state_load(&a, path, err, sizeof err) == 0 && a.next_sequence == 1, "L08",
              "missing state file: counters start at 1");
        json_value_t *c3 = parse_str("{\"kind\":\"command\",\"command\":\"PING\"}");
        for (int i = 0; i < 3; i++)
            earth_build(&a, c3, T0, &it, err, sizeof err);
        CHECK(earth_state_save(&a, path) == 0, "L08", "state saved");
        CHECK(earth_state_load(&b2, path, err, sizeof err) == 0 && b2.next_sequence == 4, "L08",
              "Sequence continues");
        earth_build(&b2, c3, T0, &it, err, sizeof err);
        CHECK(it.security_sequence == 4 && it.hdr.sequence == 4, "L08",
              "security_sequence continues in a second process");
        earth_ctx_t c4;
        earth_ctx_init(&c4, "Other-ID", "ipn:99.1");
        CHECK(earth_state_load(&c4, path, err, sizeof err) < 0, "L08",
              "state of another Source ID refused");
        json_free(c3);
        earth_ctx_free(&a);
        earth_ctx_free(&b2);
        earth_ctx_free(&c4);
        remove(path);
    }

    /* JSON reader corner cases. */
    const char *badj[] = {"{", "[1,]", "{\"a\":}", "\"\\ud800\"", "01", "1.", "tru", "\"\x01\""};
    int okbad = 1;
    for (size_t i = 0; i < sizeof badj / sizeof badj[0]; i++) {
        json_value_t *v = json_parse(badj[i], strlen(badj[i]), err, sizeof err);
        if (v) {
            okbad = 0;
            json_free(v);
        }
    }
    CHECK(okbad, "L07", "malformed JSON rejected");
    json_value_t *v = parse_str("{\"s\":\"\\u00dc\\ud83d\\ude00\",\"n\":-12,\"f\":1.5e3}");
    CHECK(v && strcmp(json_get_str(v, "s"), "\xc3\x9c\xf0\x9f\x98\x80") == 0 &&
              json_get(v, "n")->integer == -12 && json_get(v, "f")->number == 1500.0,
          "L07", "escapes and numbers");
    json_free(v);

    free(b);
    free(rep);
    jw_free(&w);
    earth_item_free(&it);
    summary("log records and JSON", p0, f0);
}

int main(int argc, char **argv)
{
    const char *path = NULL, *report = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--report") == 0 && i + 1 < argc)
            report = argv[++i];
        else
            path = argv[i];
    }
    if (!path) {
        fprintf(stderr, "usage: test_pbs <test-vectors.json> [--report FILE]\n");
        return 2;
    }
    size_t len;
    char *text = json_read_file(path, &len);
    if (!text) {
        perror(path);
        return 2;
    }
    char err[160];
    json_value_t *root = json_parse(text, len, err, sizeof err);
    free(text);
    if (!root) {
        fprintf(stderr, "%s: %s\n", path, err);
        return 2;
    }
    if (report) {
        g_report = fopen(report, "w");
        if (!g_report) {
            perror(report);
            return 2;
        }
    }

    pbs_policy_t pol;
    earth_ctx_t ctx;
    if (pbs_policy_demo(&pol) != PBS_E_OK || earth_ctx_init(&ctx, "Earth-MCC", "ipn:99.1") != 0) {
        fprintf(stderr, "key setup failed\n");
        return 2;
    }

    printf("Endpoint B unit tests, vectors: %s\n", path);
    t_constants(root);
    t_envelopes(root, &ctx, &pol);
    t_receiver_cases(root, &pol);
    t_lifetime(root);
    t_cos(root);
    t_keys_policy(root, &pol);
    t_secb_messages(root, &ctx);
    t_secb_cases(root, &pol);
    t_negative(&pol, &ctx);
    t_mutation(root, &pol);
    t_plan(&ctx, &pol);
    t_logs(&ctx, &pol);
    printf("TOTAL: %d checks passed, %d failed\n", g_pass, g_fail);

    earth_ctx_free(&ctx);
    json_free(root);
    if (g_report)
        fclose(g_report);
    return g_fail ? 1 : 0;
}
