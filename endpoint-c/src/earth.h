/*
 * earth.h - Endpoint B message construction from plan items and JSON-lines
 * log records. Independent of ION so that it can be unit-tested.
 *
 * Copyright 2026 the PBS ION demo authors. Licensed under the Apache
 * License, Version 2.0 (see LICENSE at the repository root).
 */
#ifndef PBS_EARTH_H
#define PBS_EARTH_H

#include "json.h"
#include "pbs.h"

#define EARTH_ENDPOINT_NAME "earth-c"
#define EARTH_IMPL_NAME     "endpoint-c"
#define EARTH_MAX_KEYS      8
#define EARTH_MAX_SECSEQ    16
#define EARTH_LABEL_MAX     64
#define EARTH_EID_MAX       64

typedef struct {
    char     label[EARTH_LABEL_MAX];
    uint8_t *bytes;
    size_t   len;
} earth_labeled_t;

typedef struct {
    char     key_id[PBS_KEY_ID_MAX + 1];
    uint64_t next;
} earth_secseq_t;

typedef struct {
    char            source_id[PBS_SOURCE_ID_LEN + 1];
    uint16_t        next_sequence;          /* A5: first value 1, step 1 */
    pbs_key_t       keys[EARTH_MAX_KEYS];   /* signing material */
    size_t          nkeys;
    earth_secseq_t  secseq[EARTH_MAX_SECSEQ];
    size_t          nsecseq;
    earth_labeled_t *labeled;
    size_t          nlabeled;
    char            default_dest[EARTH_EID_MAX];
} earth_ctx_t;

typedef struct {
    /* what was asked */
    char     kind[16];
    char     label[EARTH_LABEL_MAX];
    char     replay_of[EARTH_LABEL_MAX];
    char     tamper[32];
    char     dest[EARTH_EID_MAX];
    char     note[128];
    /* envelope as handed to ION */
    uint8_t *env;                           /* PBS_MAX_ENVELOPE + 1 bytes */
    size_t   len;
    int      have_header;
    pbs_header_t hdr;
    /* protected messages */
    int      has_security;
    uint16_t suite_id;
    char     key_id[PBS_KEY_ID_MAX + 1];
    char     signing_key[PBS_KEY_ID_MAX + 1];
    uint64_t security_sequence;
    char     command[256];
    char     role[64];
    /* gateway decision (digest 7.4, 7.5) */
    pbs_lifetime_t lt;
    int      lifetime_override;
    int64_t  lifespan_s;
    int      send;                          /* 1: hand to bp_send() */
    char     drop_reason[32];               /* GW_EXPIRED, GW_LIFETIME_UNDER_1S */
    int      class_of_service;
} earth_item_t;

int  earth_ctx_init(earth_ctx_t *c, const char *source_id, const char *default_dest);
void earth_ctx_free(earth_ctx_t *c);
const pbs_key_t *earth_find_key(const earth_ctx_t *c, const char *key_id);

int  earth_item_alloc(earth_item_t *it);
void earth_item_free(earth_item_t *it);

/* Build the envelope for one plan item at clock reading now_us (Unix epoch
 * microseconds) and decide whether and how it is handed to ION. Advances
 * the Sequence and security_sequence counters for newly built envelopes.
 * Returns 0, or -1 with a message in err. Plan item fields: README.md. */
int earth_build(earth_ctx_t *c, const json_value_t *item, int64_t now_us,
                earth_item_t *out, char *err, size_t errcap);

/* Build an envelope from test-vector style inputs (docs/test-vectors.json
 * "inputs" objects, plus an optional "security" object with suite_id,
 * key_id, security_sequence and signing_key). No counters are used. */
int earth_build_from_inputs(const earth_ctx_t *c, const json_value_t *inputs,
                            const json_value_t *security, uint8_t *out,
                            size_t cap, size_t *out_len, char *err, size_t errcap);

/* Parse "0x0A", "10" or a JSON integer. -1 on error. */
long earth_parse_uint(const json_value_t *v);

/* ---- Envelope identity tracking (A5) -------------------------------------- */

typedef struct {
    uint8_t  (*keys)[PBS_SOURCE_ID_LEN + 2 + 8];
    uint32_t *counts;
    size_t   cap, n;
} earth_idset_t;

void earth_idset_init(earth_idset_t *s);
void earth_idset_free(earth_idset_t *s);
/* Returns how many times the identity was seen before this call. */
uint32_t earth_idset_add(earth_idset_t *s, const pbs_header_t *h);

/* ---- Log records: docs/LOG-SCHEMA.md, pbs-ion-demo-log/1 ------------------- */

#define EARTH_LOG_SCHEMA "pbs-ion-demo-log/1"
#define EARTH_IMPL       "endpoint-c 0.1.0"
#define EARTH_HEX_MAX    4096           /* envelope_hex up to this length */

typedef struct {
    const char *endpoint;               /* "B" */
    const char *node_eid;               /* EID opened, e.g. "ipn:23.1" */
    const char *scenario;               /* label, or NULL */
} earth_logctx_t;

typedef struct {
    int         have_bundle;
    const char *bundle_source_eid;      /* BpDelivery.bundleSourceEid */
    uint64_t    creation_ms;            /* BpDelivery.bundleCreationTime.msec */
    uint32_t    creation_count;
    uint32_t    ttl_field_ms;           /* BpDelivery.timeToLive (A30) */
    int         admin_record;
    const char *expected_eid;           /* Source ID map (A32); NULL if unknown */
    int         have_duplicates;
    uint32_t    duplicates_before;      /* earlier records with this identity */
} earth_bundle_meta_t;

/* Fields present in every record: schema, event, t_us, endpoint,
 * node_eid, scenario. Leaves the object open. */
void earth_log_begin(jw_t *w, const earth_logctx_t *lc, const char *event, int64_t t_us);

/* "send" record. t_us: read immediately before bp_send() (or when the
 * decision not to send was made). called: bp_send() was called; bp_rc: its
 * return value; t_return_us: when it returned. fail_reason: send reason
 * code when the item was to be sent but ION refused it (ZCO_FAILED,
 * BP_SEND_FAILED), else NULL. */
void earth_log_send(jw_t *w, const earth_logctx_t *lc, const earth_item_t *it, int64_t t_us,
                    int called, int bp_rc, int64_t t_return_us, const char *fail_reason);

/* Dry run of a plan item (pbs_tool plan): event "x_plan", the send record
 * fields with result "TO_SEND" or "NOT_SENT" and lifespan_s of the
 * decision, plus x_envelope_hex (all bytes, any length); nothing is handed
 * to ION. */
void earth_log_plan(jw_t *w, const earth_logctx_t *lc, const earth_item_t *it, int64_t t_us);

/* "recv" record for one bundle payload. buf holds avail bytes of an
 * rx->total_len byte payload; sha256 is the digest of all of it (NULL:
 * computed from buf when avail == total_len). meta may be NULL (offline). */
void earth_log_recv(jw_t *w, const earth_logctx_t *lc, const pbs_rx_t *rx, const uint8_t *buf,
                    size_t avail, const uint8_t *sha256, int64_t recv_us,
                    const earth_bundle_meta_t *meta);

/* "exec" record for Command frame frame_index of an accepted envelope. */
void earth_log_exec(jw_t *w, const earth_logctx_t *lc, const pbs_rx_t *rx, const uint8_t *buf,
                    unsigned frame_index, int64_t t_us);

/* Envelope decode used for "send" records and by pbs_tool crosscheck:
 * writes the docs/LOG-SCHEMA.md Section 4 fields for len bytes. */
void earth_log_envelope_fields(jw_t *w, const uint8_t *buf, size_t len);

/* ---- Counter state across processes ------------------------------------------ */

/* Sequence and per-key_id security_sequence counters, so that several
 * pbs_earth invocations in one scenario run continue one counter (digest
 * 6.4, A5). File: {"source_id": ..., "next_sequence": N,
 * "next_security_sequence": {"<key_id>": N, ...}}. A missing file is not an
 * error (counters start at 1). The Source ID in the file must match. */
int earth_state_load(earth_ctx_t *c, const char *path, char *err, size_t errcap);
/* Written to path.tmp and renamed. */
int earth_state_save(const earth_ctx_t *c, const char *path);

/* ---- Plan files ------------------------------------------------------------ */

typedef struct {
    double              at_s;     /* seconds after the plan time origin */
    const json_value_t *item;
    long                line;
    int                 rep;      /* repetition index for "count" > 1 */
} earth_plan_entry_t;

typedef struct {
    json_value_t       **items;
    size_t               nitems;
    earth_plan_entry_t  *entries; /* sorted by at_s, then file order */
    size_t               nentries;
} earth_plan_t;

/* One JSON object per line; blank lines and lines starting with '#' are
 * ignored. "count" N with "interval_s" D expands to N entries D apart. */
int  earth_plan_load(FILE *f, earth_plan_t *p, char *err, size_t errcap);
void earth_plan_free(earth_plan_t *p);

/* Unix epoch microseconds from CLOCK_REALTIME. */
int64_t earth_now_us(void);

#endif
