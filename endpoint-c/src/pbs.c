/*
 * pbs.c - PBS v1.5.0 envelope, MUX, authority context and authenticated
 * command processing for Endpoint B (Earth, C) of the PBS ION demo.
 * See pbs.h. Section references: PBS specifications and docs/SPEC-DIGEST.md.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#include "pbs.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

#include <openssl/crypto.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>

/* ---- Byte order (big-endian everywhere: ENV-01 4, MUX-01 4, A21) ---------- */

uint16_t pbs_get_u16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }

uint32_t pbs_get_u32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

uint64_t pbs_get_u64(const uint8_t *p)
{
    return ((uint64_t)pbs_get_u32(p) << 32) | pbs_get_u32(p + 4);
}

void pbs_put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

void pbs_put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

void pbs_put_u64(uint8_t *p, uint64_t v)
{
    pbs_put_u32(p, (uint32_t)(v >> 32));
    pbs_put_u32(p + 4, (uint32_t)v);
}

/* ---- Reason codes ----------------------------------------------------------- */

static const char *const reason_names[PBS_R_COUNT_] = {
    "OK",
    "LENGTH",
    "MAGIC",
    "CRC",
    "PRIORITY",
    "TTL_EXPIRED",
    "MUX_VERSION",
    "MUX_MALFORMED",
    "SEC_MISSING",
    "SEC_MALFORMED",
    "SEC_SUITE_UNKNOWN",
    "SEC_KEY_UNKNOWN",
    "SEC_AUTH_FAIL",
    "SEC_REPLAY",
    "AUTH_CTX_MISSING",
    "AUTH_CTX_MALFORMED",
    "AUTHZ_DENIED",
};

const char *pbs_reason_str(pbs_reason_t r)
{
    if ((int)r < 0 || r >= PBS_R_COUNT_)
        return "UNKNOWN";
    return reason_names[r];
}

int pbs_reason_from_str(const char *s)
{
    for (int i = 0; i < PBS_R_COUNT_; i++)
        if (strcmp(s, reason_names[i]) == 0)
            return i;
    return -1;
}

/* ---- CRC-32 ------------------------------------------------------------------- */

static uint32_t crc_table[256];
static int crc_table_ready;

static void crc_init(void)
{
    for (uint32_t n = 0; n < 256; n++) {
        uint32_t c = n;
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc_table[n] = c;
    }
    crc_table_ready = 1;
}

uint32_t pbs_crc32_update(uint32_t crc, const void *data, size_t len)
{
    const uint8_t *p = data;
    if (!crc_table_ready)
        crc_init();
    crc ^= 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++)
        crc = crc_table[(crc ^ p[i]) & 0xFFu] ^ (crc >> 8);
    return crc ^ 0xFFFFFFFFu;
}

uint32_t pbs_crc32(const void *data, size_t len)
{
    return pbs_crc32_update(0, data, len);
}

/* ---- Header -------------------------------------------------------------------- */

void pbs_header_write(const pbs_header_t *h, uint8_t out[PBS_HEADER_SIZE])
{
    out[0] = h->magic;
    out[1] = h->priority;
    out[2] = h->flags;
    out[3] = h->reserved03;
    pbs_put_u16(out + 4, h->sequence);
    pbs_put_u16(out + 6, h->reserved06);
    memcpy(out + 8, h->source_id, PBS_SOURCE_ID_LEN);
    pbs_put_u64(out + 0x18, h->timestamp_us);
    pbs_put_u32(out + 0x20, h->size);
    pbs_put_u32(out + 0x24, h->ttl);
    pbs_put_u32(out + 0x28, h->crc32);
}

void pbs_header_read(const uint8_t in[PBS_HEADER_SIZE], pbs_header_t *h)
{
    h->magic = in[0];
    h->priority = in[1];
    h->flags = in[2];
    h->reserved03 = in[3];
    h->sequence = pbs_get_u16(in + 4);
    h->reserved06 = pbs_get_u16(in + 6);
    memcpy(h->source_id, in + 8, PBS_SOURCE_ID_LEN);
    h->timestamp_us = pbs_get_u64(in + 0x18);
    h->size = pbs_get_u32(in + 0x20);
    h->ttl = pbs_get_u32(in + 0x24);
    h->crc32 = pbs_get_u32(in + 0x28);
}

uint32_t pbs_header_crc(const uint8_t hdr[PBS_HEADER_SIZE])
{
    static const uint8_t zero[4] = {0, 0, 0, 0};
    uint32_t c = pbs_crc32_update(0, hdr, 0x28);
    return pbs_crc32_update(c, zero, 4);
}

uint32_t pbs_header_seal(uint8_t hdr[PBS_HEADER_SIZE])
{
    uint32_t c = pbs_header_crc(hdr);
    pbs_put_u32(hdr + 0x28, c);
    return c;
}

int pbs_source_id_encode(const char *utf8, uint8_t out[PBS_SOURCE_ID_LEN])
{
    if (!utf8)
        return PBS_E_ARG;
    size_t n = strlen(utf8);
    if (n > PBS_SOURCE_ID_LEN)
        return PBS_E_SOURCE_ID;
    memset(out, 0, PBS_SOURCE_ID_LEN);
    memcpy(out, utf8, n);
    return PBS_E_OK;
}

/* Length of a valid UTF-8 sequence at p (n bytes available), or 0. */
static size_t utf8_seq_len(const uint8_t *p, size_t n)
{
    uint8_t b = p[0];
    if (b < 0x80)
        return 1;
    size_t need;
    uint8_t lo = 0x80, hi = 0xBF;
    if (b >= 0xC2 && b <= 0xDF)
        need = 2;
    else if (b == 0xE0) {
        need = 3;
        lo = 0xA0;
    } else if ((b >= 0xE1 && b <= 0xEC) || b == 0xEE || b == 0xEF)
        need = 3;
    else if (b == 0xED) {
        need = 3;
        hi = 0x9F;
    } else if (b == 0xF0) {
        need = 4;
        lo = 0x90;
    } else if (b >= 0xF1 && b <= 0xF3)
        need = 4;
    else if (b == 0xF4) {
        need = 4;
        hi = 0x8F;
    } else
        return 0;
    if (n < need)
        return 0;
    if (p[1] < lo || p[1] > hi)
        return 0;
    for (size_t i = 2; i < need; i++)
        if (p[i] < 0x80 || p[i] > 0xBF)
            return 0;
    return need;
}

void pbs_source_id_display(const uint8_t sid[PBS_SOURCE_ID_LEN], char *out,
                           size_t cap)
{
    size_t n = 0, o = 0;
    while (n < PBS_SOURCE_ID_LEN && sid[n] != 0)
        n++;
    if (cap == 0)
        return;
    for (size_t i = 0; i < n;) {
        size_t l = utf8_seq_len(sid + i, n - i);
        if (l == 0) {
            if (o + 3 >= cap)
                break;
            out[o++] = (char)0xEF;
            out[o++] = (char)0xBF;
            out[o++] = (char)0xBD;
            i++;
        } else {
            if (o + l >= cap)
                break;
            memcpy(out + o, sid + i, l);
            o += l;
            i += l;
        }
    }
    out[o] = '\0';
}

int pbs_envelope_build(pbs_header_t *h, const uint8_t *payload, size_t plen,
                       uint8_t *out, size_t cap, size_t *out_len)
{
    if (!h || !out || (plen && !payload))
        return PBS_E_ARG;
    if (h->priority > PBS_PRIORITY_MAX)
        return PBS_E_ARG;
    if (plen > PBS_MAX_PAYLOAD)
        return PBS_E_SPACE;
    if (cap < PBS_HEADER_SIZE + plen)
        return PBS_E_SPACE;
    h->magic = PBS_MAGIC;
    h->size = (uint32_t)plen;
    h->crc32 = 0;
    pbs_header_write(h, out);
    h->crc32 = pbs_header_seal(out);
    if (plen)
        memcpy(out + PBS_HEADER_SIZE, payload, plen);
    if (out_len)
        *out_len = PBS_HEADER_SIZE + plen;
    return PBS_E_OK;
}

/* ---- TTL, lifetime, class of service ------------------------------------------ */

int pbs_ttl_expired(uint32_t ttl, uint64_t timestamp_us, int64_t now_us)
{
    if (ttl == 0)
        return 0;                       /* ENV-01 12.1: never expires */
    if (now_us < 0 || (uint64_t)now_us <= timestamp_us)
        return 0;                       /* age <= 0 */
    uint64_t age = (uint64_t)now_us - timestamp_us;
    return age > (uint64_t)ttl * 1000000u;
}

int64_t pbs_floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0)))
        q--;
    return q;
}

void pbs_lifetime(uint32_t ttl, uint64_t timestamp_us, int64_t clock_us,
                  pbs_lifetime_t *out)
{
    memset(out, 0, sizeof *out);
    if (ttl == 0) {
        /* DTN-MAP-01 6.1.1; digest 7.4 / A30. */
        out->outcome = PBS_LT_NO_EXPIRY;
        out->lifespan_s = PBS_NO_EXPIRY_LIFETIME_S;
        return;
    }
    out->has_bound = 1;
    out->c_us = clock_us > INT64_MAX - PBS_LIFETIME_MARGIN_US
                    ? INT64_MAX
                    : clock_us + PBS_LIFETIME_MARGIN_US;
    /* bound = floor((TTL*1e6 - max(0, c_us - Timestamp)) / 1000), in
     * unsigned 64-bit pieces so that any u64 Timestamp is handled. */
    uint64_t ttl_us = (uint64_t)ttl * 1000000u;          /* < 2^53 */
    uint64_t elapsed = 0;
    if (out->c_us > 0 && (uint64_t)out->c_us > timestamp_us)
        elapsed = (uint64_t)out->c_us - timestamp_us;    /* < 2^63 */
    if (elapsed <= ttl_us)
        out->bound_ms = (int64_t)((ttl_us - elapsed) / 1000u);
    else
        out->bound_ms = -(int64_t)((elapsed - ttl_us + 999u) / 1000u);
    if (out->bound_ms < 1) {
        out->outcome = PBS_LT_GW_EXPIRED;
        return;
    }
    int64_t s = out->bound_ms / 1000;   /* bound_ms >= 1: floor == trunc */
    if (s > PBS_NO_EXPIRY_LIFETIME_S)
        s = PBS_NO_EXPIRY_LIFETIME_S;
    if (s < 1) {
        out->outcome = PBS_LT_UNDER_1S;
        return;
    }
    out->outcome = PBS_LT_OK;
    out->lifespan_s = s;
}

const char *pbs_lt_outcome_str(pbs_lt_outcome_t o)
{
    switch (o) {
    case PBS_LT_OK: return "OK";
    case PBS_LT_NO_EXPIRY: return "NO_EXPIRY";
    case PBS_LT_GW_EXPIRED: return "GW_EXPIRED";
    case PBS_LT_UNDER_1S: return "GW_LIFETIME_UNDER_1S";
    }
    return "UNKNOWN";
}

int pbs_ion_class_of_service(uint8_t priority)
{
    switch (priority) {
    case 0:
    case 1: return 2;   /* Expedited: BP_EXPEDITED_PRIORITY */
    case 2: return 1;   /* Normal: BP_STD_PRIORITY */
    case 3:
    case 4: return 0;   /* Bulk: BP_BULK_PRIORITY */
    default: return -1;
    }
}

const char *pbs_priority_name(uint8_t priority)
{
    static const char *const n[] = {"CRITICAL", "HIGH", "NORMAL", "LOW", "BULK"};
    return priority <= PBS_PRIORITY_MAX ? n[priority] : "RESERVED";
}

/* ---- MUX -------------------------------------------------------------------------- */

pbs_reason_t pbs_mux_parse(const uint8_t *payload, size_t len, pbs_mux_t *m)
{
    memset(m, 0, sizeof *m);
    if (len < 1)
        return PBS_R_MUX_MALFORMED;
    m->version = payload[0];
    if (m->version != PBS_MUX_VERSION)
        return PBS_R_MUX_VERSION;
    if (len < 2)
        return PBS_R_MUX_MALFORMED;
    m->count = payload[1];
    size_t off = 2;
    for (unsigned i = 0; i < m->count; i++) {
        if (len - off < 3)
            return PBS_R_MUX_MALFORMED;
        uint16_t flen = pbs_get_u16(payload + off + 1);
        if (len - off - 3 < flen)
            return PBS_R_MUX_MALFORMED;
        m->frames[i].type = payload[off];
        m->frames[i].length = flen;
        m->frames[i].offset = off;
        m->frames[i].value_offset = off + 3;
        off += 3u + flen;
    }
    m->frames_end = off;
    m->padding = len - off;
    return PBS_R_OK;
}

int pbs_mux_encode(const pbs_frame_in_t *frames, size_t n, uint8_t *out,
                   size_t cap, size_t *out_len)
{
    if (n > PBS_MUX_MAX_FRAMES || (n && !frames) || !out)
        return PBS_E_ARG;
    size_t need = 2;
    for (size_t i = 0; i < n; i++) {
        if (frames[i].length > 0xFFFF || (frames[i].length && !frames[i].value))
            return PBS_E_ARG;
        need += 3 + frames[i].length;
    }
    if (need > cap)
        return PBS_E_SPACE;
    out[0] = PBS_MUX_VERSION;
    out[1] = (uint8_t)n;
    size_t off = 2;
    for (size_t i = 0; i < n; i++) {
        out[off] = frames[i].type;
        pbs_put_u16(out + off + 1, (uint16_t)frames[i].length);
        if (frames[i].length)
            memcpy(out + off + 3, frames[i].value, frames[i].length);
        off += 3 + frames[i].length;
    }
    if (out_len)
        *out_len = off;
    return PBS_E_OK;
}

/* ---- Authority context ------------------------------------------------------------- */

int pbs_auth_ctx_parse(const uint8_t *v, size_t len, pbs_auth_ctx_t *a)
{
    memset(a, 0, sizeof *a);
    if (len < PBS_AUTH_FIXED_LEN)
        return -1;
    a->version = v[0];
    a->authority_id_type = v[1];
    a->authority_id_len = v[2];
    a->role_len = v[3];
    a->policy_epoch = pbs_get_u32(v + 4);
    if (a->version != PBS_AUTH_VERSION)
        return -1;
    if (len != (size_t)PBS_AUTH_FIXED_LEN + a->authority_id_len + a->role_len)
        return -1;
    a->authority_id = v + PBS_AUTH_FIXED_LEN;
    a->role = a->authority_id + a->authority_id_len;
    return 0;
}

int pbs_auth_ctx_encode(uint8_t authority_id_type, const char *authority_id,
                        const char *role, uint32_t policy_epoch, uint8_t *out,
                        size_t cap, size_t *out_len)
{
    if (!authority_id || !role || !out)
        return PBS_E_ARG;
    size_t il = strlen(authority_id), rl = strlen(role);
    if (il > 255 || rl > 255)
        return PBS_E_ARG;
    size_t need = PBS_AUTH_FIXED_LEN + il + rl;
    if (need > cap)
        return PBS_E_SPACE;
    out[0] = PBS_AUTH_VERSION;
    out[1] = authority_id_type;
    out[2] = (uint8_t)il;
    out[3] = (uint8_t)rl;
    pbs_put_u32(out + 4, policy_epoch);
    memcpy(out + 8, authority_id, il);
    memcpy(out + 8 + il, role, rl);
    if (out_len)
        *out_len = need;
    return PBS_E_OK;
}

/* ---- Security frame ------------------------------------------------------------------ */

int pbs_sec_frame_parse(const uint8_t *v, size_t len, pbs_sec_frame_t *s)
{
    memset(s, 0, sizeof *s);
    if (len < PBS_SEC_FIXED_LEN)
        return -1;
    s->version = v[0];
    s->suite_id = pbs_get_u16(v + 1);
    s->flags = v[3];
    s->key_id_len = v[4];
    s->nonce_len = v[5];
    s->auth_len = pbs_get_u16(v + 6);
    s->security_sequence = pbs_get_u64(v + 8);
    if (len != (size_t)PBS_SEC_FIXED_LEN + s->key_id_len + s->nonce_len +
                   s->auth_len)
        return -1;
    s->key_id = v + PBS_SEC_FIXED_LEN;
    s->nonce = s->key_id + s->key_id_len;
    s->auth_data = s->nonce + s->nonce_len;
    return 0;
}

/* ---- Keys and suites --------------------------------------------------------------- */

int pbs_suite_auth_len(uint16_t suite_id)
{
    switch (suite_id) {
    case PBS_SUITE_HMAC_SHA256: return PBS_HMAC_TAG_LEN;
    case PBS_SUITE_ED25519: return PBS_ED25519_SIG_LEN;
    default: return -1;
    }
}

const char *pbs_suite_name(uint16_t suite_id)
{
    switch (suite_id) {
    case PBS_SUITE_HMAC_SHA256: return "PBS-DEMO-TEST-HMAC-SHA256";
    case PBS_SUITE_ED25519: return "PBS-DEMO-TEST-ED25519";
    default: return "UNKNOWN";
    }
}

static int ed25519_public_from_seed(const uint8_t seed[32], uint8_t pub[32])
{
    int rc = PBS_E_CRYPTO;
    EVP_PKEY *pk = EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, NULL, seed,
                                                PBS_ED25519_SEED_LEN);
    size_t pl = PBS_ED25519_PUB_LEN;
    if (pk && EVP_PKEY_get_raw_public_key(pk, pub, &pl) == 1 &&
        pl == PBS_ED25519_PUB_LEN)
        rc = PBS_E_OK;
    EVP_PKEY_free(pk);
    ERR_clear_error();
    return rc;
}

int pbs_test_key_derive(const char *key_id, uint16_t suite_id, pbs_key_t *k)
{
    if (!key_id || !k || pbs_suite_auth_len(suite_id) < 0)
        return PBS_E_ARG;
    size_t kl = strlen(key_id);
    if (kl < 1 || kl > PBS_KEY_ID_MAX)
        return PBS_E_ARG;
    memset(k, 0, sizeof *k);
    k->suite_id = suite_id;
    memcpy(k->key_id, key_id, kl);

    char label[PBS_KEY_ID_MAX + 64];
    int ll = snprintf(label, sizeof label,
                      "PBS-ION-DEMO TEST KEY %s (NOT FOR OPERATIONAL USE)", key_id);
    if (ll < 0 || (size_t)ll >= sizeof label)
        return PBS_E_ARG;
    unsigned int mdlen = 0;
    if (EVP_Digest(label, (size_t)ll, k->secret, &mdlen, EVP_sha256(), NULL) != 1 ||
        mdlen != 32) {
        ERR_clear_error();
        return PBS_E_CRYPTO;
    }
    k->has_secret = 1;
    if (suite_id == PBS_SUITE_ED25519) {
        int rc = ed25519_public_from_seed(k->secret, k->public_key);
        if (rc != PBS_E_OK)
            return rc;
        k->has_public = 1;
    }
    return PBS_E_OK;
}

void pbs_key_make_verify_only(pbs_key_t *k)
{
    if (k->suite_id == PBS_SUITE_ED25519) {
        OPENSSL_cleanse(k->secret, sizeof k->secret);
        k->has_secret = 0;
    }
}

int pbs_key_bind(pbs_key_t *k, const char *source_id, const char *authority)
{
    if (!k || !source_id || !authority || strlen(authority) >= sizeof k->bound_authority)
        return PBS_E_ARG;
    int rc = pbs_source_id_encode(source_id, k->bound_source_id);
    if (rc != PBS_E_OK)
        return rc;
    strcpy(k->bound_authority, authority);
    k->has_binding = 1;
    return PBS_E_OK;
}

int pbs_auth_compute(const pbs_key_t *k, const uint8_t *msg, size_t len,
                     uint8_t *out, size_t cap, size_t *out_len)
{
    if (!k || (!msg && len) || !out)
        return PBS_E_ARG;
    if (!k->has_secret)
        return PBS_E_KEY;
    if (k->suite_id == PBS_SUITE_HMAC_SHA256) {
        if (cap < PBS_HMAC_TAG_LEN)
            return PBS_E_SPACE;
        unsigned int ol = 0;
        if (!HMAC(EVP_sha256(), k->secret, PBS_HMAC_KEY_LEN, msg, len, out, &ol) ||
            ol != PBS_HMAC_TAG_LEN) {
            ERR_clear_error();
            return PBS_E_CRYPTO;
        }
        if (out_len)
            *out_len = ol;
        return PBS_E_OK;
    }
    if (k->suite_id == PBS_SUITE_ED25519) {
        if (cap < PBS_ED25519_SIG_LEN)
            return PBS_E_SPACE;
        int rc = PBS_E_CRYPTO;
        EVP_PKEY *pk = EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, NULL,
                                                    k->secret, PBS_ED25519_SEED_LEN);
        EVP_MD_CTX *ctx = EVP_MD_CTX_new();
        size_t sl = PBS_ED25519_SIG_LEN;
        if (pk && ctx && EVP_DigestSignInit(ctx, NULL, NULL, NULL, pk) == 1 &&
            EVP_DigestSign(ctx, out, &sl, msg, len) == 1 && sl == PBS_ED25519_SIG_LEN) {
            if (out_len)
                *out_len = sl;
            rc = PBS_E_OK;
        }
        EVP_MD_CTX_free(ctx);
        EVP_PKEY_free(pk);
        ERR_clear_error();
        return rc;
    }
    return PBS_E_ARG;
}

int pbs_auth_verify(const pbs_key_t *k, const uint8_t *msg, size_t len,
                    const uint8_t *auth, size_t auth_len)
{
    if (!k || (!msg && len) || !auth)
        return PBS_E_ARG;
    if (k->suite_id == PBS_SUITE_HMAC_SHA256) {
        uint8_t tag[PBS_HMAC_TAG_LEN];
        size_t tl = 0;
        if (auth_len != PBS_HMAC_TAG_LEN)
            return 0;
        int rc = pbs_auth_compute(k, msg, len, tag, sizeof tag, &tl);
        if (rc != PBS_E_OK)
            return rc;
        int ok = CRYPTO_memcmp(tag, auth, PBS_HMAC_TAG_LEN) == 0;
        OPENSSL_cleanse(tag, sizeof tag);
        return ok;
    }
    if (k->suite_id == PBS_SUITE_ED25519) {
        if (auth_len != PBS_ED25519_SIG_LEN)
            return 0;
        if (!k->has_public)
            return PBS_E_KEY;
        int result = PBS_E_CRYPTO;
        EVP_PKEY *pk = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, NULL,
                                                   k->public_key, PBS_ED25519_PUB_LEN);
        EVP_MD_CTX *ctx = EVP_MD_CTX_new();
        if (pk && ctx && EVP_DigestVerifyInit(ctx, NULL, NULL, NULL, pk) == 1) {
            int v = EVP_DigestVerify(ctx, auth, auth_len, msg, len);
            result = (v == 1) ? 1 : 0;
        }
        EVP_MD_CTX_free(ctx);
        EVP_PKEY_free(pk);
        ERR_clear_error();
        return result;
    }
    return PBS_E_ARG;
}

/* ---- Protected messages ------------------------------------------------------------- */

int pbs_build_protected(const pbs_protected_spec_t *s, uint8_t *out,
                        size_t cap, size_t *out_len, size_t *auth_input_len)
{
    if (!s || !out || (s->nframes && !s->frames))
        return PBS_E_ARG;
    if (s->hdr.priority > PBS_PRIORITY_MAX)
        return PBS_E_ARG;
    size_t nfr = s->nframes + (s->with_security ? 1 : 0);
    if (nfr > PBS_MUX_MAX_FRAMES)
        return PBS_E_ARG;

    size_t kid_len = 0, auth_len = 0;
    if (s->with_security) {
        if (!s->signing_key || !s->key_id)
            return PBS_E_ARG;
        int al = pbs_suite_auth_len(s->signing_key->suite_id);
        if (al < 0)
            return PBS_E_ARG;
        auth_len = (size_t)al;
        kid_len = strlen(s->key_id);
        if (kid_len < 1 || kid_len > PBS_KEY_ID_MAX)
            return PBS_E_ARG;
    }

    size_t size = 2;
    for (size_t i = 0; i < s->nframes; i++) {
        if (s->frames[i].length > 0xFFFF ||
            (s->frames[i].length && !s->frames[i].value))
            return PBS_E_ARG;
        size += 3 + s->frames[i].length;
    }
    if (s->with_security)
        size += 3 + PBS_SEC_FIXED_LEN + kid_len + auth_len;
    if (size > PBS_MAX_PAYLOAD)
        return PBS_E_SPACE;
    if (cap < PBS_HEADER_SIZE + size)
        return PBS_E_SPACE;

    pbs_header_t h = s->hdr;
    h.magic = PBS_MAGIC;
    h.size = (uint32_t)size;
    h.crc32 = 0;
    pbs_header_write(&h, out);
    pbs_header_seal(out);

    uint8_t *p = out + PBS_HEADER_SIZE;
    size_t off = 0;
    p[off++] = PBS_MUX_VERSION;
    p[off++] = (uint8_t)nfr;
    for (size_t i = 0; i < s->nframes; i++) {
        p[off] = s->frames[i].type;
        pbs_put_u16(p + off + 1, (uint16_t)s->frames[i].length);
        if (s->frames[i].length)
            memcpy(p + off + 3, s->frames[i].value, s->frames[i].length);
        off += 3 + s->frames[i].length;
    }
    size_t ai = 0;
    if (s->with_security) {
        size_t vlen = PBS_SEC_FIXED_LEN + kid_len + auth_len;
        p[off] = PBS_FRAME_SECURITY;
        pbs_put_u16(p + off + 1, (uint16_t)vlen);
        uint8_t *v = p + off + 3;
        v[0] = PBS_SEC_VERSION;
        pbs_put_u16(v + 1, s->suite_id);
        v[3] = PBS_SEC_FLAG_AUTH;
        v[4] = (uint8_t)kid_len;
        v[5] = 0;                               /* nonce_len (A19) */
        pbs_put_u16(v + 6, (uint16_t)auth_len);
        pbs_put_u64(v + 8, s->security_sequence);
        memcpy(v + PBS_SEC_FIXED_LEN, s->key_id, kid_len);
        off += 3 + vlen;
        ai = PBS_HEADER_SIZE + size - auth_len;  /* digest 6.2 */
        size_t al = 0;
        int rc = pbs_auth_compute(s->signing_key, out, ai, out + ai, auth_len, &al);
        if (rc != PBS_E_OK)
            return rc;
    }
    if (out_len)
        *out_len = PBS_HEADER_SIZE + size;
    if (auth_input_len)
        *auth_input_len = ai;
    return PBS_E_OK;
}

/* ---- Replay window --------------------------------------------------------------------- */

void pbs_replay_init(pbs_replay_t *r) { memset(r, 0, sizeof *r); }

static const pbs_replay_slot_t *replay_find(const pbs_replay_t *r,
                                            const uint8_t src[16],
                                            const uint8_t *kid, size_t kl)
{
    for (size_t i = 0; i < PBS_REPLAY_SLOTS; i++) {
        const pbs_replay_slot_t *sl = &r->slots[i];
        if (sl->in_use && sl->key_id_len == kl &&
            memcmp(sl->source_id, src, PBS_SOURCE_ID_LEN) == 0 &&
            memcmp(sl->key_id, kid, kl) == 0)
            return sl;
    }
    return NULL;
}

int pbs_replay_check(const pbs_replay_t *r, const uint8_t src[16],
                     const uint8_t *key_id, size_t key_id_len, uint64_t s)
{
    if (s == 0 || key_id_len > PBS_KEY_ID_MAX)
        return 0;
    const pbs_replay_slot_t *sl = replay_find(r, src, key_id, key_id_len);
    if (!sl) {
        for (size_t i = 0; i < PBS_REPLAY_SLOTS; i++)
            if (!r->slots[i].in_use)
                return 1;               /* H = 0: any s > 0 */
        return 0;                       /* table full: fail closed */
    }
    if (s > sl->highest)
        return 1;
    uint64_t d = sl->highest - s;
    if (d >= PBS_REPLAY_WINDOW)
        return 0;                       /* s <= H - 64 */
    return (sl->mask & ((uint64_t)1 << d)) == 0;
}

int pbs_replay_record(pbs_replay_t *r, const uint8_t src[16],
                      const uint8_t *key_id, size_t key_id_len, uint64_t s)
{
    if (key_id_len > PBS_KEY_ID_MAX)
        return -1;
    pbs_replay_slot_t *sl = (pbs_replay_slot_t *)replay_find(r, src, key_id, key_id_len);
    if (!sl) {
        for (size_t i = 0; i < PBS_REPLAY_SLOTS && !sl; i++)
            if (!r->slots[i].in_use)
                sl = &r->slots[i];
        if (!sl)
            return -1;
        memset(sl, 0, sizeof *sl);
        sl->in_use = 1;
        memcpy(sl->source_id, src, PBS_SOURCE_ID_LEN);
        sl->key_id_len = (uint8_t)key_id_len;
        memcpy(sl->key_id, key_id, key_id_len);
    }
    if (s > sl->highest) {
        uint64_t shift = s - sl->highest;
        sl->mask = (shift >= PBS_REPLAY_WINDOW) ? 0 : (sl->mask << shift);
        sl->mask |= 1;
        sl->highest = s;
    } else {
        uint64_t d = sl->highest - s;
        if (d < PBS_REPLAY_WINDOW)
            sl->mask |= (uint64_t)1 << d;
    }
    return 0;
}

/* ---- Policy --------------------------------------------------------------------------------- */

static void role_add(pbs_policy_t *p, const char *name, const char *const *ops, size_t n)
{
    pbs_role_t *r = &p->roles[p->nroles++];
    memset(r, 0, sizeof *r);
    snprintf(r->name, sizeof r->name, "%s", name);
    for (size_t i = 0; i < n && i < PBS_POLICY_MAX_OPS; i++)
        snprintf(r->ops[r->nops++], sizeof r->ops[0], "%s", ops[i]);
}

int pbs_policy_demo(pbs_policy_t *p)
{
    static const char *const flight[] = {"PING", "SET_HEATER", "DRIVE",
                                         "CAPTURE_IMAGE", "SET_MODE"};
    static const char *const science[] = {"CAPTURE_IMAGE"};
    memset(p, 0, sizeof *p);
    p->authority_id_type = 3;
    snprintf(p->authority_id, sizeof p->authority_id, "PBS-DEMO-MCC");
    p->policy_epoch = 1;
    role_add(p, "FLIGHT-OPS", flight, 5);
    role_add(p, "SCIENCE-OPS", science, 1);

    int rc = pbs_test_key_derive("TEST-ED25519-01", PBS_SUITE_ED25519, &p->keys[0]);
    if (rc == PBS_E_OK)
        rc = pbs_test_key_derive("TEST-HMAC-01", PBS_SUITE_HMAC_SHA256, &p->keys[1]);
    if (rc != PBS_E_OK)
        return rc;
    pbs_key_make_verify_only(&p->keys[0]);      /* rover holds the public key only */
    pbs_key_bind(&p->keys[0], "Earth-MCC", "PBS-DEMO-MCC");
    pbs_key_bind(&p->keys[1], "Earth-MCC", "PBS-DEMO-MCC");
    p->nkeys = 2;
    return PBS_E_OK;
}

const pbs_key_t *pbs_policy_find_key(const pbs_policy_t *p, uint16_t suite,
                                     const uint8_t *key_id, size_t len)
{
    for (size_t i = 0; i < p->nkeys; i++) {
        const pbs_key_t *k = &p->keys[i];
        if (k->suite_id == suite && strlen(k->key_id) == len &&
            memcmp(k->key_id, key_id, len) == 0)
            return k;
    }
    return NULL;
}

size_t pbs_command_op(const uint8_t *v, size_t len, char *out, size_t cap)
{
    size_t n = 0;
    while (n < len && v[n] != ' ')
        n++;
    if (cap == 0)
        return n;
    size_t c = n < cap - 1 ? n : cap - 1;
    memcpy(out, v, c);
    out[c] = '\0';
    return n;
}

static int bytes_eq_str(const uint8_t *b, size_t bl, const char *s)
{
    size_t sl = strlen(s);
    return bl == sl && memcmp(b, s, sl) == 0;
}

/* R13 (digest 6.6): source/authority binding, namespace, epoch, role and
 * every Command operation. */
static int authorize(const pbs_policy_t *pol, const pbs_key_t *key,
                     const pbs_rx_t *rx, const uint8_t *payload)
{
    const pbs_auth_ctx_t *a = &rx->auth_ctx;
    if (!key->has_binding)
        return 0;
    if (memcmp(key->bound_source_id, rx->hdr.source_id, PBS_SOURCE_ID_LEN) != 0)
        return 0;
    if (!bytes_eq_str(a->authority_id, a->authority_id_len, key->bound_authority))
        return 0;
    if (a->authority_id_type != pol->authority_id_type)
        return 0;
    if (!bytes_eq_str(a->authority_id, a->authority_id_len, pol->authority_id))
        return 0;
    if (a->policy_epoch != pol->policy_epoch)
        return 0;
    const pbs_role_t *role = NULL;
    for (size_t i = 0; i < pol->nroles; i++)
        if (bytes_eq_str(a->role, a->role_len, pol->roles[i].name))
            role = &pol->roles[i];
    if (!role)
        return 0;
    for (unsigned i = 0; i < rx->mux.count; i++) {
        const pbs_frame_t *f = &rx->mux.frames[i];
        if (f->type != PBS_FRAME_COMMAND)
            continue;
        const uint8_t *v = payload + f->value_offset;
        size_t oplen = pbs_command_op(v, f->length, NULL, 0);
        int permitted = 0;
        for (size_t j = 0; j < role->nops && !permitted; j++)
            permitted = oplen > 0 && bytes_eq_str(v, oplen, role->ops[j]);
        if (!permitted)
            return 0;
    }
    return 1;
}

/* 1 if a provisioned key of the policy is bound to this Source ID (R8a). */
static int source_bound(const pbs_policy_t *pol, const uint8_t sid[PBS_SOURCE_ID_LEN])
{
    for (size_t i = 0; pol && i < pol->nkeys; i++)
        if (pol->keys[i].has_binding &&
            memcmp(pol->keys[i].bound_source_id, sid, PBS_SOURCE_ID_LEN) == 0)
            return 1;
    return 0;
}

static pbs_reason_t rx_done(pbs_rx_t *rx, pbs_reason_t r)
{
    rx->reason = r;
    rx->accepted = (r == PBS_R_OK);
    return r;
}

pbs_reason_t pbs_receive_ex(const pbs_policy_t *pol, pbs_replay_t *rep,
                            const uint8_t *buf, size_t avail,
                            size_t total_len, int64_t now_us, pbs_rx_t *rx)
{
    memset(rx, 0, sizeof *rx);
    rx->total_len = total_len;

    /* R1: at least a header. */
    if (!buf || avail < PBS_HEADER_SIZE || total_len < PBS_HEADER_SIZE)
        return rx_done(rx, PBS_R_LENGTH);
    rx->have_header = 1;
    pbs_header_read(buf, &rx->hdr);
    const pbs_header_t *h = &rx->hdr;

    /* R2 to R5: PBS-ENV-01 14 steps 1 to 4. */
    if (h->magic != PBS_MAGIC)
        return rx_done(rx, PBS_R_MAGIC);
    rx->crc_computed = pbs_header_crc(buf);
    if (rx->crc_computed != h->crc32)
        return rx_done(rx, PBS_R_CRC);
    if (h->priority > PBS_PRIORITY_MAX)
        return rx_done(rx, PBS_R_PRIORITY);
    if (pbs_ttl_expired(h->ttl, h->timestamp_us, now_us))
        return rx_done(rx, PBS_R_TTL_EXPIRED);

    /* R6: payload extraction; one envelope per bundle payload (A7, A8, A9). */
    if (h->size > PBS_MAX_PAYLOAD || total_len != (size_t)PBS_HEADER_SIZE + h->size ||
        avail != total_len)
        return rx_done(rx, PBS_R_LENGTH);

    /* R6a: header-only message (A12). */
    if (h->size == 0)
        return rx_done(rx, PBS_R_OK);

    /* R7: MUX container. */
    const uint8_t *payload = buf + PBS_HEADER_SIZE;
    pbs_reason_t r = pbs_mux_parse(payload, h->size, &rx->mux);
    if (r != PBS_R_OK)
        return rx_done(rx, r);
    rx->have_mux = 1;

    unsigned sec_index = 0, auth_index = 0;
    for (unsigned i = 0; i < rx->mux.count; i++) {
        switch (rx->mux.frames[i].type) {
        case PBS_FRAME_COMMAND:
            rx->n_commands++;
            break;
        case PBS_FRAME_SECURITY:
            rx->n_security++;
            sec_index = i;
            break;
        case PBS_FRAME_AUTHORITY:
            rx->n_authority++;
            auth_index = i;
            break;
        default:
            break;                       /* other and unknown types skipped */
        }
    }

    /* R8, R8a, R8b: no Security frame. A Command frame (R8), an Authority
     * Context frame, padding, or a Source ID bound to a provisioned key
     * (R8a, digest A38) requires one. Lowering frame_count in transit turns
     * the Security frame into padding (PBS-MUX-01 4.3), so padding without
     * a Security frame is not accepted. */
    if (rx->n_security == 0) {
        if (rx->n_commands > 0)
            return rx_done(rx, PBS_R_SEC_MISSING);
        if (rx->n_authority > 0 || rx->mux.padding > 0 || source_bound(pol, h->source_id))
            return rx_done(rx, PBS_R_SEC_MISSING);
        return rx_done(rx, PBS_R_OK);
    }

    /* R9: exactly one Security frame, last, no padding; structure. */
    if (rx->n_security != 1 || sec_index + 1u != rx->mux.count || rx->mux.padding != 0)
        return rx_done(rx, PBS_R_SEC_MALFORMED);
    const pbs_frame_t *sf = &rx->mux.frames[sec_index];
    if (pbs_sec_frame_parse(payload + sf->value_offset, sf->length, &rx->sec) != 0 ||
        rx->sec.version != PBS_SEC_VERSION)
        return rx_done(rx, PBS_R_SEC_MALFORMED);
    rx->sec_parsed = 1;

    /* R9a, R9b, R9c. */
    int auth_len = pbs_suite_auth_len(rx->sec.suite_id);
    if (auth_len < 0)
        return rx_done(rx, PBS_R_SEC_SUITE_UNKNOWN);
    if (rx->sec.flags != PBS_SEC_FLAG_AUTH || rx->sec.nonce_len != 0 ||
        rx->sec.auth_len != (uint16_t)auth_len || rx->sec.key_id_len < 1)
        return rx_done(rx, PBS_R_SEC_MALFORMED);
    rx->have_sec = 1;
    const pbs_key_t *key = pol ? pbs_policy_find_key(pol, rx->sec.suite_id, rx->sec.key_id,
                                                     rx->sec.key_id_len)
                               : NULL;
    if (!key)
        return rx_done(rx, PBS_R_SEC_KEY_UNKNOWN);

    /* R10: auth_input = envelope[0 : 44 + Size - auth_len] (digest 6.2). */
    size_t ai = (size_t)(rx->sec.auth_data - buf);
    if (ai + (size_t)auth_len != total_len)
        return rx_done(rx, PBS_R_SEC_MALFORMED);   /* unreachable after R9 */
    if (pbs_auth_verify(key, buf, ai, rx->sec.auth_data, (size_t)auth_len) != 1)
        return rx_done(rx, PBS_R_SEC_AUTH_FAIL);
    rx->authenticated = 1;

    /* R11: replay window; state updated before authorization (A22). */
    if (!rep || !pbs_replay_check(rep, h->source_id, rx->sec.key_id, rx->sec.key_id_len,
                                  rx->sec.security_sequence))
        return rx_done(rx, PBS_R_SEC_REPLAY);
    if (pbs_replay_record(rep, h->source_id, rx->sec.key_id, rx->sec.key_id_len,
                          rx->sec.security_sequence) != 0)
        return rx_done(rx, PBS_R_SEC_REPLAY);

    /* R11a: protected data without commands. */
    if (rx->n_commands == 0) {
        rx->protected_msg = 1;
        return rx_done(rx, PBS_R_OK);
    }

    /* R12, R12a: exactly one well-formed Authority Context frame. */
    if (rx->n_authority == 0)
        return rx_done(rx, PBS_R_AUTH_CTX_MISSING);
    if (rx->n_authority > 1)
        return rx_done(rx, PBS_R_AUTH_CTX_MALFORMED);
    const pbs_frame_t *af = &rx->mux.frames[auth_index];
    if (pbs_auth_ctx_parse(payload + af->value_offset, af->length, &rx->auth_ctx) != 0)
        return rx_done(rx, PBS_R_AUTH_CTX_MALFORMED);
    rx->have_auth_ctx = 1;

    /* R13: authorization. */
    if (!authorize(pol, key, rx, payload))
        return rx_done(rx, PBS_R_AUTHZ_DENIED);

    /* R14. */
    rx->protected_msg = 1;
    return rx_done(rx, PBS_R_OK);
}

pbs_reason_t pbs_receive(const pbs_policy_t *pol, pbs_replay_t *rep,
                         const uint8_t *buf, size_t len, int64_t now_us,
                         pbs_rx_t *rx)
{
    return pbs_receive_ex(pol, rep, buf, len, len, now_us, rx);
}

/* ---- Hex -------------------------------------------------------------------------------------- */

void pbs_hex_encode(const uint8_t *in, size_t len, char *out)
{
    static const char d[] = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[2 * i] = d[in[i] >> 4];
        out[2 * i + 1] = d[in[i] & 15];
    }
    out[2 * len] = '\0';
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

long pbs_hex_decode(const char *hex, uint8_t *out, size_t cap)
{
    size_t n = strlen(hex);
    if (n % 2 != 0 || n / 2 > cap || n / 2 > (size_t)LONG_MAX)
        return -1;
    for (size_t i = 0; i < n / 2; i++) {
        int a = hexval(hex[2 * i]), b = hexval(hex[2 * i + 1]);
        if (a < 0 || b < 0)
            return -1;
        out[i] = (uint8_t)((a << 4) | b);
    }
    return (long)(n / 2);
}
