/*
 * pbs.h - PBS v1.5.0 envelope, MUX, authority context and authenticated
 * command processing for Endpoint B (Earth, C) of the PBS ION demo.
 *
 * Written from the PBS specifications (PBS-ENV-01 1.5, PBS-MUX-01 1.4,
 * PBS-AUTH-01 1.4, PBS-SEC-B-01 1.5, PBS-DTN-MAP-01 1.5) and from
 * docs/SPEC-DIGEST.md, which fixes the choices the specifications leave
 * open (A1 to A38). Section numbers in comments refer to those documents.
 *
 * No function here depends on ION. Cryptography uses OpenSSL libcrypto.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#ifndef PBS_H
#define PBS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Constants ---------------------------------------------------------- */

#define PBS_HEADER_SIZE        44            /* PBS-ENV-01 4 */
#define PBS_MAGIC              0x10          /* PBS-ENV-01 5 */
#define PBS_PRIORITY_MAX       4             /* PBS-ENV-01 6, PBS-PRIO-01 4 */
#define PBS_FLAG_ACK_REQ       0x01          /* PBS-ENV-01 7 */
#define PBS_SOURCE_ID_LEN      16            /* PBS-ENV-01 9 */
#define PBS_MAX_PAYLOAD        65536u        /* digest A9 */
#define PBS_MAX_ENVELOPE       (PBS_HEADER_SIZE + PBS_MAX_PAYLOAD)

#define PBS_MUX_VERSION        0x01          /* digest A13 */
#define PBS_MUX_MAX_FRAMES     255

#define PBS_FRAME_TELEMETRY    0x01          /* PBS-MUX-01 5 */
#define PBS_FRAME_COMMAND      0x02
#define PBS_FRAME_STATUS       0x03
#define PBS_FRAME_EVENT        0x04
#define PBS_FRAME_SERVICE      0x07
#define PBS_FRAME_AUTHORITY    0x08          /* PBS-AUTH-01 2 */
#define PBS_FRAME_PNT          0x09
#define PBS_FRAME_SECURITY     0x0A          /* PBS-SEC-B-01 4 */

#define PBS_AUTH_VERSION       0x01
#define PBS_AUTH_FIXED_LEN     8
#define PBS_SEC_VERSION        0x01
#define PBS_SEC_FIXED_LEN      16
#define PBS_SEC_FLAG_AUTH      0x01          /* digest A17 */

#define PBS_SUITE_HMAC_SHA256  0xFF01        /* digest 6.3, test-only */
#define PBS_SUITE_ED25519      0xFF02        /* digest 6.3, test-only */
#define PBS_HMAC_KEY_LEN       32
#define PBS_HMAC_TAG_LEN       32
#define PBS_ED25519_SEED_LEN   32
#define PBS_ED25519_PUB_LEN    32
#define PBS_ED25519_SIG_LEN    64
#define PBS_MAX_AUTH_LEN       64

#define PBS_REPLAY_WINDOW      64            /* digest 6.4 */
#define PBS_REPLAY_SLOTS       256

#define PBS_NO_EXPIRY_LIFETIME_S 2147483647LL  /* digest 7.4, A30 */
#define PBS_LIFETIME_MARGIN_US   1000000LL     /* digest 7.4, A29 */
#define PBS_DTN_EPOCH_OFFSET_MS  946684800000LL /* RFC 9171 4.2.6 */

/* Return codes of builders and helpers. */
#define PBS_E_OK         0
#define PBS_E_ARG       -1   /* invalid argument */
#define PBS_E_SPACE     -2   /* output buffer too small or limit exceeded */
#define PBS_E_SOURCE_ID -3   /* Source ID longer than 16 bytes (A4) */
#define PBS_E_CRYPTO    -4   /* OpenSSL failure */
#define PBS_E_KEY       -5   /* key lacks the material for the operation */

/* ---- Receive reason codes (digest 2.5 and 6.6) -------------------------- */

typedef enum {
    PBS_R_OK = 0,
    PBS_R_LENGTH,
    PBS_R_MAGIC,
    PBS_R_CRC,
    PBS_R_PRIORITY,
    PBS_R_TTL_EXPIRED,
    PBS_R_MUX_VERSION,
    PBS_R_MUX_MALFORMED,
    PBS_R_SEC_MISSING,
    PBS_R_SEC_MALFORMED,
    PBS_R_SEC_SUITE_UNKNOWN,
    PBS_R_SEC_KEY_UNKNOWN,
    PBS_R_SEC_AUTH_FAIL,
    PBS_R_SEC_REPLAY,
    PBS_R_AUTH_CTX_MISSING,
    PBS_R_AUTH_CTX_MALFORMED,
    PBS_R_AUTHZ_DENIED,
    PBS_R_COUNT_
} pbs_reason_t;

const char *pbs_reason_str(pbs_reason_t r);
/* Returns -1 if the name is unknown. */
int pbs_reason_from_str(const char *s);

/* ---- CRC-32 (PBS-ENV-01 13; digest A10) --------------------------------- */

/* zlib crc32(): reflected polynomial 0xEDB88320, init and final XOR
 * 0xFFFFFFFF. pbs_crc32_update(0, ...) starts a new computation. */
uint32_t pbs_crc32_update(uint32_t crc, const void *data, size_t len);
uint32_t pbs_crc32(const void *data, size_t len);

/* ---- Header (PBS-ENV-01 4) ---------------------------------------------- */

typedef struct {
    uint8_t  magic;
    uint8_t  priority;
    uint8_t  flags;
    uint8_t  reserved03;
    uint16_t sequence;
    uint16_t reserved06;
    uint8_t  source_id[PBS_SOURCE_ID_LEN];
    uint64_t timestamp_us;
    uint32_t size;
    uint32_t ttl;
    uint32_t crc32;
} pbs_header_t;

/* Serialize every field as given, CRC32 field included. */
void pbs_header_write(const pbs_header_t *h, uint8_t out[PBS_HEADER_SIZE]);
/* Deserialize every field, CRC32 field included. */
void pbs_header_read(const uint8_t in[PBS_HEADER_SIZE], pbs_header_t *h);
/* CRC32 over the 44 bytes with bytes 0x28..0x2B taken as zero (13.1). */
uint32_t pbs_header_crc(const uint8_t hdr[PBS_HEADER_SIZE]);
/* Compute the CRC32 and store it big-endian at 0x28 (13.1, sender). */
uint32_t pbs_header_seal(uint8_t hdr[PBS_HEADER_SIZE]);

/* Encode a UTF-8 Source ID into 16 null-padded bytes. Refuses an encoding
 * longer than 16 bytes with PBS_E_SOURCE_ID instead of truncating (A4). */
int pbs_source_id_encode(const char *utf8, uint8_t out[PBS_SOURCE_ID_LEN]);
/* Display form: bytes up to the first 0x00; invalid UTF-8 sequences are
 * replaced by U+FFFD (A4). Output is NUL-terminated valid UTF-8; cap >= 49
 * always suffices. */
void pbs_source_id_display(const uint8_t sid[PBS_SOURCE_ID_LEN], char *out,
                           size_t cap);

/* Build header + payload. Sets h->magic, h->size and h->crc32; uses the
 * remaining fields of *h as given. Returns PBS_E_OK and *out_len. */
int pbs_envelope_build(pbs_header_t *h, const uint8_t *payload, size_t plen,
                       uint8_t *out, size_t cap, size_t *out_len);

/* ---- TTL, lifetime and class of service --------------------------------- */

/* PBS-ENV-01 12.2 in exact integer microseconds (A6): expired iff TTL > 0
 * and now_us - timestamp_us > TTL * 1000000. A Timestamp later than now_us
 * is not expired (A37). */
int pbs_ttl_expired(uint32_t ttl, uint64_t timestamp_us, int64_t now_us);

typedef enum {
    PBS_LT_OK = 0,              /* send with lifespan_s */
    PBS_LT_NO_EXPIRY,           /* TTL 0: send with the no-expiry lifetime */
    PBS_LT_GW_EXPIRED,          /* bound < 1 ms: do not encapsulate */
    PBS_LT_UNDER_1S             /* bound >= 1 ms but < 1 s: ION cannot carry */
} pbs_lt_outcome_t;

typedef struct {
    pbs_lt_outcome_t outcome;
    int     has_bound;          /* 0 for TTL 0 */
    int64_t c_us;               /* clock_us + PBS_LIFETIME_MARGIN_US */
    int64_t bound_ms;           /* PBS-DTN-MAP-01 6.1 upper bound */
    int64_t lifespan_s;         /* value for bp_send(); 0 when not sent */
} pbs_lifetime_t;

/* PBS-DTN-MAP-01 6.1 / 6.1.1 with the ION 4.2.0 rule of digest 7.4. */
void pbs_lifetime(uint32_t ttl, uint64_t timestamp_us, int64_t clock_us,
                  pbs_lifetime_t *out);
const char *pbs_lt_outcome_str(pbs_lt_outcome_t o);

/* PBS-DTN-MAP-01 6.3 table on ION classes (digest 7.5): 0,1 -> 2
 * (expedited), 2 -> 1 (standard), 3,4 -> 0 (bulk). -1 for priority > 4. */
int pbs_ion_class_of_service(uint8_t priority);
const char *pbs_priority_name(uint8_t priority);

/* Floor division for signed 64-bit values (b > 0). */
int64_t pbs_floor_div(int64_t a, int64_t b);

/* ---- MUX container (PBS-MUX-01 4) ---------------------------------------- */

typedef struct {
    uint8_t  type;
    uint16_t length;
    size_t   offset;            /* offset of the frame's type byte in payload */
    size_t   value_offset;      /* offset of the value in payload */
} pbs_frame_t;

typedef struct {
    uint8_t     version;
    uint8_t     count;
    pbs_frame_t frames[PBS_MUX_MAX_FRAMES];
    size_t      frames_end;     /* payload offset just after the last frame */
    size_t      padding;        /* bytes after the last frame */
} pbs_mux_t;

/* Returns PBS_R_OK, PBS_R_MUX_VERSION or PBS_R_MUX_MALFORMED (A13, A14). */
pbs_reason_t pbs_mux_parse(const uint8_t *payload, size_t len, pbs_mux_t *m);

typedef struct {
    uint8_t        type;
    const uint8_t *value;
    size_t         length;
} pbs_frame_in_t;

/* Encode version 1, frame_count and the frames, without padding. */
int pbs_mux_encode(const pbs_frame_in_t *frames, size_t n, uint8_t *out,
                   size_t cap, size_t *out_len);

/* ---- Authority context (PBS-AUTH-01 2) ----------------------------------- */

typedef struct {
    uint8_t        version;
    uint8_t        authority_id_type;
    uint8_t        authority_id_len;
    uint8_t        role_len;
    uint32_t       policy_epoch;
    const uint8_t *authority_id;
    const uint8_t *role;
} pbs_auth_ctx_t;

/* 0 if version is 1 and the value length is 8 + id_len + role_len,
 * -1 otherwise (digest 6.6 R12a). */
int pbs_auth_ctx_parse(const uint8_t *v, size_t len, pbs_auth_ctx_t *a);
int pbs_auth_ctx_encode(uint8_t authority_id_type, const char *authority_id,
                        const char *role, uint32_t policy_epoch, uint8_t *out,
                        size_t cap, size_t *out_len);

/* ---- Security frame (PBS-SEC-B-01 4) ------------------------------------- */

typedef struct {
    uint8_t        version;
    uint16_t       suite_id;
    uint8_t        flags;
    uint8_t        key_id_len;
    uint8_t        nonce_len;
    uint16_t       auth_len;
    uint64_t       security_sequence;
    const uint8_t *key_id;
    const uint8_t *nonce;
    const uint8_t *auth_data;
} pbs_sec_frame_t;

/* Structural parse: value length >= 16 and equal to
 * 16 + key_id_len + nonce_len + auth_len. Version is not checked here.
 * 0 on success, -1 otherwise. */
int pbs_sec_frame_parse(const uint8_t *v, size_t len, pbs_sec_frame_t *s);

/* ---- Keys and suites (digest 6.3) ---------------------------------------- */

#define PBS_KEY_ID_MAX 255

typedef struct {
    uint16_t suite_id;                 /* algorithm of the key */
    char     key_id[PBS_KEY_ID_MAX + 1];
    uint8_t  secret[32];               /* HMAC key or Ed25519 seed */
    int      has_secret;
    uint8_t  public_key[32];           /* Ed25519 only */
    int      has_public;
    /* Binding used for authorization (digest 5, R13). */
    int      has_binding;
    uint8_t  bound_source_id[PBS_SOURCE_ID_LEN];
    char     bound_authority[256];
} pbs_key_t;

/* auth_len of a demo suite, or -1 for an unknown suite. */
int pbs_suite_auth_len(uint16_t suite_id);
const char *pbs_suite_name(uint16_t suite_id);

/* Test key from the public label (digest 6.3):
 *   secret = SHA-256("PBS-ION-DEMO TEST KEY <key_id> (NOT FOR OPERATIONAL USE)")
 * For PBS_SUITE_ED25519 the public key is derived from the seed. */
int pbs_test_key_derive(const char *key_id, uint16_t suite_id, pbs_key_t *k);
/* Remove the secret of an Ed25519 key (verify-only copy). HMAC keys keep
 * their secret, which verification needs. */
void pbs_key_make_verify_only(pbs_key_t *k);
int pbs_key_bind(pbs_key_t *k, const char *source_id, const char *authority);

/* Compute auth_data (auth_len of the key's suite) over msg. */
int pbs_auth_compute(const pbs_key_t *k, const uint8_t *msg, size_t len,
                     uint8_t *out, size_t cap, size_t *out_len);
/* 1 valid, 0 invalid, negative on error. HMAC tags are compared in
 * constant time. */
int pbs_auth_verify(const pbs_key_t *k, const uint8_t *msg, size_t len,
                    const uint8_t *auth, size_t auth_len);

/* ---- Protected messages (digest 6.5) ------------------------------------- */

typedef struct {
    pbs_header_t          hdr;         /* priority, flags, sequence,
                                          source_id, timestamp_us, ttl and
                                          reserved bytes; size/crc computed */
    const pbs_frame_in_t *frames;      /* frames before the Security frame */
    size_t                nframes;
    int                   with_security;
    uint16_t              suite_id;    /* written in the Security frame */
    const char           *key_id;      /* written in the Security frame */
    uint64_t              security_sequence;
    const pbs_key_t      *signing_key; /* algorithm and material */
} pbs_protected_spec_t;

/* Build the envelope of digest 6.5. With with_security == 0 the frames are
 * sent as an unprotected MUX container. *auth_input_len receives
 * 44 + Size - auth_len (0 without security) if non-NULL. */
int pbs_build_protected(const pbs_protected_spec_t *s, uint8_t *out,
                        size_t cap, size_t *out_len, size_t *auth_input_len);

/* ---- Replay window (digest 6.4) ------------------------------------------ */

typedef struct {
    int      in_use;
    uint8_t  source_id[PBS_SOURCE_ID_LEN];
    uint8_t  key_id_len;
    uint8_t  key_id[PBS_KEY_ID_MAX];
    uint64_t highest;                  /* H, initially 0 */
    uint64_t mask;                     /* bit i set: H - i accepted */
} pbs_replay_slot_t;

typedef struct {
    pbs_replay_slot_t slots[PBS_REPLAY_SLOTS];
} pbs_replay_t;

void pbs_replay_init(pbs_replay_t *r);
/* 1 if s is acceptable, 0 if it is a replay (s = 0, s <= H - 64, repeat)
 * or no state slot is free (fail closed). */
int pbs_replay_check(const pbs_replay_t *r, const uint8_t src[16],
                     const uint8_t *key_id, size_t key_id_len, uint64_t s);
/* Record an accepted s. Returns 0, or -1 if no slot is free. */
int pbs_replay_record(pbs_replay_t *r, const uint8_t src[16],
                      const uint8_t *key_id, size_t key_id_len, uint64_t s);

/* ---- Receiver policy (digest 5) and receive path (digest 2.5, 6.6) ------- */

#define PBS_POLICY_MAX_ROLES 8
#define PBS_POLICY_MAX_OPS   16
#define PBS_POLICY_MAX_KEYS  8

typedef struct {
    char name[64];
    char ops[PBS_POLICY_MAX_OPS][32];
    size_t nops;
} pbs_role_t;

typedef struct {
    uint8_t    authority_id_type;
    char       authority_id[256];
    uint32_t   policy_epoch;
    pbs_role_t roles[PBS_POLICY_MAX_ROLES];
    size_t     nroles;
    pbs_key_t  keys[PBS_POLICY_MAX_KEYS];   /* provisioned verification keys */
    size_t     nkeys;
} pbs_policy_t;

/* The demo rover policy of digest 5: authority type 3 "PBS-DEMO-MCC",
 * epoch 1, roles FLIGHT-OPS and SCIENCE-OPS, verification keys
 * TEST-ED25519-01 (public key only) and TEST-HMAC-01, both bound to Source
 * ID "Earth-MCC" and authority "PBS-DEMO-MCC". */
int pbs_policy_demo(pbs_policy_t *p);
const pbs_key_t *pbs_policy_find_key(const pbs_policy_t *p, uint16_t suite,
                                     const uint8_t *key_id, size_t len);

typedef struct {
    pbs_reason_t   reason;
    int            accepted;
    int            have_header;       /* at least 44 bytes were present */
    pbs_header_t   hdr;
    uint32_t       crc_computed;
    size_t         total_len;
    int            have_mux;
    pbs_mux_t      mux;
    size_t         n_commands;
    size_t         n_security;
    size_t         n_authority;
    int            sec_parsed;        /* Security frame structure checked (R9) */
    int            have_sec;          /* R9b passed */
    pbs_sec_frame_t sec;
    int            authenticated;     /* R10 passed */
    int            have_auth_ctx;
    pbs_auth_ctx_t auth_ctx;
    int            protected_msg;     /* accepted with a Security frame */
} pbs_rx_t;

/* Run the digest 6.6 receive path on one bundle payload. buf holds
 * avail bytes of a payload whose real length is total_len (avail may be
 * smaller than total_len only when total_len > PBS_MAX_ENVELOPE; the
 * payload is then rejected with LENGTH after the header checks). The
 * replay state is updated as R11 specifies. Pointers in *rx refer to buf. */
pbs_reason_t pbs_receive_ex(const pbs_policy_t *pol, pbs_replay_t *rep,
                            const uint8_t *buf, size_t avail,
                            size_t total_len, int64_t now_us, pbs_rx_t *rx);
pbs_reason_t pbs_receive(const pbs_policy_t *pol, pbs_replay_t *rep,
                         const uint8_t *buf, size_t len, int64_t now_us,
                         pbs_rx_t *rx);

/* Command operation = Command frame value up to the first space (digest 5). */
size_t pbs_command_op(const uint8_t *v, size_t len, char *out, size_t cap);

/* ---- Hex helpers ----------------------------------------------------------- */

/* Lowercase hex; out needs 2 * len + 1 bytes. */
void pbs_hex_encode(const uint8_t *in, size_t len, char *out);
/* Returns number of bytes written or -1 on invalid input or overflow. */
long pbs_hex_decode(const char *hex, uint8_t *out, size_t cap);

uint16_t pbs_get_u16(const uint8_t *p);
uint32_t pbs_get_u32(const uint8_t *p);
uint64_t pbs_get_u64(const uint8_t *p);
void pbs_put_u16(uint8_t *p, uint16_t v);
void pbs_put_u32(uint8_t *p, uint32_t v);
void pbs_put_u64(uint8_t *p, uint64_t v);

#ifdef __cplusplus
}
#endif

#endif /* PBS_H */
