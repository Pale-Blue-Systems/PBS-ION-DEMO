/*
 * earth.c - Endpoint B message construction and log records. See earth.h.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#include "earth.h"

#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <openssl/evp.h>

#define MAX_ITEM_FRAMES 32

static void seterr(char *err, size_t cap, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

static void seterr(char *err, size_t cap, const char *fmt, ...)
{
    if (!err || !cap)
        return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, cap, fmt, ap);
    va_end(ap);
}

static void copy_str(char *dst, size_t cap, const char *src)
{
    if (!cap)
        return;
    if (!src)
        src = "";
    snprintf(dst, cap, "%s", src);
}

int64_t earth_now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* ---- Context ------------------------------------------------------------------- */

int earth_ctx_init(earth_ctx_t *c, const char *source_id, const char *default_dest)
{
    static const struct {
        const char *id;
        uint16_t suite;
    } keys[] = {
        {"TEST-ED25519-01", PBS_SUITE_ED25519},
        {"TEST-HMAC-01", PBS_SUITE_HMAC_SHA256},
        {"TEST-ED25519-ROGUE", PBS_SUITE_ED25519},
        {"TEST-HMAC-ROGUE", PBS_SUITE_HMAC_SHA256},
    };
    uint8_t raw[PBS_SOURCE_ID_LEN];
    memset(c, 0, sizeof *c);
    if (!source_id || pbs_source_id_encode(source_id, raw) != PBS_E_OK)
        return -1;
    copy_str(c->source_id, sizeof c->source_id, source_id);
    copy_str(c->default_dest, sizeof c->default_dest, default_dest ? default_dest : "ipn:99.1");
    c->next_sequence = 1;
    for (size_t i = 0; i < sizeof keys / sizeof keys[0]; i++) {
        if (pbs_test_key_derive(keys[i].id, keys[i].suite, &c->keys[c->nkeys]) != PBS_E_OK)
            return -1;
        c->nkeys++;
    }
    return 0;
}

void earth_ctx_free(earth_ctx_t *c)
{
    for (size_t i = 0; i < c->nlabeled; i++)
        free(c->labeled[i].bytes);
    free(c->labeled);
    c->labeled = NULL;
    c->nlabeled = 0;
}

const pbs_key_t *earth_find_key(const earth_ctx_t *c, const char *key_id)
{
    for (size_t i = 0; i < c->nkeys; i++)
        if (strcmp(c->keys[i].key_id, key_id) == 0)
            return &c->keys[i];
    return NULL;
}

static int next_secseq(earth_ctx_t *c, const char *key_id, uint64_t *out)
{
    for (size_t i = 0; i < c->nsecseq; i++)
        if (strcmp(c->secseq[i].key_id, key_id) == 0) {
            *out = c->secseq[i].next++;
            return 0;
        }
    if (c->nsecseq >= EARTH_MAX_SECSEQ)
        return -1;
    earth_secseq_t *s = &c->secseq[c->nsecseq++];
    copy_str(s->key_id, sizeof s->key_id, key_id);
    s->next = 1;                         /* digest 6.4: first value 1 */
    *out = s->next++;
    return 0;
}

static int store_label(earth_ctx_t *c, const char *label, const uint8_t *b, size_t n)
{
    uint8_t *copy = malloc(n ? n : 1);
    if (!copy)
        return -1;
    memcpy(copy, b, n);
    for (size_t i = 0; i < c->nlabeled; i++)
        if (strcmp(c->labeled[i].label, label) == 0) {
            free(c->labeled[i].bytes);
            c->labeled[i].bytes = copy;
            c->labeled[i].len = n;
            return 0;
        }
    earth_labeled_t *nl = realloc(c->labeled, (c->nlabeled + 1) * sizeof *nl);
    if (!nl) {
        free(copy);
        return -1;
    }
    c->labeled = nl;
    copy_str(nl[c->nlabeled].label, sizeof nl[0].label, label);
    nl[c->nlabeled].bytes = copy;
    nl[c->nlabeled].len = n;
    c->nlabeled++;
    return 0;
}

static const earth_labeled_t *find_label(const earth_ctx_t *c, const char *label)
{
    for (size_t i = 0; i < c->nlabeled; i++)
        if (strcmp(c->labeled[i].label, label) == 0)
            return &c->labeled[i];
    return NULL;
}

int earth_item_alloc(earth_item_t *it)
{
    memset(it, 0, sizeof *it);
    it->env = malloc(PBS_MAX_ENVELOPE + 1);
    return it->env ? 0 : -1;
}

void earth_item_free(earth_item_t *it)
{
    free(it->env);
    it->env = NULL;
}

static void item_reset(earth_item_t *it)
{
    uint8_t *env = it->env;
    memset(it, 0, sizeof *it);
    it->env = env;
}

/* ---- Field helpers ------------------------------------------------------------------ */

long earth_parse_uint(const json_value_t *v)
{
    if (!v)
        return -1;
    if (v->type == JSON_NUMBER && v->is_integer && v->integer >= 0)
        return (long)v->integer;
    if (v->type == JSON_STRING && v->string_len > 0) {
        char *end = NULL;
        long r = strtol(v->string, &end, 0);
        if (end && *end == '\0' && r >= 0)
            return r;
    }
    return -1;
}

/* Integer member in [lo, hi]; def when absent. -1 on a bad value. */
static int get_ranged(const json_value_t *o, const char *k, int64_t lo, int64_t hi,
                      int64_t def, int64_t *out, char *err, size_t ecap)
{
    const json_value_t *v = json_get(o, k);
    if (!v) {
        *out = def;
        return 0;
    }
    int64_t x;
    if (v->type == JSON_NUMBER && v->is_integer)
        x = v->integer;
    else if (v->type == JSON_STRING) {
        long r = earth_parse_uint(v);
        if (r < 0) {
            seterr(err, ecap, "bad value for \"%s\"", k);
            return -1;
        }
        x = r;
    } else {
        seterr(err, ecap, "bad value for \"%s\"", k);
        return -1;
    }
    if (x < lo || x > hi) {
        seterr(err, ecap, "\"%s\" out of range", k);
        return -1;
    }
    *out = x;
    return 0;
}

static int parse_suite(const json_value_t *v, uint16_t *out)
{
    if (!v)
        return -1;
    if (v->type == JSON_STRING) {
        if (strcmp(v->string, "ed25519") == 0) {
            *out = PBS_SUITE_ED25519;
            return 0;
        }
        if (strcmp(v->string, "hmac") == 0 || strcmp(v->string, "hmac-sha256") == 0) {
            *out = PBS_SUITE_HMAC_SHA256;
            return 0;
        }
    }
    long s = earth_parse_uint(v);
    if (s < 0 || s > 0xFFFF)
        return -1;
    *out = (uint16_t)s;
    return 0;
}

/* Frame list storage for one build. */
typedef struct {
    pbs_frame_in_t f[MAX_ITEM_FRAMES];
    uint8_t       *owned[MAX_ITEM_FRAMES];
    size_t         n;
} frames_t;

static void frames_free(frames_t *fr)
{
    for (size_t i = 0; i < fr->n; i++)
        free(fr->owned[i]);
    fr->n = 0;
}

static int frames_add(frames_t *fr, uint8_t type, const uint8_t *v, size_t len)
{
    if (fr->n >= MAX_ITEM_FRAMES || len > 0xFFFF)
        return -1;
    uint8_t *copy = malloc(len ? len : 1);
    if (!copy)
        return -1;
    if (len)
        memcpy(copy, v, len);
    fr->owned[fr->n] = copy;
    fr->f[fr->n].type = type;
    fr->f[fr->n].value = copy;
    fr->f[fr->n].length = len;
    fr->n++;
    return 0;
}

static int frames_add_hex(frames_t *fr, uint8_t type, const char *hex)
{
    size_t n = strlen(hex) / 2;
    uint8_t *buf = malloc(n ? n : 1);
    if (!buf)
        return -1;
    long r = pbs_hex_decode(hex, buf, n);
    int rc = (r < 0) ? -1 : frames_add(fr, type, buf, (size_t)r);
    free(buf);
    return rc;
}

/* JSON frame list: [{"type": 1 | "0x01", "text" | "hex" | "value_hex": ...}] */
static int frames_from_json(frames_t *fr, const json_value_t *arr, char *err, size_t ecap)
{
    if (!arr || arr->type != JSON_ARRAY) {
        seterr(err, ecap, "frames must be an array");
        return -1;
    }
    for (size_t i = 0; i < arr->count; i++) {
        const json_value_t *f = arr->items[i];
        long t = earth_parse_uint(json_get(f, "type"));
        if (t < 0 || t > 255) {
            seterr(err, ecap, "frame %zu: bad type", i);
            return -1;
        }
        const char *text = json_get_str(f, "text");
        const char *hex = json_get_str(f, "hex");
        if (!hex)
            hex = json_get_str(f, "value_hex");
        int rc;
        if (text)
            rc = frames_add(fr, (uint8_t)t, (const uint8_t *)text, strlen(text));
        else if (hex)
            rc = frames_add_hex(fr, (uint8_t)t, hex);
        else
            rc = frames_add(fr, (uint8_t)t, NULL, 0);
        if (rc < 0) {
            seterr(err, ecap, "frame %zu: bad value", i);
            return -1;
        }
    }
    return 0;
}

/* Text value padded to pad_to bytes with the repeating pattern "0123456789". */
static int frames_add_text(frames_t *fr, uint8_t type, const char *text, int64_t pad_to)
{
    size_t tl = strlen(text);
    size_t n = (pad_to > 0 && (size_t)pad_to > tl) ? (size_t)pad_to : tl;
    if (n > 0xFFFF)
        return -1;
    uint8_t *buf = malloc(n ? n : 1);
    if (!buf)
        return -1;
    memcpy(buf, text, tl);
    for (size_t i = tl; i < n; i++)
        buf[i] = (uint8_t)('0' + (i - tl) % 10);
    int rc = frames_add(fr, type, buf, n);
    free(buf);
    return rc;
}

/* ---- Tampering (S6 fault injection; digest 9) ------------------------------------- */

static int find_frame(const uint8_t *env, size_t len, uint8_t type, pbs_mux_t *m,
                      const pbs_frame_t **out)
{
    if (len <= PBS_HEADER_SIZE)
        return -1;
    if (pbs_mux_parse(env + PBS_HEADER_SIZE, len - PBS_HEADER_SIZE, m) != PBS_R_OK)
        return -1;
    for (unsigned i = 0; i < m->count; i++)
        if (m->frames[i].type == type) {
            *out = &m->frames[i];
            return 0;
        }
    return -1;
}

static int apply_tamper(earth_item_t *o, const char *mode, char *err, size_t ecap)
{
    pbs_mux_t *m = NULL;
    const pbs_frame_t *f = NULL;
    uint8_t *env = o->env;
    int rc = -1;
    if (!mode || !*mode || strcmp(mode, "none") == 0)
        return 0;
    if (o->len < PBS_HEADER_SIZE) {
        seterr(err, ecap, "tamper needs a header");
        return -1;
    }
    m = malloc(sizeof *m);
    if (!m)
        return -1;
    if (strcmp(mode, "command") == 0) {
        /* Change the last byte of the first Command frame value; header
         * and CRC32 unchanged (vector CMD-03 style). */
        if (find_frame(env, o->len, PBS_FRAME_COMMAND, m, &f) == 0 && f->length > 0) {
            env[PBS_HEADER_SIZE + f->value_offset + f->length - 1] ^= 0x08;
            rc = 0;
        }
    } else if (strcmp(mode, "authority") == 0) {
        if (find_frame(env, o->len, PBS_FRAME_AUTHORITY, m, &f) == 0 && f->length > 0) {
            env[PBS_HEADER_SIZE + f->value_offset + f->length - 1] ^= 0x01;
            rc = 0;
        }
    } else if (strcmp(mode, "priority") == 0) {
        env[1] = env[1] > 0 ? (uint8_t)(env[1] - 1) : 1;   /* CRC recomputed */
        pbs_header_seal(env);
        rc = 0;
    } else if (strcmp(mode, "ttl") == 0) {
        uint32_t ttl = pbs_get_u32(env + 0x24);
        pbs_put_u32(env + 0x24, ttl > 0xFFFFFFFFu - 3600u ? 0 : ttl + 3600u);
        pbs_header_seal(env);
        rc = 0;
    } else if (strcmp(mode, "timestamp") == 0) {
        pbs_put_u64(env + 0x18, pbs_get_u64(env + 0x18) + 10000000u);
        pbs_header_seal(env);
        rc = 0;
    } else if (strcmp(mode, "security_sequence") == 0) {
        if (find_frame(env, o->len, PBS_FRAME_SECURITY, m, &f) == 0 && f->length >= 16) {
            uint8_t *v = env + PBS_HEADER_SIZE + f->value_offset;
            pbs_put_u64(v + 8, pbs_get_u64(v + 8) + 1);
            rc = 0;
        }
    } else if (strcmp(mode, "auth_data") == 0) {
        if (o->len > PBS_HEADER_SIZE &&
            find_frame(env, o->len, PBS_FRAME_SECURITY, m, &f) == 0) {
            env[o->len - 1] ^= 0x01;          /* last byte of auth_data */
            rc = 0;
        }
    } else if (strcmp(mode, "crc") == 0) {
        env[0x2B] ^= 0x01;
        rc = 0;
    } else {
        seterr(err, ecap, "unknown tamper mode \"%s\"", mode);
        free(m);
        return -1;
    }
    free(m);
    if (rc < 0)
        seterr(err, ecap, "tamper \"%s\": envelope has no such field", mode);
    return rc;
}

/* ---- Building ------------------------------------------------------------------------ */

typedef struct {
    int         with_security;
    uint16_t    suite_id;
    char        key_id[PBS_KEY_ID_MAX + 1];
    const pbs_key_t *signing_key;
    uint64_t    security_sequence;
} secparams_t;

static int build_core(const pbs_header_t *h, const frames_t *fr, int use_mux,
                      const secparams_t *sp, uint8_t *out, size_t cap, size_t *len,
                      char *err, size_t ecap)
{
    int rc;
    if (!use_mux) {
        pbs_header_t hh = *h;
        rc = pbs_envelope_build(&hh, NULL, 0, out, cap, len);
    } else {
        pbs_protected_spec_t s;
        memset(&s, 0, sizeof s);
        s.hdr = *h;
        s.frames = fr->f;
        s.nframes = fr->n;
        if (sp && sp->with_security) {
            s.with_security = 1;
            s.suite_id = sp->suite_id;
            s.key_id = sp->key_id;
            s.security_sequence = sp->security_sequence;
            s.signing_key = sp->signing_key;
        }
        rc = pbs_build_protected(&s, out, cap, len, NULL);
    }
    if (rc != PBS_E_OK)
        seterr(err, ecap, "envelope build failed (%d)", rc);
    return rc == PBS_E_OK ? 0 : -1;
}

static int security_from_json(const earth_ctx_t *c, const json_value_t *o,
                              const char *suite_key, const char *seq_key,
                              secparams_t *sp, int *seq_given, char *err, size_t ecap)
{
    uint16_t suite = PBS_SUITE_ED25519;
    const json_value_t *sv = json_get(o, suite_key);
    if (!sv && strcmp(suite_key, "suite") == 0)
        sv = json_get(o, "suite_id");
    if (sv && parse_suite(sv, &suite) != 0) {
        seterr(err, ecap, "bad suite");
        return -1;
    }
    const char *kid = json_get_str(o, "key_id");
    if (!kid)
        kid = (suite == PBS_SUITE_HMAC_SHA256) ? "TEST-HMAC-01" : "TEST-ED25519-01";
    const char *sk = json_get_str(o, "signing_key");
    if (!sk)
        sk = kid;
    size_t kl = strlen(kid);
    if (kl < 1 || kl > PBS_KEY_ID_MAX) {
        seterr(err, ecap, "bad key_id");
        return -1;
    }
    sp->signing_key = earth_find_key(c, sk);
    if (!sp->signing_key) {
        seterr(err, ecap, "no signing key \"%s\"", sk);
        return -1;
    }
    sp->with_security = 1;
    sp->suite_id = suite;
    copy_str(sp->key_id, sizeof sp->key_id, kid);
    *seq_given = 0;
    const json_value_t *qv = json_get(o, seq_key);
    if (qv) {
        if (qv->type != JSON_NUMBER || !qv->is_integer || qv->integer < 0) {
            seterr(err, ecap, "bad security_sequence");
            return -1;
        }
        sp->security_sequence = (uint64_t)qv->integer;
        *seq_given = 1;
    }
    return 0;
}

int earth_build_from_inputs(const earth_ctx_t *c, const json_value_t *in,
                            const json_value_t *sec, uint8_t *out, size_t cap,
                            size_t *out_len, char *err, size_t ecap)
{
    pbs_header_t h;
    int64_t v;
    memset(&h, 0, sizeof h);
    const char *sid = json_get_str(in, "source_id");
    const char *sid_hex = json_get_str(in, "source_id_hex");
    if (sid_hex) {
        if (pbs_hex_decode(sid_hex, h.source_id, PBS_SOURCE_ID_LEN) != PBS_SOURCE_ID_LEN) {
            seterr(err, ecap, "bad source_id_hex");
            return -1;
        }
    } else if (!sid || pbs_source_id_encode(sid, h.source_id) != PBS_E_OK) {
        seterr(err, ecap, "bad source_id");
        return -1;
    }
    if (get_ranged(in, "priority", 0, 4, 0, &v, err, ecap) < 0)
        return -1;
    h.priority = (uint8_t)v;
    if (get_ranged(in, "flags", 0, 255, 0, &v, err, ecap) < 0)
        return -1;
    h.flags = (uint8_t)v;
    if (get_ranged(in, "sequence", 0, 65535, 0, &v, err, ecap) < 0)
        return -1;
    h.sequence = (uint16_t)v;
    if (json_get_int(in, "timestamp_us", &v) < 0 || v < 0) {
        seterr(err, ecap, "bad timestamp_us");
        return -1;
    }
    h.timestamp_us = (uint64_t)v;
    if (get_ranged(in, "ttl_s", 0, 4294967295LL, 0, &v, err, ecap) < 0)
        return -1;
    h.ttl = (uint32_t)v;

    const json_value_t *fl = json_get(in, "frames_before_security_frame");
    if (fl) {
        frames_t fr;
        secparams_t sp;
        int given = 0;
        memset(&fr, 0, sizeof fr);
        memset(&sp, 0, sizeof sp);
        int rc = frames_from_json(&fr, fl, err, ecap);
        if (rc == 0 && sec)
            rc = security_from_json(c, sec, "suite_id", "security_sequence", &sp, &given,
                                    err, ecap);
        if (rc == 0 && sec && !given) {
            seterr(err, ecap, "security_sequence required");
            rc = -1;
        }
        if (rc == 0)
            rc = build_core(&h, &fr, 1, &sp, out, cap, out_len, err, ecap);
        frames_free(&fr);
        return rc;
    }
    const char *ph = json_get_str(in, "payload_hex");
    if (!ph)
        ph = "";
    size_t pl = strlen(ph) / 2;
    uint8_t *pb = malloc(pl ? pl : 1);
    if (!pb)
        return -1;
    long r = pbs_hex_decode(ph, pb, pl);
    int rc = -1;
    if (r < 0)
        seterr(err, ecap, "bad payload_hex");
    else if (pbs_envelope_build(&h, pb, (size_t)r, out, cap, out_len) != PBS_E_OK)
        seterr(err, ecap, "envelope build failed");
    else
        rc = 0;
    free(pb);
    return rc;
}

static int build_new(earth_ctx_t *c, const json_value_t *item, int64_t now_us,
                     earth_item_t *o, char *err, size_t ecap)
{
    const char *kind = o->kind;
    int is_cmd = strcmp(kind, "command") == 0;
    int is_hb = strcmp(kind, "heartbeat") == 0;
    uint8_t ftype = 0;
    if (strcmp(kind, "telemetry") == 0)
        ftype = PBS_FRAME_TELEMETRY;
    else if (strcmp(kind, "status") == 0)
        ftype = PBS_FRAME_STATUS;
    else if (strcmp(kind, "event") == 0)
        ftype = PBS_FRAME_EVENT;
    else if (!is_cmd && !is_hb && strcmp(kind, "frames") != 0) {
        seterr(err, ecap, "unknown kind \"%s\"", kind);
        return -1;
    }

    pbs_header_t h;
    int64_t v;
    memset(&h, 0, sizeof h);
    if (get_ranged(item, "priority", 0, 4, is_cmd ? 1 : 2, &v, err, ecap) < 0)
        return -1;
    h.priority = (uint8_t)v;
    if (get_ranged(item, "flags", 0, 255, 0, &v, err, ecap) < 0)
        return -1;
    h.flags = (uint8_t)v;
    if (get_ranged(item, "ttl", 0, 4294967295LL, is_cmd ? 30 : 60, &v, err, ecap) < 0)
        return -1;
    h.ttl = (uint32_t)v;
    if (get_ranged(item, "reserved03", 0, 255, 0, &v, err, ecap) < 0)
        return -1;
    h.reserved03 = (uint8_t)v;
    if (get_ranged(item, "reserved06", 0, 65535, 0, &v, err, ecap) < 0)
        return -1;
    h.reserved06 = (uint16_t)v;
    const char *sid = json_get_str(item, "source_id");
    if (pbs_source_id_encode(sid ? sid : c->source_id, h.source_id) != PBS_E_OK) {
        seterr(err, ecap, "Source ID longer than 16 bytes (A4)");
        return -1;
    }
    if (json_get(item, "timestamp_us")) {
        if (json_get_int(item, "timestamp_us", &v) < 0 || v < 0) {
            seterr(err, ecap, "bad timestamp_us");
            return -1;
        }
        h.timestamp_us = (uint64_t)v;
    } else {
        int64_t off = 0;
        if (json_get(item, "timestamp_offset_us") &&
            json_get_int(item, "timestamp_offset_us", &off) < 0) {
            seterr(err, ecap, "bad timestamp_offset_us");
            return -1;
        }
        h.timestamp_us = (uint64_t)(now_us + off);
    }

    frames_t fr;
    secparams_t sp;
    memset(&fr, 0, sizeof fr);
    memset(&sp, 0, sizeof sp);
    int rc = 0, use_mux = !is_hb, seq_given = 0;
    const json_value_t *fl = json_get(item, "frames");
    if (fl) {
        rc = frames_from_json(&fr, fl, err, ecap);
    } else if (ftype) {
        const char *text = json_get_str(item, "text");
        const char *hex = json_get_str(item, "hex");
        int64_t pad = 0;
        if (get_ranged(item, "pad_to", 0, 65000, 0, &pad, err, ecap) < 0)
            rc = -1;
        else if (hex)
            rc = frames_add_hex(&fr, ftype, hex);
        else
            rc = frames_add_text(&fr, ftype, text ? text : "", pad);
        if (rc < 0 && err && !err[0])
            seterr(err, ecap, "bad frame value");
    } else if (is_cmd) {
        const char *cmd = json_get_str(item, "command");
        if (!cmd) {
            seterr(err, ecap, "command item needs \"command\"");
            rc = -1;
        }
        if (rc == 0 && !json_get_bool(item, "omit_auth_ctx", 0)) {
            int64_t t, ep;
            const char *aid = json_get_str(item, "authority_id");
            const char *role = json_get_str(item, "role");
            uint8_t av[8 + 255 + 255];
            size_t al = 0;
            if (get_ranged(item, "authority_id_type", 0, 255, 3, &t, err, ecap) < 0 ||
                get_ranged(item, "policy_epoch", 0, 4294967295LL, 1, &ep, err, ecap) < 0)
                rc = -1;
            else if (pbs_auth_ctx_encode((uint8_t)t, aid ? aid : "PBS-DEMO-MCC",
                                         role ? role : "FLIGHT-OPS", (uint32_t)ep, av,
                                         sizeof av, &al) != PBS_E_OK ||
                     frames_add(&fr, PBS_FRAME_AUTHORITY, av, al) < 0) {
                seterr(err, ecap, "bad authority context");
                rc = -1;
            }
            copy_str(o->role, sizeof o->role, role ? role : "FLIGHT-OPS");
        }
        const json_value_t *pre = json_get(item, "extra_frames");
        if (rc == 0 && pre)
            rc = frames_from_json(&fr, pre, err, ecap);
        if (rc == 0 && frames_add(&fr, PBS_FRAME_COMMAND, (const uint8_t *)cmd, strlen(cmd)) < 0) {
            seterr(err, ecap, "bad command");
            rc = -1;
        }
        if (rc == 0)
            copy_str(o->command, sizeof o->command, cmd);
    }
    int want_sec = (is_cmd && !json_get_bool(item, "unsigned", 0)) ||
                   json_get_bool(item, "sign", 0);
    if (rc == 0 && want_sec)
        rc = security_from_json(c, item, "suite", "security_sequence", &sp, &seq_given,
                                err, ecap);

    /* Counters advance only for an envelope that is actually built. */
    if (rc == 0) {
        if (json_get(item, "sequence")) {
            rc = get_ranged(item, "sequence", 0, 65535, 0, &v, err, ecap);
            h.sequence = (uint16_t)v;
        } else {
            h.sequence = c->next_sequence;
        }
    }
    if (rc == 0 && sp.with_security && !seq_given &&
        next_secseq(c, sp.key_id, &sp.security_sequence) < 0) {
        seterr(err, ecap, "too many key_ids");
        rc = -1;
    }
    if (rc == 0)
        rc = build_core(&h, &fr, use_mux, &sp, o->env, PBS_MAX_ENVELOPE, &o->len, err, ecap);
    if (rc == 0 && !json_get(item, "sequence"))
        c->next_sequence = (uint16_t)(c->next_sequence + 1);  /* 65535 -> 0 */
    if (rc == 0 && sp.with_security) {
        o->has_security = 1;
        o->suite_id = sp.suite_id;
        copy_str(o->key_id, sizeof o->key_id, sp.key_id);
        copy_str(o->signing_key, sizeof o->signing_key, sp.signing_key->key_id);
        o->security_sequence = sp.security_sequence;
    }
    frames_free(&fr);
    return rc;
}

int earth_build(earth_ctx_t *c, const json_value_t *item, int64_t now_us,
                earth_item_t *o, char *err, size_t ecap)
{
    if (err && ecap)
        err[0] = '\0';
    item_reset(o);
    if (!o->env) {
        seterr(err, ecap, "item buffer not allocated");
        return -1;
    }
    if (!item || item->type != JSON_OBJECT) {
        seterr(err, ecap, "plan item must be an object");
        return -1;
    }
    const char *kind = json_get_str(item, "kind");
    copy_str(o->kind, sizeof o->kind, kind ? kind : "telemetry");
    copy_str(o->label, sizeof o->label, json_get_str(item, "label"));
    copy_str(o->tamper, sizeof o->tamper, json_get_str(item, "tamper"));
    copy_str(o->note, sizeof o->note, json_get_str(item, "note"));
    const char *dest = json_get_str(item, "dest");
    copy_str(o->dest, sizeof o->dest, dest ? dest : c->default_dest);

    int built_new = 0;
    if (strcmp(o->kind, "replay") == 0) {
        const char *ro = json_get_str(item, "replay_of");
        const earth_labeled_t *l = ro ? find_label(c, ro) : NULL;
        if (!l) {
            seterr(err, ecap, "replay_of: unknown label \"%s\"", ro ? ro : "");
            return -1;
        }
        copy_str(o->replay_of, sizeof o->replay_of, ro);
        memcpy(o->env, l->bytes, l->len);
        o->len = l->len;
    } else if (strcmp(o->kind, "raw") == 0) {
        const char *hex = json_get_str(item, "hex");
        if (!hex)
            hex = json_get_str(item, "envelope_hex");
        long r = hex ? pbs_hex_decode(hex, o->env, PBS_MAX_ENVELOPE + 1) : -1;
        if (r < 0) {
            seterr(err, ecap, "raw item needs valid \"hex\"");
            return -1;
        }
        o->len = (size_t)r;
    } else {
        if (build_new(c, item, now_us, o, err, ecap) < 0)
            return -1;
        built_new = 1;
    }
    (void)built_new;

    /* The gateway decision uses the envelope as built (or replayed); a
     * tamper mode is applied afterwards and stands for a modification in
     * transit. */
    pbs_header_t pre;
    int have_pre = o->len >= PBS_HEADER_SIZE;
    if (have_pre)
        pbs_header_read(o->env, &pre);

    if (apply_tamper(o, o->tamper, err, ecap) < 0)
        return -1;

    if (o->len >= PBS_HEADER_SIZE) {
        o->have_header = 1;
        pbs_header_read(o->env, &o->hdr);
        if (!o->has_security) {
            /* Report security fields of replayed or raw protected envelopes. */
            pbs_mux_t *m = malloc(sizeof *m);
            const pbs_frame_t *f = NULL;
            pbs_sec_frame_t s;
            if (m && find_frame(o->env, o->len, PBS_FRAME_SECURITY, m, &f) == 0 &&
                pbs_sec_frame_parse(o->env + PBS_HEADER_SIZE + f->value_offset, f->length,
                                    &s) == 0) {
                o->has_security = 1;
                o->suite_id = s.suite_id;
                size_t kl = s.key_id_len;
                memcpy(o->key_id, s.key_id, kl);
                o->key_id[kl] = '\0';
                o->security_sequence = s.security_sequence;
            }
            free(m);
        }
    }

    /* Gateway decision (digest 7.4). */
    int64_t ov = 0;
    if (json_get(item, "lifetime_s")) {
        if (json_get_int(item, "lifetime_s", &ov) < 0 || ov < 1 ||
            ov > PBS_NO_EXPIRY_LIFETIME_S) {
            seterr(err, ecap, "bad lifetime_s");
            return -1;
        }
    }
    if (have_pre)
        pbs_lifetime(pre.ttl, pre.timestamp_us, now_us, &o->lt);
    if (ov > 0) {
        /* Test-only fault injection: overrides the PBS-DTN-MAP-01 6.1
         * bound so that an expired envelope reaches a receiver. */
        o->lifetime_override = 1;
        o->lifespan_s = ov;
        o->send = 1;
    } else if (!have_pre) {
        seterr(err, ecap, "item shorter than 44 bytes needs lifetime_s");
        return -1;
    } else if (o->lt.outcome == PBS_LT_OK || o->lt.outcome == PBS_LT_NO_EXPIRY) {
        o->lifespan_s = o->lt.lifespan_s;
        o->send = 1;
    } else {
        o->send = 0;
        copy_str(o->drop_reason, sizeof o->drop_reason, pbs_lt_outcome_str(o->lt.outcome));
    }

    int64_t cos;
    if (get_ranged(item, "class_of_service", 0, 2, -1, &cos, err, ecap) < 0)
        return -1;
    if (cos < 0)
        cos = have_pre ? pbs_ion_class_of_service(pre.priority) : 1;
    o->class_of_service = cos < 0 ? 1 : (int)cos;

    if (o->label[0] && store_label(c, o->label, o->env, o->len) < 0) {
        seterr(err, ecap, "out of memory");
        return -1;
    }
    return 0;
}

/* ---- Identity set ----------------------------------------------------------------------- */

#define IDKEY_LEN (PBS_SOURCE_ID_LEN + 2 + 8)

void earth_idset_init(earth_idset_t *s) { memset(s, 0, sizeof *s); }

void earth_idset_free(earth_idset_t *s)
{
    free(s->keys);
    free(s->counts);
    memset(s, 0, sizeof *s);
}

static uint64_t fnv1a(const uint8_t *p, size_t n)
{
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) {
        h ^= p[i];
        h *= 1099511628211ULL;
    }
    return h;
}

static int idset_grow(earth_idset_t *s)
{
    size_t nc = s->cap ? s->cap * 2 : 1024;
    uint8_t(*nk)[IDKEY_LEN] = calloc(nc, IDKEY_LEN);
    uint32_t *ncnt = calloc(nc, sizeof *ncnt);
    if (!nk || !ncnt) {
        free(nk);
        free(ncnt);
        return -1;
    }
    for (size_t i = 0; i < s->cap; i++) {
        if (!s->counts[i])
            continue;
        size_t j = fnv1a(s->keys[i], IDKEY_LEN) & (nc - 1);
        while (ncnt[j])
            j = (j + 1) & (nc - 1);
        memcpy(nk[j], s->keys[i], IDKEY_LEN);
        ncnt[j] = s->counts[i];
    }
    free(s->keys);
    free(s->counts);
    s->keys = nk;
    s->counts = ncnt;
    s->cap = nc;
    return 0;
}

uint32_t earth_idset_add(earth_idset_t *s, const pbs_header_t *h)
{
    uint8_t k[IDKEY_LEN];
    memcpy(k, h->source_id, PBS_SOURCE_ID_LEN);
    pbs_put_u16(k + 16, h->sequence);
    pbs_put_u64(k + 18, h->timestamp_us);
    if ((s->n + 1) * 2 > s->cap && idset_grow(s) < 0)
        return 0;
    size_t j = fnv1a(k, IDKEY_LEN) & (s->cap - 1);
    while (s->counts[j]) {
        if (memcmp(s->keys[j], k, IDKEY_LEN) == 0)
            return s->counts[j]++;
        j = (j + 1) & (s->cap - 1);
    }
    memcpy(s->keys[j], k, IDKEY_LEN);
    s->counts[j] = 1;
    s->n++;
    return 0;
}

/* ---- Log records (docs/LOG-SCHEMA.md) ----------------------------------------------------- */

void earth_log_begin(jw_t *w, const earth_logctx_t *lc, const char *event, int64_t t_us)
{
    jw_reset(w);
    jw_obj_begin(w);
    jw_kstr(w, "schema", EARTH_LOG_SCHEMA);
    jw_kstr(w, "event", event);
    jw_kint(w, "t_us", t_us);
    jw_kstr(w, "endpoint", lc->endpoint);
    jw_kstr(w, "node_eid", lc->node_eid);
    jw_kstr(w, "scenario", lc->scenario);
}

static void sha256_of(const uint8_t *b, size_t n, uint8_t md[32])
{
    unsigned int ml = 0;
    if (EVP_Digest(b, n, md, &ml, EVP_sha256(), NULL) != 1 || ml != 32)
        memset(md, 0, 32);
}

static void log_crc(jw_t *w, const char *k, uint32_t v)
{
    char crc[16];
    snprintf(crc, sizeof crc, "0x%08X", v);
    jw_kstr(w, k, crc);
}

static void log_suite(jw_t *w, uint16_t suite)
{
    char s[8];
    snprintf(s, sizeof s, "0x%04X", suite);
    jw_kstr(w, "suite_id", s);
}

/* Section 4 fields except frame_types. */
static void log_env_core(jw_t *w, const uint8_t *buf, size_t avail, size_t total,
                         const uint8_t *sha, int have_hdr, const pbs_header_t *h)
{
    if (have_hdr) {
        char sid[64];
        pbs_source_id_display(h->source_id, sid, sizeof sid);
        jw_kstr(w, "source_id", sid);
        jw_khex(w, "source_id_hex", h->source_id, PBS_SOURCE_ID_LEN);
        jw_kuint(w, "sequence", h->sequence);
        jw_kuint(w, "priority", h->priority);
        jw_kuint(w, "flags", h->flags);
        jw_kuint(w, "timestamp_us", h->timestamp_us);
        jw_kuint(w, "ttl_s", h->ttl);
        jw_kuint(w, "size", h->size);
        log_crc(w, "crc32", h->crc32);
        jw_khex(w, "header_hex", buf, PBS_HEADER_SIZE);
    } else {
        static const char *const keys[] = {"source_id", "source_id_hex", "sequence", "priority",
                                           "flags", "timestamp_us", "ttl_s", "size", "crc32",
                                           "header_hex"};
        for (size_t i = 0; i < sizeof keys / sizeof keys[0]; i++)
            jw_knull(w, keys[i]);
    }
    jw_kuint(w, "envelope_len", total);
    uint8_t md[32];
    if (!sha && avail == total) {
        sha256_of(buf, avail, md);
        sha = md;
    }
    if (sha)
        jw_khex(w, "envelope_sha256", sha, 32);
    else
        jw_knull(w, "envelope_sha256");
    if (avail == total && total <= EARTH_HEX_MAX)
        jw_khex(w, "envelope_hex", buf, total);
    else
        jw_knull(w, "envelope_hex");
}

static void log_frame_types(jw_t *w, const pbs_mux_t *m)
{
    jw_key(w, "frame_types");
    jw_arr_begin(w);
    for (unsigned i = 0; m && i < m->count; i++)
        jw_uint(w, m->frames[i].type);
    jw_arr_end(w);
}

/* Decode as Endpoint A's crosscheck does: Size 0 gives [], otherwise the
 * container in the declared Size bytes, null when it does not parse. */
static int decode_mux(const uint8_t *buf, size_t len, pbs_header_t *h, pbs_mux_t *m)
{
    if (len < PBS_HEADER_SIZE)
        return -1;
    pbs_header_read(buf, h);
    if (h->size == 0)
        return 0;
    if (len - PBS_HEADER_SIZE < h->size)
        return -1;
    return pbs_mux_parse(buf + PBS_HEADER_SIZE, h->size, m) == PBS_R_OK ? 1 : -1;
}

void earth_log_envelope_fields(jw_t *w, const uint8_t *buf, size_t len)
{
    pbs_header_t h;
    pbs_mux_t *m = malloc(sizeof *m);
    int have = len >= PBS_HEADER_SIZE;
    memset(&h, 0, sizeof h);
    if (have)
        pbs_header_read(buf, &h);
    log_env_core(w, buf, len, len, NULL, have, &h);
    int d = m ? decode_mux(buf, len, &h, m) : -1;
    if (d == 0)
        log_frame_types(w, NULL);
    else if (d == 1)
        log_frame_types(w, m);
    else
        jw_knull(w, "frame_types");
    free(m);
}

static void log_commands(jw_t *w, const pbs_mux_t *m, const uint8_t *payload)
{
    int any = 0;
    for (unsigned i = 0; m && i < m->count; i++)
        any |= m->frames[i].type == PBS_FRAME_COMMAND;
    if (!any) {
        jw_knull(w, "commands");
        return;
    }
    jw_key(w, "commands");
    jw_arr_begin(w);
    for (unsigned i = 0; i < m->count; i++)
        if (m->frames[i].type == PBS_FRAME_COMMAND)
            jw_strn(w, (const char *)payload + m->frames[i].value_offset, m->frames[i].length);
    jw_arr_end(w);
}

/* plan != 0: the pbs_tool plan dry run ("x_plan" event, nothing sent). */
static void log_send_record(jw_t *w, const earth_logctx_t *lc, const earth_item_t *it,
                            int64_t t_us, int called, int bp_rc, int64_t t_return_us,
                            const char *fail_reason, int plan)
{
    int sent = called && bp_rc == 1;
    const char *reason = sent ? "OK" : !it->send ? it->drop_reason
                                     : plan ? "OK"
                                     : fail_reason ? fail_reason : "BP_SEND_FAILED";
    earth_log_begin(w, lc, plan ? "x_plan" : "send", t_us);
    earth_log_envelope_fields(w, it->env, it->len);
    if (plan)
        jw_kstr(w, "result", it->send ? "TO_SEND" : "NOT_SENT");
    else
        jw_kstr(w, "result", sent ? "SENT" : "NOT_SENT");
    jw_kstr(w, "reason", reason);
    jw_kstr(w, "dest_eid", it->dest);
    if (called || (plan && it->send))
        jw_kint(w, "lifespan_s", it->lifespan_s);
    else
        jw_knull(w, "lifespan_s");
    jw_kstr(w, "lifespan_rule", it->lifetime_override ? "OVERRIDE" : "DTN-MAP-01-6.1");
    jw_kint(w, "class_of_service", it->class_of_service);
    jw_kint(w, "ordinal", 0);
    if (called)
        jw_kint(w, "bp_send_return_us", t_return_us);
    else
        jw_knull(w, "bp_send_return_us");
    jw_kstr(w, "label", it->label[0] ? it->label : NULL);

    /* commands and Security frame fields: as transmitted. */
    pbs_header_t h;
    pbs_mux_t *m = malloc(sizeof *m);
    int d = m ? decode_mux(it->env, it->len, &h, m) : -1;
    const uint8_t *payload = it->env + PBS_HEADER_SIZE;
    log_commands(w, d == 1 ? m : NULL, payload);
    pbs_sec_frame_t s;
    int have_sec = 0;
    for (unsigned i = 0; d == 1 && i < m->count && !have_sec; i++)
        if (m->frames[i].type == PBS_FRAME_SECURITY &&
            pbs_sec_frame_parse(payload + m->frames[i].value_offset, m->frames[i].length, &s) == 0)
            have_sec = 1;
    if (have_sec) {
        log_suite(w, s.suite_id);
        jw_key(w, "key_id");
        jw_strn(w, (const char *)s.key_id, s.key_id_len);
        jw_kuint(w, "security_sequence", s.security_sequence);
    } else {
        jw_knull(w, "suite_id");
        jw_knull(w, "key_id");
        jw_knull(w, "security_sequence");
    }
    free(m);

    /* Additions (x_ prefix, docs/LOG-SCHEMA.md Section 1). */
    jw_kstr(w, "x_kind", it->kind);
    jw_kstr(w, "x_tamper", it->tamper[0] ? it->tamper : NULL);
    jw_kstr(w, "x_replay_of", it->replay_of[0] ? it->replay_of : NULL);
    jw_kstr(w, "x_signing_key", it->signing_key[0] ? it->signing_key : NULL);
    jw_kstr(w, "x_role", it->role[0] ? it->role : NULL);
    jw_kstr(w, "x_note", it->note[0] ? it->note : NULL);
    jw_kstr(w, "x_lifetime_outcome", it->have_header ? pbs_lt_outcome_str(it->lt.outcome) : NULL);
    if (it->have_header && it->lt.has_bound)
        jw_kint(w, "x_lifetime_bound_ms", it->lt.bound_ms);
    else
        jw_knull(w, "x_lifetime_bound_ms");
    if (plan)   /* the dry run also gives the bytes above EARTH_HEX_MAX */
        jw_khex(w, "x_envelope_hex", it->env, it->len);
    jw_obj_end(w);
}

void earth_log_send(jw_t *w, const earth_logctx_t *lc, const earth_item_t *it, int64_t t_us,
                    int called, int bp_rc, int64_t t_return_us, const char *fail_reason)
{
    log_send_record(w, lc, it, t_us, called, bp_rc, t_return_us, fail_reason, 0);
}

void earth_log_plan(jw_t *w, const earth_logctx_t *lc, const earth_item_t *it, int64_t t_us)
{
    log_send_record(w, lc, it, t_us, 0, 0, 0, NULL, 1);
}

static int printable(const uint8_t *b, size_t n)
{
    for (size_t i = 0; i < n; i++)
        if (b[i] < 0x20 || b[i] > 0x7E)
            return 0;
    return 1;
}

static void log_frames_detail(jw_t *w, const pbs_mux_t *m, const uint8_t *payload)
{
    jw_key(w, "x_frames");
    jw_arr_begin(w);
    for (unsigned i = 0; i < m->count; i++) {
        const pbs_frame_t *f = &m->frames[i];
        const uint8_t *v = payload + f->value_offset;
        size_t show = f->length;
        jw_obj_begin(w);
        jw_kuint(w, "type", f->type);
        jw_kuint(w, "length", f->length);
        if (f->type != PBS_FRAME_SECURITY && f->type != PBS_FRAME_AUTHORITY &&
            printable(v, f->length)) {
            if (show > 512)
                show = 512;
            jw_key(w, "text");
            jw_strn(w, (const char *)v, show);
        } else {
            if (show > 64)
                show = 64;
            jw_khex(w, "hex", v, show);
        }
        jw_kbool(w, "truncated", show < f->length);
        jw_obj_end(w);
    }
    jw_arr_end(w);
}

void earth_log_recv(jw_t *w, const earth_logctx_t *lc, const pbs_rx_t *rx, const uint8_t *buf,
                    size_t avail, const uint8_t *sha256, int64_t recv_us,
                    const earth_bundle_meta_t *meta)
{
    earth_log_begin(w, lc, "recv", recv_us);
    log_env_core(w, buf, avail, rx->total_len, sha256, rx->have_header, &rx->hdr);
    if (rx->have_mux)
        log_frame_types(w, &rx->mux);
    else if (rx->accepted && rx->hdr.size == 0)
        log_frame_types(w, NULL);
    else
        jw_knull(w, "frame_types");
    jw_kstr(w, "result", rx->accepted ? "ACCEPT" : "REJECT");
    jw_kstr(w, "reason", pbs_reason_str(rx->reason));
    if (meta && meta->have_bundle) {
        jw_kstr(w, "bundle_source_eid", meta->bundle_source_eid);
        jw_kuint(w, "bundle_creation_ms", meta->creation_ms);
        jw_kuint(w, "bundle_creation_count", meta->creation_count);
        jw_kint(w, "bundle_creation_us",
                ((int64_t)meta->creation_ms + PBS_DTN_EPOCH_OFFSET_MS) * 1000);
        jw_kuint(w, "bundle_ttl_field_ms", meta->ttl_field_ms);
        jw_kbool(w, "bundle_admin_record", meta->admin_record);
    } else {
        static const char *const keys[] = {"bundle_source_eid", "bundle_creation_ms",
                                           "bundle_creation_count", "bundle_creation_us",
                                           "bundle_ttl_field_ms", "bundle_admin_record"};
        for (size_t i = 0; i < sizeof keys / sizeof keys[0]; i++)
            jw_knull(w, keys[i]);
    }
    const char *exp = meta ? meta->expected_eid : NULL;
    jw_kstr(w, "expected_eid", exp);
    if (exp && meta->have_bundle && meta->bundle_source_eid)
        jw_kbool(w, "eid_match", strcmp(exp, meta->bundle_source_eid) == 0);
    else
        jw_knull(w, "eid_match");
    if (rx->have_header)
        jw_kint(w, "latency_us", recv_us - (int64_t)rx->hdr.timestamp_us);
    else
        jw_knull(w, "latency_us");
    const uint8_t *payload = buf + PBS_HEADER_SIZE;
    if (rx->accepted && rx->n_commands)
        log_commands(w, &rx->mux, payload);
    else
        jw_knull(w, "commands");
    if (rx->sec_parsed) {
        log_suite(w, rx->sec.suite_id);
        jw_key(w, "key_id");
        jw_strn(w, (const char *)rx->sec.key_id, rx->sec.key_id_len);
        jw_kuint(w, "security_sequence", rx->sec.security_sequence);
    } else {
        jw_knull(w, "suite_id");
        jw_knull(w, "key_id");
        jw_knull(w, "security_sequence");
    }
    if (rx->have_auth_ctx) {
        jw_key(w, "authority_id");
        jw_strn(w, (const char *)rx->auth_ctx.authority_id, rx->auth_ctx.authority_id_len);
        jw_key(w, "role");
        jw_strn(w, (const char *)rx->auth_ctx.role, rx->auth_ctx.role_len);
        jw_kuint(w, "policy_epoch", rx->auth_ctx.policy_epoch);
    } else {
        jw_knull(w, "authority_id");
        jw_knull(w, "role");
        jw_knull(w, "policy_epoch");
    }
    /* Additions. */
    if (rx->have_header && rx->reason != PBS_R_MAGIC)
        log_crc(w, "x_crc32_computed", rx->crc_computed);
    if (meta && meta->have_duplicates)
        jw_kuint(w, "x_duplicates_before", meta->duplicates_before);
    if (rx->sec_parsed)
        jw_kbool(w, "x_authenticated", rx->authenticated);
    if (rx->have_mux)
        log_frames_detail(w, &rx->mux, payload);
    jw_obj_end(w);
}

void earth_log_exec(jw_t *w, const earth_logctx_t *lc, const pbs_rx_t *rx, const uint8_t *buf,
                    unsigned frame_index, int64_t t_us)
{
    const pbs_frame_t *f = &rx->mux.frames[frame_index];
    const uint8_t *v = buf + PBS_HEADER_SIZE + f->value_offset;
    size_t oplen = pbs_command_op(v, f->length, NULL, 0);
    earth_log_begin(w, lc, "exec", t_us);
    jw_khex(w, "source_id_hex", rx->hdr.source_id, PBS_SOURCE_ID_LEN);
    jw_kuint(w, "sequence", rx->hdr.sequence);
    jw_kuint(w, "timestamp_us", rx->hdr.timestamp_us);
    jw_key(w, "command");
    jw_strn(w, (const char *)v, f->length);
    jw_key(w, "operation");
    jw_strn(w, (const char *)v, oplen);
    jw_key(w, "key_id");
    jw_strn(w, (const char *)rx->sec.key_id, rx->sec.key_id_len);
    jw_kuint(w, "security_sequence", rx->sec.security_sequence);
    jw_key(w, "role");
    jw_strn(w, (const char *)rx->auth_ctx.role, rx->auth_ctx.role_len);
    jw_kstr(w, "action", "LOGGED");
    jw_obj_end(w);
}

/* ---- Plan files ---------------------------------------------------------------------------- */

static int cmp_entry(const void *a, const void *b)
{
    const earth_plan_entry_t *x = a, *y = b;
    if (x->at_s < y->at_s)
        return -1;
    if (x->at_s > y->at_s)
        return 1;
    if (x->line != y->line)
        return x->line < y->line ? -1 : 1;
    return x->rep - y->rep;
}

void earth_plan_free(earth_plan_t *p)
{
    for (size_t i = 0; i < p->nitems; i++)
        json_free(p->items[i]);
    free(p->items);
    free(p->entries);
    memset(p, 0, sizeof *p);
}

int earth_plan_load(FILE *f, earth_plan_t *p, char *err, size_t ecap)
{
    char *line = NULL;
    size_t cap = 0;
    long lineno = 0;
    ssize_t n;
    memset(p, 0, sizeof *p);
    while ((n = getline(&line, &cap, f)) >= 0) {
        lineno++;
        char *s = line;
        while (*s == ' ' || *s == '\t')
            s++;
        if (*s == '#' || *s == '\n' || *s == '\r' || *s == '\0')
            continue;
        char perr[160];
        json_value_t *v = json_parse(s, strlen(s), perr, sizeof perr);
        if (!v || v->type != JSON_OBJECT) {
            seterr(err, ecap, "line %ld: %s", lineno, v ? "not an object" : perr);
            json_free(v);
            goto fail;
        }
        double at = 0, interval = 0;
        int64_t count = 1;
        if (json_get(v, "at_s") && (json_get_num(v, "at_s", &at) < 0 || at < 0)) {
            seterr(err, ecap, "line %ld: bad at_s", lineno);
            json_free(v);
            goto fail;
        }
        if ((json_get(v, "interval_s") && (json_get_num(v, "interval_s", &interval) < 0 ||
                                           interval < 0)) ||
            (json_get(v, "count") &&
             (json_get_int(v, "count", &count) < 0 || count < 1 || count > 1000000))) {
            seterr(err, ecap, "line %ld: bad count or interval_s", lineno);
            json_free(v);
            goto fail;
        }
        json_value_t **ni = realloc(p->items, (p->nitems + 1) * sizeof *ni);
        if (!ni) {
            json_free(v);
            goto oom;
        }
        p->items = ni;
        p->items[p->nitems++] = v;
        earth_plan_entry_t *ne = realloc(p->entries, (p->nentries + (size_t)count) * sizeof *ne);
        if (!ne)
            goto oom;
        p->entries = ne;
        for (int64_t r = 0; r < count; r++) {
            earth_plan_entry_t *e = &p->entries[p->nentries++];
            e->at_s = at + (double)r * interval;
            e->item = v;
            e->line = lineno;
            e->rep = (int)r;
        }
    }
    free(line);
    if (ferror(f)) {
        seterr(err, ecap, "read error: %s", strerror(errno));
        earth_plan_free(p);
        return -1;
    }
    if (p->nentries)
        qsort(p->entries, p->nentries, sizeof *p->entries, cmp_entry);
    return 0;
oom:
    seterr(err, ecap, "out of memory");
fail:
    free(line);
    earth_plan_free(p);
    return -1;
}

/* ---- Counter state ------------------------------------------------------------------------- */

int earth_state_load(earth_ctx_t *c, const char *path, char *err, size_t ecap)
{
    size_t len;
    char *text = json_read_file(path, &len);
    if (!text) {
        if (errno == ENOENT)
            return 0;
        seterr(err, ecap, "%s: %s", path, strerror(errno));
        return -1;
    }
    char perr[160];
    json_value_t *v = json_parse(text, len, perr, sizeof perr);
    free(text);
    if (!v || v->type != JSON_OBJECT) {
        seterr(err, ecap, "%s: %s", path, v ? "not an object" : perr);
        json_free(v);
        return -1;
    }
    int rc = -1;
    const char *sid = json_get_str(v, "source_id");
    int64_t ns;
    const json_value_t *ss = json_get(v, "next_security_sequence");
    if (!sid || strcmp(sid, c->source_id) != 0)
        seterr(err, ecap, "%s: Source ID differs from %s", path, c->source_id);
    else if (json_get_int(v, "next_sequence", &ns) < 0 || ns < 0 || ns > 65535)
        seterr(err, ecap, "%s: bad next_sequence", path);
    else if (ss && ss->type != JSON_OBJECT)
        seterr(err, ecap, "%s: bad next_security_sequence", path);
    else {
        c->next_sequence = (uint16_t)ns;
        c->nsecseq = 0;
        rc = 0;
        for (size_t i = 0; ss && i < ss->count; i++) {
            const json_value_t *x = ss->items[i];
            if (c->nsecseq >= EARTH_MAX_SECSEQ || strlen(ss->keys[i]) > PBS_KEY_ID_MAX ||
                x->type != JSON_NUMBER || !x->is_integer || x->integer < 1) {
                seterr(err, ecap, "%s: bad security_sequence entry", path);
                rc = -1;
                break;
            }
            copy_str(c->secseq[c->nsecseq].key_id, sizeof c->secseq[0].key_id, ss->keys[i]);
            c->secseq[c->nsecseq].next = (uint64_t)x->integer;
            c->nsecseq++;
        }
    }
    json_free(v);
    return rc;
}

int earth_state_save(const earth_ctx_t *c, const char *path)
{
    char tmp[4096];
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp)
        return -1;
    jw_t w;
    jw_init(&w);
    jw_obj_begin(&w);
    jw_kstr(&w, "source_id", c->source_id);
    jw_kuint(&w, "next_sequence", c->next_sequence);
    jw_key(&w, "next_security_sequence");
    jw_obj_begin(&w);
    for (size_t i = 0; i < c->nsecseq; i++)
        jw_kuint(&w, c->secseq[i].key_id, c->secseq[i].next);
    jw_obj_end(&w);
    jw_obj_end(&w);
    FILE *f = fopen(tmp, "w");
    int rc = -1;
    if (f) {
        int ok = fputs(jw_cstr(&w), f) >= 0 && fputc('\n', f) != EOF;
        if (fclose(f) == 0 && ok && !w.oom)
            rc = rename(tmp, path) == 0 ? 0 : -1;
    }
    jw_free(&w);
    return rc;
}
