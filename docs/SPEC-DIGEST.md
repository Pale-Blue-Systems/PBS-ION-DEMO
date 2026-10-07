# PBS specification digest for the PBS ION demo

This document lists what Endpoint A (rover, Python) and Endpoint B (Earth, C) implement, with the specification clause for each rule. Endpoint B is written from the PBS specifications and this digest. Both endpoints implement the demo choices recorded here, so their agreement on those choices (reason codes, check order, canonicalization, replay window, lifetime rule) follows from this one document; it was written, and `docs/test-vectors.json` generated, by a separate step that ran PBS_LINK 0.1.4 (Section 10).

Two kinds of statement appear below:

- **Spec**: a rule taken from the cited section of PBS v1.5.0.
- **Demo**: a choice made for this demo where the specification leaves the choice open or says nothing. Each one is listed with its reason in Section 11 (A1 to A38).

## 1. Sources

| Item | Version used |
|---|---|
| PBS specifications | PBS-PROTOCOL-OPEN `main`, PBS v1.5.0 (2026-10-06), commit `f18957b8fd96eb7955ffa8b55081f8f5fd352ce1` |
| PBS-ENV-01 | 1.5 |
| PBS-PRIO-01 | 1.4 |
| PBS-SEC-A-01 | 1.5 |
| PBS-MUX-01 | 1.4 |
| PBS-SEC-B-01 | 1.5 |
| PBS-AUTH-01 | 1.4 |
| PBS-SVC-01 | 1.4 (not used by the demo; see A23) |
| PBS-DTN-MAP-01 | 1.5 |
| PBS-DTN-MAP-02 | 1.5 |
| PBS-CONFORMANCE-01 | 1.5 |
| PBS-CONFORMANCE-02 | 1.5 |
| PBS_LINK | 0.1.4, commit `54ae20ee650be9b31601f824a7cdecf4bce2dfb2` |
| ION | `ion-open-source-4.2.0`, commit `568df887cb9f18aa8ec013b1a566d83215e499ae` (as pinned in `ion/build_ion.sh`) |

ION behaviour stated in this document was read from the ION 4.2.0 source tree; file and line references are given. The scenarios test it at run time; this document reports no run-time result for ION.

## 2. Envelope (PBS-ENV-01)

### 2.1 Header layout

Spec (PBS-ENV-01 Section 4): a fixed 44-byte header followed by `Size` payload bytes. All multi-byte integers are big-endian. The 4-byte integers at 0x20, 0x24 and 0x28 are 4-byte aligned (Section 3).

| Offset | Field | Size | Type | Rule |
|---|---|---|---|---|
| 0x00 | Magic | 1 | u8 | `0x10` (Section 5) |
| 0x01 | Priority | 1 | u8 | 0 to 4 (Section 6, PBS-PRIO-01 Section 4) |
| 0x02 | Flags | 1 | u8 | bit 0 = ACK_REQ; bits 1 to 7 reserved (Section 7) |
| 0x03 | Reserved | 1 | u8 | sender writes `0x00` |
| 0x04 | Sequence | 2 | u16 | rolling counter (Section 8) |
| 0x06 | Reserved | 2 | u16 | sender writes `0x0000` |
| 0x08 | Source ID | 16 | char[16] | UTF-8, null-padded (Section 9) |
| 0x18 | Timestamp | 8 | u64 | Unix epoch microseconds (Section 10) |
| 0x20 | Size | 4 | u32 | payload length in bytes (Section 11) |
| 0x24 | TTL | 4 | u32 | seconds from Timestamp; `0` = never expires (Section 12) |
| 0x28 | CRC32 | 4 | u32 | Section 2.3 below |

Python `struct` format of the header: `'>B B B x H xx 16s Q I I I'` (PBS-ENV-01 Section 19).

### 2.2 Field rules

- **Magic.** Spec: senders write `0x10`; receivers reject any other value (Section 5, Section 14 step 1).
- **Priority.** Spec: values 5 to 255 are reserved; receivers discard them (Section 6; PBS-PRIO-01 Section 5.1). Relays do not change the byte (PBS-PRIO-01 Sections 5.1, 8, 10).
- **Flags.** Spec: senders requesting acknowledgement write `0x01`; reserved bits are written as zero and ignored by receivers (Sections 7 and 18). Demo: no acknowledgement message is defined by PBS v1.5.0; endpoints carry and log ACK_REQ and send no acknowledgement (A11).
- **Reserved bytes.** Spec: senders write zero (Section 4); receivers ignore them (Section 18). Demo: a receiver accepts an envelope with non-zero reserved bytes and a valid CRC32 (A2; vector RX-12).
- **Sequence.** Spec: increases monotonically per source device and rolls over from 65535 to 0 (Section 8; PBS-CONFORMANCE-01 Section 7). Demo: one counter per Source ID per scenario run, shared by all priorities; the first envelope of a run carries 1; each envelope adds 1 (A5).
- **Source ID.** Spec: 16 bytes of UTF-8, null-padded when shorter; bytes after the first `0x00` are `0x00`; unique within the deployment (Section 9). A 16-byte ID has no terminator (vectors ENV-09, ENV-10). Demo: senders refuse an ID whose UTF-8 encoding exceeds 16 bytes instead of truncating it; receivers compare the 16 raw bytes and do not discard on UTF-8 errors (A4).
- **Timestamp.** Spec: Unix epoch time in microseconds at message creation (Section 10). Demo: taken from the host clock when the envelope is built; all ION nodes and endpoints run on one host and share its clock (A37).
- **Size.** Spec: number of payload bytes after the header; `0` is a header-only message (Section 11). Demo: maximum 65536 bytes (A9). A bundle payload whose length is not `44 + Size` is discarded (A7, A8).
- **TTL.** Spec: set by the originator; not modified by relays or gateways (Section 12.1). Section 2.4 below.

### 2.3 CRC32

Spec (PBS-ENV-01 Section 13, Section 13.1; PBS-SEC-A-01 Sections 3.2 and 4.1; PBS-CONFORMANCE-01 Sections 4.3 and 5.1):

- Computed over all 44 header bytes (0x00 to 0x2B) with bytes 0x28 to 0x2B set to zero.
- IEEE 802.3 CRC-32 as computed by zlib `crc32()`: reflected polynomial `0xEDB88320`, initial value `0xFFFFFFFF`, final XOR `0xFFFFFFFF` (A10). Check value: CRC-32 of ASCII `123456789` is `0xCBF43926`.
- Stored big-endian at 0x28.
- Does not cover the payload (Section 16.2).
- Sender: build the header with zero CRC field, compute, write. Receiver: read the stored value, zero the field in a copy, compute, compare, discard on mismatch.
- Relays and gateways do not recompute CRC32; the originator's value is verified at every hop (PBS-SEC-A-01 Section 5.1; PBS-CONFORMANCE-01 Section 5.2).

Specification vectors (ENV-01 to ENV-03 built with PBS_LINK 0.1.4 in the generator run, Section 10; RX-09 rejected by the reference receiver and by PBS_LINK 0.1.4 in the same run):

| Vector | Content | CRC32 |
|---|---|---|
| ENV-01 | Section 13.2 header: Priority 0, Flags `0x01`, Sequence 1, Source ID `Rover-A`, Timestamp `1767225600000000`, Size 0, TTL 30 | `0x588721ED` |
| RX-09 | Same header with a CRC computed over bytes 0x00 to 0x27 only | `0x019507AC`, discarded |
| ENV-02 | Section 12.5 header with TTL 0 | `0x8757080E` |
| ENV-03 | Section 12.5 header with TTL 10 (what a v1.4.1 decrementing relay forwarded) | `0xCDE710AF` |

### 2.4 TTL and expiry

Spec:

- An envelope with TTL > 0 is expired when `current_time - Timestamp / 1000000 > TTL`, `current_time` in Unix epoch seconds (Section 12.2).
- TTL `0` never expires. No node discards it as TTL-expired at any age (Section 12.1; PBS-CONFORMANCE-01 Section 8).
- Receivers check expiry on receipt before processing, before forwarding stored messages, and periodically for stored messages (Section 12.2). Gateways discard expired envelopes (Section 12.1).
- Relays and gateways forward the 44 header bytes as received; every node computes the same expiry instant `Timestamp / 1000000 + TTL` (Sections 12.3 and 15).
- Relays and gateways keep a clock synchronized to Unix epoch time (Section 15).
- Discard for other reasons (storage exhaustion, bundle lifetime expiry in DTN) is not TTL expiry (Section 12.1).

Demo (A6): integer arithmetic in microseconds:

```
expired  <=>  TTL > 0  and  now_us - Timestamp > TTL * 1000000
```

A Timestamp later than `now_us` gives a negative age and is not expired; the comparison is made without overflow for any 64-bit Timestamp. This equals the Section 12.2 formula evaluated with exact real numbers. Vectors: RX-01 (T+5 s, accept), RX-02 (T+30 s, accept), RX-03 (T+30.000001 s, expired), RX-04 (T+31 s, expired), RX-05 (TTL 0 at 365 days, accept), RX-06 (TTL 10 at T+26 s, expired). RX-01, RX-02, RX-04 and RX-05 are the receiver checks of the PBS-ENV-01 Section 12.5 statements, at the clock values of the PBS-CONFORMANCE-01 Section 8 statements 1 and 2; the relay store-and-forward steps of those statements are not covered by them (A33).

Typical TTL values (Section 12.4, informative): critical alerts 0, commands 30, telemetry 60, science data 3600, bulk 86400.

### 2.5 Receive processing order

Spec (PBS-ENV-01 Section 14; Section 17; PBS-CONFORMANCE-01 Sections 4.2 and 9): magic, CRC32, priority, TTL, payload extraction. Failure at any step discards the envelope; processing continues with the next envelope and no persistent failure state results.

Demo reason codes for these steps (used in logs and in `test-vectors.json`):

| Step | Check | Reason code |
|---|---|---|
| R1 | at least 44 bytes | `LENGTH` |
| R2 | Magic = `0x10` | `MAGIC` |
| R3 | CRC32 | `CRC` |
| R4 | Priority <= 4 | `PRIORITY` |
| R5 | not expired (Section 2.4) | `TTL_EXPIRED` |
| R6 | `Size <= 65536` and buffer length = `44 + Size` | `LENGTH` |

PBS_LINK 0.1.4 `parse()` checks payload completeness before TTL and does not check for trailing bytes; the generator recorded its outcome for every receiver case (field `pbs_link_0_1_4_parse`). For RX-15 (one byte after the payload) PBS_LINK returns a parsed envelope; Endpoint A therefore checks `len == 44 + Size` itself.

### 2.6 Relay and gateway rules

Spec (PBS-ENV-01 Section 15; PBS-SEC-A-01 Section 5; PBS-CONFORMANCE-01 Section 8): verify CRC32 before forwarding; check expiry against the unchanged Timestamp and TTL; discard expired envelopes; forward the 44 header bytes unchanged; never discard TTL 0 as expired; never modify any header field.

In this demo the ION relay node ipn:50.1 is a bundle node only. It never parses PBS envelopes, so these rules apply to the two endpoints, which act as gateways (Section 7).

## 3. Priority (PBS-PRIO-01)

Spec (Sections 4, 5.1, 9):

| Value | Name | Meaning |
|---|---|---|
| 0 | CRITICAL | life- or safety-critical |
| 1 | HIGH | mission-critical operational |
| 2 | NORMAL | routine mission data |
| 3 | LOW | opportunistic or deferrable |
| 4 | BULK | non-urgent, high volume |

Lower value is higher priority. All five classes are supported. Priority applies to the whole envelope and every MUX frame in it; frames carry no priority of their own (PBS-PRIO-01 Section 9; PBS-MUX-01 Section 6). Higher priority SHOULD be forwarded first (Section 6); the scheduling algorithm is implementation-defined (PBS-CONFORMANCE-01 Section 6). In the demo, scheduling is performed by ION through the class-of-service mapping in Section 7.5.

## 4. MUX container (PBS-MUX-01)

PBS-SEC-B-01 Section 4 carries security metadata as MUX frame type `0x0A`, and PBS-AUTH-01 Section 2 carries authority context as frame type `0x08`, so commands use a MUX container.

Spec (Sections 3 and 4): when MUX is used, the envelope payload is a MUX container. All multi-byte fields are big-endian.

```
container = mux_version (u8) | frame_count (u8) | frame * frame_count | padding
frame     = type (u8) | length (u16) | value (length bytes)
```

Rules (Sections 4.1 to 4.3, 8, 9, 10):

- Reject unsupported `mux_version`. Demo: the only version is `0x01` (A13).
- Parse exactly `frame_count` frames in order; frames do not overlap or run past the payload.
- Bytes after the last frame are padding, ignored, never parsed as a frame.
- Unknown frame types are skipped using `length`.
- Frame boundaries are preserved; discarding one frame does not invalidate others.

Frame types (Section 5): `0x01` Telemetry, `0x02` Command, `0x03` Status, `0x04` Event, `0x07` Service Intent (PBS-SVC-01), `0x08` Authority Context (PBS-AUTH-01), `0x09` PNT Context (PBS-PNT-CTX-01), `0x0A` Security (PBS-SEC-B-01), `0x80` to `0xFF` application-defined.

Demo:

- Every envelope with `Size > 0` carries a MUX container; `Size = 0` is a header-only heartbeat (A12).
- A container whose TLV structure is inconsistent (fewer than `frame_count` complete frames, or a frame running past the payload end) is discarded as a whole with reason `MUX_MALFORMED`; an unsupported `mux_version` gives `MUX_VERSION` (A14; vectors RX-17 to RX-19).
- Telemetry, Status and Event frame values are ASCII text or opaque bytes; Command frame values are ASCII `<OPERATION>[ <arguments>]`, for example `DRIVE 10` (A15).
- Senders write no padding. A receiver rejects a container that has padding and no Security frame with `SEC_MISSING` (Section 6.6 R8a, A38; RX-20); a protected container has none (Section 6.2).

## 5. Authority Context (PBS-AUTH-01)

Spec (Section 2): MUX frame type `0x08`, value:

| Offset | Field | Size |
|---|---|---|
| 0 | version | 1, `0x01` |
| 1 | authority_id_type | 1 |
| 2 | authority_id_len | 1 |
| 3 | role_len | 1 |
| 4 | policy_epoch | 4 (big-endian, A21) |
| 8 | authority_id | authority_id_len |
| 8 + authority_id_len | role | role_len |

Value length is `8 + authority_id_len + role_len`. Namespaces (Section 4): 0 deployment-defined, 1 URI, 2 registered organization identifier, 3 mission authority identifier. The complete AUTH frame is covered by PBS-SEC-B-01 authentication when used for protected operations (Section 2). A protected command carries authenticated authority context (PBS-AUTH-REQ-002; PBS-SECB-REQ-005). The receiver evaluates source, authority, role, operation and current policy before accepting a command (REQ-003); `policy_epoch` identifies the policy generation (REQ-005); failure gives a deterministic rejection and audit record (REQ-006).

Demo rover policy (A24), also in `test-vectors.json` `sec_b.rover_policy`:

| Item | Value |
|---|---|
| authority_id_type | 3 |
| authority_id | ASCII `PBS-DEMO-MCC` |
| policy_epoch | 1 |
| role `FLIGHT-OPS` | may issue `PING`, `SET_HEATER`, `DRIVE`, `CAPTURE_IMAGE`, `SET_MODE` |
| role `SCIENCE-OPS` | may issue `CAPTURE_IMAGE` |
| key binding | keys `TEST-ED25519-01` and `TEST-HMAC-01` are bound to Source ID `Earth-MCC` and authority `PBS-DEMO-MCC` |

The operation is the Command frame value up to the first space.

## 6. Authenticated commands (PBS-SEC-B-01)

### 6.1 Security frame

Spec (Section 4): MUX frame type `0x0A`, value:

| Offset | Field | Size |
|---|---|---|
| 0 | version | 1, `0x01` |
| 1 | suite_id | 2 |
| 3 | flags | 1 |
| 4 | key_id_len | 1 |
| 5 | nonce_len | 1 |
| 6 | auth_len | 2 |
| 8 | security_sequence | 8 |
| 16 | key_id | key_id_len |
| 16 + key_id_len | nonce | nonce_len |
| 16 + key_id_len + nonce_len | auth_data | auth_len |

Multi-byte fields are big-endian (A21). Value length is `16 + key_id_len + nonce_len + auth_len`.

Spec (Section 3): a suite identifier defines the algorithm, key identifier format, nonce requirements, tag length and canonicalization. Section 7: `key_id` selects key material and contains no secret.

### 6.2 Protected data

Spec (Section 5): the suite authenticates a canonical representation containing the envelope fields for origin, priority, sequencing, timestamp, size and lifetime; Service Intent when present; Authority Context when present; PNT Context when marked protected; and the application payload. Header fields, TTL included, are not mutable network-layer fields. PBS-SECB-REQ-004: priority and Service Intent are authenticated.

Demo canonicalization, identical for both suites (A20):

```
auth_input = envelope[0 : 44 + Size - auth_len]
auth_data  = envelope[44 + Size - auth_len : 44 + Size]
```

`auth_input` is all 44 header bytes (CRC32 included) followed by the payload up to `auth_data`: the MUX header, every frame before the Security frame, and the Security frame's type, length and value up to and including `nonce`. Constraints: exactly one Security frame; it is the last frame; no padding follows it. Changing any header byte, any frame, `suite_id`, `key_id` or `security_sequence` invalidates `auth_data`.

### 6.3 Demo suites and keys

No suite registry exists in PBS v1.5.0 (A16). The demo defines two unregistered suites for test use only:

| suite_id | Name | Algorithm | auth_len | nonce_len | flags |
|---|---|---|---|---|---|
| `0xFF01` | PBS-DEMO-TEST-HMAC-SHA256 | HMAC-SHA-256, 32-byte key, untruncated tag | 32 | 0 | `0x01` |
| `0xFF02` | PBS-DEMO-TEST-ED25519 | Ed25519 (RFC 8032, pure), 32-byte seed | 64 | 0 | `0x01` |

- `flags`: bit 0 (`0x01`) = authentication. Receivers reject any other value (A17).
- `key_id`: ASCII, 1 to 255 bytes; the key is looked up by (suite_id, key_id) (A18).
- `nonce_len` is 0; both algorithms are deterministic, so identical inputs give identical bytes (A19).
- Both endpoints implement both suites. Endpoint A uses the Python standard `hmac` module and the `cryptography` package; Endpoint B uses OpenSSL libcrypto. S6 uses `0xFF02`: Earth signs with the Ed25519 seed, the rover holds only the public key.

Test keys (derivations and values in `test-vectors.json` `sec_b.test_keys`). These are TEST KEYS derived from public labels:

| key_id | Suite | Provisioned at rover | Derivation |
|---|---|---|---|
| `TEST-ED25519-01` | `0xFF02` | yes (public key `462d874ceff2700bf1e04fc01405357c2980f342b26665c3755f5d016677f01f`) | seed = SHA-256(`PBS-ION-DEMO TEST KEY TEST-ED25519-01 (NOT FOR OPERATIONAL USE)`) |
| `TEST-HMAC-01` | `0xFF01` | yes | key = SHA-256(`PBS-ION-DEMO TEST KEY TEST-HMAC-01 (NOT FOR OPERATIONAL USE)`) |
| `TEST-ED25519-ROGUE` | `0xFF02` | no | seed = SHA-256(`PBS-ION-DEMO TEST KEY TEST-ED25519-ROGUE (NOT FOR OPERATIONAL USE)`) |
| `TEST-HMAC-ROGUE` | `0xFF01` | no | key = SHA-256(`PBS-ION-DEMO TEST KEY TEST-HMAC-ROGUE (NOT FOR OPERATIONAL USE)`) |

OpenSSL 3.0 calls for Endpoint B: `HMAC(EVP_sha256(), ...)`; Ed25519 with `EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, ...)` or `EVP_PKEY_new_raw_public_key(...)` and `EVP_DigestSign` / `EVP_DigestVerify` with a NULL digest.

### 6.4 Replay protection

Spec (PBS-SECB-REQ-003): replay validation uses security sequence, nonce state, transaction identity or a suite-defined equivalent.

Demo (A22):

- Senders: `security_sequence` starts at 1 per key per scenario run and increases by 1 per protected message.
- Receivers keep, per (Source ID 16 raw bytes, key_id), the highest accepted value `H` (initially 0) and the set of accepted values in `(H - 64, H]`.
- Accept `s` when `s > H`, or when `H - 64 < s <= H` and `s` is not in the set. Reject `s = 0`, `s <= H - 64`, and repeats, with reason `SEC_REPLAY`.
- State is updated after authentication succeeds and before authorization (step R11 below). An envelope rejected at an earlier step does not consume its `security_sequence` (vector SB-11).

### 6.5 Send procedure (Earth, protected command)

1. Frames before the Security frame, in this order: Authority Context (`0x08`), Command (`0x02`). Demo commands carry no Service Intent frame (A23).
2. Security frame metadata: version 1, suite_id, flags `0x01`, key_id, nonce empty, auth_len of the suite, next security_sequence.
3. `Size = 2 + sum(3 + len(value))` over all frames, with the Security frame value length `16 + key_id_len + auth_len`.
4. Build the 44-byte header with that Size and compute CRC32 (Section 2.3).
5. `auth_data = MAC or signature over (header || payload without auth_data)`; append it.

Demo command defaults: Source ID `Earth-MCC`, Priority 1 (HIGH), Flags `0x00`, TTL 30 (PBS-ENV-01 Section 12.4).

### 6.6 Receive procedure (rover)

Spec order: PBS-ENV-01 Section 14 first; authenticate before application acceptance (PBS-SECB-REQ-001); reject on authentication failure (PBS-SECB-REQ-002); replay validation (PBS-SECB-REQ-003); authenticated authority context for commands (PBS-SECB-REQ-005); authorize before execution (PBS-SECB-REQ-006); audit security failures (PBS-SECB-REQ-007). Demo order and reason codes (A26), implemented by `Receiver.receive` in `docs/gen_test_vectors.py`:

| Step | Check | Reason code on failure |
|---|---|---|
| R1 to R6 | Section 2.5 | `LENGTH`, `MAGIC`, `CRC`, `PRIORITY`, `TTL_EXPIRED`, `LENGTH` |
| R6a | `Size = 0`: accept, no frames | none |
| R7 | MUX version and TLV structure | `MUX_VERSION`, `MUX_MALFORMED` |
| R8 | no Security frame, and a Command frame | `SEC_MISSING` |
| R8a | no Security frame, and at least one of: an Authority Context frame; padding after the last frame; a Source ID to which a provisioned key is bound (`Earth-MCC` at the rover) (A38) | `SEC_MISSING` |
| R8b | no Security frame: accept as unprotected data | none |
| R9 | one Security frame, last, no padding; value length >= 16; version 1; value length = `16 + key_id_len + nonce_len + auth_len` | `SEC_MALFORMED` |
| R9a | suite_id is `0xFF01` or `0xFF02` | `SEC_SUITE_UNKNOWN` |
| R9b | flags `0x01`, nonce_len 0, auth_len of the suite, key_id_len >= 1 | `SEC_MALFORMED` |
| R9c | (suite_id, key_id) provisioned | `SEC_KEY_UNKNOWN` |
| R10 | verify `auth_data` over `auth_input` (Section 6.2) | `SEC_AUTH_FAIL` |
| R11 | replay window (Section 6.4); then record `s` | `SEC_REPLAY` |
| R11a | no Command frame: accept (protected data) | none |
| R12 | exactly one Authority Context frame | `AUTH_CTX_MISSING` (none), `AUTH_CTX_MALFORMED` (more than one) |
| R12a | AUTH version 1, value length = `8 + authority_id_len + role_len` | `AUTH_CTX_MALFORMED` |
| R13 | Source ID and authority match the key binding; authority_id_type 3; policy_epoch 1; role known; every Command operation permitted for the role | `AUTHZ_DENIED` |
| R14 | accept; execute Command frames in frame order | `OK` |

A header-only envelope (Size 0) is accepted at R6a; it carries no frames, and R8a does not apply to it.

Unknown frame types, including `0x80` to `0xFF`, are skipped in every step and stay covered by authentication (vector CMD-16, PBS-C02-T014).

### 6.7 Audit records

Spec: security failures are auditable (PBS-SECB-REQ-007); authorization failure produces an auditable status (PBS-AUTH-REQ-006); PBS-C02-T015 checks traceable event evidence. Demo (A25): each endpoint writes one JSON line per received envelope with at least: receive time (us), endpoint, result (`ACCEPT` or `REJECT`), reason code, Source ID, Sequence, Priority, Timestamp, TTL, Size, header hex, bundle source EID, bundle creation time, and for protected messages suite_id, key_id and security_sequence.

## 7. DTN mapping (PBS-DTN-MAP-01 Sections 5 to 8, PBS-DTN-MAP-02)

### 7.1 Roles and EIDs

PBS-DTN-MAP-01 Section 4 places the mapping at a gateway. In this demo each endpoint builds its envelopes and hands them to the ION node on the same host, so each endpoint performs the gateway steps for its own traffic (A27).

| Source ID | ION node | Source EID |
|---|---|---|
| `Rover-Alpha` | rover node 99 | `ipn:99.1` |
| (none, relay only) | Lunar Gateway relay node 50 | `ipn:50.1` |
| `Earth-MCC` | Earth node 23 | `ipn:23.1` |

Spec (PBS-DTN-MAP-01 Section 8; PBS-DTN-MAP-02 Section 3): the Source ID to EID mapping is deterministic and stable within the deployment; destination EIDs are gateway configuration. The PBS v1.5.0 changelog lists a known issue: a mapping to the EID of a node other than the bundle's source node conflicts with RFC 9171 Section 5.2. The demo maps each Source ID to an endpoint on the node where that endpoint runs, which avoids it (A28). ION 4.2.0 `bp_send()` returns an error when the source EID's node number is not the local node (`bpv7/library/libbp.c` lines 1533 to 1555).

ION 4.2.0 allows one receiving process per endpoint: `bp_open()` fails with "Endpoint is already open" when another live process holds it (`libbp.c` lines 98 to 145). `bp_open_source()` opens an endpoint for sending only (`bpv7/include/bp.h` line 467). `bpsendfile` opens its own EID with `bp_open()` (`bpv7/utils/bpsendfile.c` line 85).

### 7.2 One envelope per bundle

Spec (PBS-DTN-MAP-01 Sections 5.1, 6.2, 6.4, 7.2; PBS-DTN-MAP-02 Section 2): each envelope maps to exactly one bundle; the whole envelope (header and payload) is the bundle payload block; no envelope is split across bundles; the DTN layer does not interpret or alter it; on delivery the envelope is extracted verbatim. Demo: a delivered payload whose length is not `44 + Size` is discarded (A8).

### 7.3 Primary block

Spec (PBS-DTN-MAP-01 Section 6.1):

| PBS field | BPv7 field | Rule |
|---|---|---|
| Source ID | Source EID | Section 7.1 table |
| (gateway configuration) | Destination EID | rover sends to `ipn:23.1`, Earth sends to `ipn:99.1` |
| TTL | Lifetime | Section 7.4 |
| (gateway BPA) | Creation timestamp | assigned by the BPA per RFC 9171 Section 4.2.7, not from envelope fields; the sequence number is not derived from `Sequence` |
| Sequence | none | carried in the envelope only |
| Priority | none | carried in the envelope only; network treatment per Section 7.5 |

With ION, `bp_send()` assigns the creation timestamp; the endpoint passes no creation time.

### 7.4 Bundle lifetime

Spec, TTL > 0 (PBS-DTN-MAP-01 Section 6.1): the lifetime does not exceed

```
floor((TTL * 1000000 - max(0, c_us - Timestamp)) / 1000)  ms
```

where `c_us` is the bundle creation time in Unix epoch microseconds, `(creation_time_ms + 946684800000) * 1000`. If the value is less than 1 ms the envelope is not encapsulated. A gateway that computes the lifetime before the BPA assigns the creation time uses a `c_us` not earlier than that creation time, such as its clock reading plus the maximum latency of the send request.

Spec, TTL = 0 (PBS-DTN-MAP-01 Section 6.1.1; PBS-DTN-MAP-02 Section 4): the bundle lifetime is the gateway's documented no-expiry lifetime: greater than 0 ms, not less than any lifetime the gateway assigns to an envelope with TTL > 0, at most 4294967295000 ms, accepted by the BPA without overflow in its expiration time.

ION 4.2.0 facts used for the demo values:

- `bp_send()` takes `int lifespan` in seconds (`bpv7/include/bp.h` line 554) and passes `lifespan * 1000` ms to `bpSend()` (`libbp.c` line 1561). `bpSend()` rejects a lifespan of 0 (`libbpP.c` line 6555).
- The bundle's `timeToLive` is a `uvast` in ms (`bpv7/library/bpP.h` line 271); the expiration time is a `time_t` in seconds computed as `(creation ms + timeToLive) / 1000 + EPOCH_2000_SEC` (`libbpP.c` lines 5852 to 5857). `time_t` is 64-bit on x86-64 Linux.
- `BpDelivery.timeToLive`, which a receiving application reads, is an `unsigned int` in ms (`bp.h` line 414). For a 2147483647 s lifetime it holds 2147483647000 mod 2^32 = 4294966296. Receivers log the lifetime from the sender's log, not from this field.
- `bpsendfile` takes the lifetime in seconds as its fifth argument and uses 300 s when it is omitted (`bpsendfile.c` lines 212 and 251 to 253).

Demo lifetime rule (A29, A30), computed by the sending endpoint immediately before `bp_send()` (or the ION tool call):

```
if TTL == 0:          lifespan_s = 2147483647                       (no-expiry lifetime)
else:
    c_us      = now_us + 1000000                                    (1 s latency margin)
    bound_ms  = floor((TTL * 1000000 - max(0, c_us - Timestamp)) / 1000)
    if bound_ms < 1:  do not send; log GW_EXPIRED
    lifespan_s = min(floor(bound_ms / 1000), 2147483647)
    if lifespan_s < 1: do not send; log GW_LIFETIME_UNDER_1S
```

The no-expiry lifetime is 2147483647 s = 2147483647000 ms, the largest value `bp_send()` accepts, documented for ION 4.2.0 in the mapping profile, `docs/MAPPING-PROFILE.md` Section 6, as PBS-DTN-MAP-01 Section 6.1.1 and PBS-DTN-MAP-02 Section 4 require. Vectors LT-01 to LT-10 give the results for ten inputs, for example TTL 30 handed over at Timestamp gives 29 s (LT-01), and TTL 4294967295 is clamped to 2147483647 s (LT-09).

### 7.5 Priority and class of service

Spec (PBS-DTN-MAP-01 Section 6.3): no primary block field carries Priority; reserved or unassigned bundle processing control flags, bits 7 and 8 included, are not used for it; a gateway requesting the Expedited, Normal and Bulk classes of RFC 4838 Section 3.5 uses this table; a gateway requesting network treatment on the basis of priority documents a mapping profile with the PBS-DTN-MAP-02 Section 5 items.

| PBS Priority | DTN class | ION `classOfService` | ION ordinal |
|---|---|---|---|
| 0 CRITICAL | Expedited | 2 (`BP_EXPEDITED_PRIORITY`) | 0 |
| 1 HIGH | Expedited | 2 | 0 |
| 2 NORMAL | Normal | 1 (`BP_STD_PRIORITY`) | 0 |
| 3 LOW | Bulk | 0 (`BP_BULK_PRIORITY`) | 0 |
| 4 BULK | Bulk | 0 | 0 |

Constants: `bp.h` lines 52 to 54. With `bpsendfile` the class is passed as the string `0.<classOfService>` (format `<custody-requested>.<priority>[.<ordinal>...]`, `bp.h` `BP_PARSE_QUALITY_OF_SERVICE_USAGE`). Demo: ancillary data NULL, ordinal 0, no custody (A31).

The mapping profile with the four PBS-DTN-MAP-02 Section 5 items (BP QoS mechanism: `classOfService` of `bp_send()`, carried in ION's block type 193; queue treatment: urgent queue first, then the standard and bulk queues shared 2:1 by bytes sent; behaviour under congestion, measured in S4; behaviour when the treatment is unavailable) and the no-expiry lifetime of PBS-DTN-MAP-02 Section 4 is `docs/MAPPING-PROFILE.md`.

### 7.6 Inbound

Spec: DTN-layer validation first; invalid bundles are discarded before PBS processing (PBS-DTN-MAP-01 Section 7.1). The envelope is extracted verbatim (Section 7.2). A bundle deleted for lifetime expiry loses its envelope; that is not TTL expiry (Sections 6.1.1, 7.3). The receiver applies the PBS-ENV-01 Section 12.2 check to the extracted envelope regardless of bundle delivery status (Section 7.3; PBS-DTN-MAP-02 Section 4, last paragraph). Demo: receivers log the bundle source EID and whether it matches the Section 7.1 table; a mismatch is logged, not a discard reason (A32).

### 7.7 Other PBS-DTN-MAP-02 clauses

- Section 6: Service Intent rows do not apply; no Service Intent frames are sent (A23). The row "no finite deadline, maximum age, TTL or mission expiry limit" applies to TTL 0 envelopes (Section 7.4).
- Section 7: BPSec is not configured. PBS-SEC-B-01 is applied to commands by mission profile (Section 6).
- Section 8: contact plans, routes and convergence layers are ION configuration.
- Section 9: BP status reports, if used, are logged separately from PBS receipt and application acceptance.
- Section 10: the verification items are mapped to scenarios in Section 9.

## 8. Demo profile constants

| Constant | Value | Reference |
|---|---|---|
| Header size | 44 | PBS-ENV-01 Section 4 |
| Maximum payload | 65536 bytes | A9 |
| MUX version | `0x01` | A13 |
| Rover Source ID / EID | `Rover-Alpha` / `ipn:99.1` | Section 7.1 |
| Earth Source ID / EID | `Earth-MCC` / `ipn:23.1` | Section 7.1 |
| Suites | `0xFF01` HMAC-SHA-256, `0xFF02` Ed25519 | Section 6.3 |
| Replay window | 64 | Section 6.4 |
| Policy epoch | 1 | Section 5 |
| Lifetime latency margin | 1000000 us | Section 7.4 |
| No-expiry lifetime | 2147483647 s | Section 7.4 |
| Envelope identity in logs | (Source ID, Sequence, Timestamp) | A5 |

## 9. Scenario to conformance mapping

PBS-CONFORMANCE-01 defines no test identifiers; it is cited by section, and the Section 8 test statements are numbered 1 (TTL 30) and 2 (TTL 0) in order of appearance (A33). PBS-CONFORMANCE-02 test identifiers (`PBS-C02-T001` to `PBS-C02-T016`) are from its Section 3, requirement identifiers from PBS-SEC-B-01 Section 6 (`PBS-SECB-REQ-nnn`) and PBS-AUTH-01 Section 3 (`PBS-AUTH-REQ-nnn`). The method letters T, D, A, I are read as Test, Demonstration, Analysis, Inspection (A34). The demo exercises these tests and makes no conformance claim and no certification claim.

| Scenario | PBS-CONFORMANCE-02 | PBS-CONFORMANCE-01 | Other clauses checked |
|---|---|---|---|
| S1 nominal traffic | PBS-C02-T002 (header byte-identical after BPv7 carriage), PBS-C02-T010 (partial: one static class per priority), PBS-C02-T015 (dispositions logged), PBS-C02-T016 (TTL 0 envelopes carried with the 2147483647 s lifetime of `docs/MAPPING-PROFILE.md` and delivered) | 4.1, 4.2, 4.3, 5.1, 6, 7, 10 | DTN-MAP-01 5.1, 6.1, 6.1.1, 6.2, 7.2, 8; DTN-MAP-02 10 (envelope preservation, EID mapping stability, no-expiry lifetime) |
| S2 scheduled outage | PBS-C02-T011, PBS-C02-T002 (header unchanged after storage in ION) | 4.2 | DTN-MAP-01 9; DTN-MAP-02 10 (store-and-forward delivery); C02 Section 4 (complete outage, delayed contact, service restoration) |
| S3 unscheduled blackout and loss | PBS-C02-T011, PBS-C02-T002 | 4.2 | C02 Section 4 (complete outage, service restoration, link impairment model) |
| S4 priority under a rate limit | PBS-C02-T012 (partial: no authorized degradation policy), PBS-C02-T010 (partial: one static class per priority, Section 7.5 table) | 6 | PRIO-01 4, 6; DTN-MAP-01 6.3; DTN-MAP-02 5 (`docs/MAPPING-PROFILE.md`), 10 (priority semantic preservation); C02 Section 4 (constrained throughput) |
| S5 expiry | PBS-C02-T004, PBS-C02-T011 (expiry), PBS-C02-T016 | 4.2, 8 (receiver side of statements 1 and 2: vectors RX-01, RX-02, RX-04, RX-05; the relay store-and-forward steps are not exercised; discard of expired envelopes by the endpoints as gateways: S5-1, S5-2), 9 | ENV-01 12.1 to 12.5; DTN-MAP-01 6.1, 6.1.1, 7.3; DTN-MAP-02 4, 10 (deadline-to-lifetime bounding, expiry during disruption, no-expiry lifetime) |
| S6 authenticated commands | PBS-C02-T006 (command byte, priority, TTL, Timestamp, security_sequence, Authority Context, CRC32 and frame_count modified; wrong keys), PBS-C02-T007 (replay), PBS-C02-T008 (authorized and unauthorized role, wrong policy epoch, unknown authority), PBS-C02-T004 (expired command), PBS-C02-T014, PBS-C02-T015 (audit) | 9 | PBS-SECB-REQ-001 to PBS-SECB-REQ-007; PBS-AUTH-REQ-002, PBS-AUTH-REQ-003, PBS-AUTH-REQ-005, PBS-AUTH-REQ-006; PRIO-01 10 |
| S7 cross-implementation | PBS-C02-T014 (unknown frame skipped, live and RX-21, CMD-16), PBS-C02-T002 | 4.1, 4.3, 5.1, 7, 10 | ENV-01 12.5, 13.2 vectors |

Not exercised: PBS-C02-T001 (IP carriage), PBS-C02-T003 (provider or path transition), PBS-C02-T005 (latest-state degradation), PBS-C02-T009 (PNT context), PBS-C02-T013 (security layering with BPSec); asymmetric link availability (C02 Section 4: every outage takes down both directions of a leg); the relay steps of PBS-CONFORMANCE-01 Section 8 statements 1 and 2 (no PBS relay); DTN-MAP-02 Section 10 items "duplicate handling" (no delivery mode is declared and no criterion is designed for it; repeated deliveries are listed per run in REPORT.md) and "PBS-SEC-B preservation with BPSec enabled" (A35).

Records: PBS-CONFORMANCE-02 Section 4 lists what the test environment records (endpoint and gateway implementations, emulator, clock source and uncertainty, impairment model, contact plan, cryptographic suite, test-vector hashes); Section 5 lists the fields of each verification record (test and requirement IDs, configuration, procedure revision, input vectors, expected and observed result, disposition, logs). REPORT.md carries these fields per scenario and the SHA-256 of the `test-vectors.json` used.

## 10. Test vectors

File: `docs/test-vectors.json`. Generator: `docs/gen_test_vectors.py`.

```
PYTHONPATH=<PBS_LINK 0.1.4 checkout> python3 docs/gen_test_vectors.py
```

First generated on 2026-10-07 with PBS_LINK 0.1.4 at commit `54ae20e` (15 envelopes, 22 receiver cases, 10 lifetime cases, 24 SEC-B messages, 21 SEC-B cases with 30 deliveries; SHA-256 `789ac1b4a02f38e8adf3d9bc1eeabe4d0bb7abc74a95b3dd8439633bcb3885ff`). Regenerated on 2026-10-07 after rule R8a (A38) with Python 3.13.16, cryptography 50.0.1 and PBS_LINK 0.1.4 at commit `d3b81d3` (`build/pbs-link`, fetched by `endpoint-python/fetch_pbs_link.sh`; its `PBS_LINK/` package tree is the one of `54ae20e`). Output: 15 envelopes, 23 receiver cases, 10 lifetime cases, 28 SEC-B messages and 25 SEC-B cases with 34 deliveries; all self-checks passed. Two runs produced identical files, SHA-256 `51e09856fa3c59130aa01f9c5d0e661a26cd19bda2aec74c45f8084bffd69b25`. Compared with the first file, RX-20's expected result changed (`ACCEPT OK` to `REJECT SEC_MISSING`) and RX-23, CMD-19 to CMD-22 and SB-22 to SB-25 were added; every other vector is unchanged.

Self-checks in that run:

- Every envelope built with `PBSLink.send` (fixed clock) is byte-identical to an independent `struct`/`zlib` build of the Section 4 layout and parses back with `PBS_LINK.parse_envelope`.
- ENV-01 and ENV-02 equal the header listings of PBS-ENV-01 Sections 13.2 and 12.5 byte for byte; ENV-01, ENV-02 and ENV-03 have the CRC32 values `0x588721ED`, `0x8757080E` and `0xCDE710AF` stated in Sections 13.2 and 12.5.
- CRC-32 of `123456789` is `0xCBF43926`.
- Every receiver case and SEC-B delivery gives the stated result and reason code in the reference receiver.
- Every SEC-B header built by PBS_LINK equals the header used for `auth_input`.

Cross-check with OpenSSL 3.0.13 command-line tools, run on 2026-10-07: `openssl pkeyutl -verify -rawin` verified the Ed25519 signatures of CMD-01, CMD-10 and CMD-W70 with the `TEST-ED25519-01` public key and reported failure for CMD-07 (signed with the rogue key); `openssl pkeyutl -sign` with the `TEST-ED25519-01` seed reproduced the CMD-01 signature; `openssl dgst -sha256 -mac HMAC` reproduced the CMD-02 tag. After the regeneration, `openssl pkeyutl -verify -rawin` verified the CMD-22 signature.

Contents:

| Section | Content |
|---|---|
| `envelopes` | ENV-01 to ENV-15: Section 13.2 and 12.5 vectors; one telemetry envelope per priority 0 to 4; 16-byte ASCII and 16-byte multi-byte UTF-8 Source IDs; Sequence 65535 and 0; TTL 0, TTL 4294967295; Timestamp with a microsecond fraction; ACK_REQ set; Size 0 and Size up to 261. Each has inputs, header, CRC32 and full envelope hex. |
| `receiver_cases` | RX-01 to RX-23: TTL boundaries, bad magic, bad CRC, reserved priority, reserved bytes and flag bits, truncated and over-long buffers, MUX version and structure errors, padding without a Security frame, unknown frame, empty container, Authority Context without a Security frame. Each has input hex, `now_us`, expected result and reason, and the PBS_LINK 0.1.4 `parse()` outcome. |
| `lifetime_cases` | LT-01 to LT-10: Section 7.4 inputs and results. |
| `cos_map` | Section 7.5 table. |
| `sec_b.messages` | CMD-01 to CMD-22 and CMD-W00 to CMD-W70: valid Ed25519 and HMAC commands; tampered command byte, priority, TTL and security_sequence; wrong key for each suite; unknown key_id; unauthorized and authorized roles; wrong policy epoch; unsigned command; missing Authority Context; unknown suite; unknown frame inside a protected command; Source ID not bound to the key; padding after the Security frame; `frame_count` lowered to 1 and 0; unsigned and signed Earth-MCC telemetry; replay-window messages. Each untampered message lists `auth_input_hex` and `auth_data_hex`. |
| `sec_b.cases` | SB-01 to SB-25: ordered deliveries, each case starting from empty receiver state, with `now_us` and expected result and reason. |

Use: each endpoint builds every `envelopes` and `sec_b.messages` entry from its `inputs` and compares the bytes (S7, identical bytes for identical inputs); parses every `envelope_hex` and compares the fields; runs `receiver_cases` and `sec_b.cases` through its receive path with the stated `now_us`; and computes `lifetime_cases`.

## 11. Ambiguities and resolutions

| ID | Text and issue | Resolution |
|---|---|---|
| A1 | PBS-ENV-01 Section 3, PBS-SEC-A-01 Section 3 and PBS-CONFORMANCE-01 Sections 4.1, 12 and 14 name "PBS-ENV-01 v1.3"; PBS-ENV-01 Section 19 and PBS-CONFORMANCE-01 Section 11.1 name PBS_LINK 0.1.3. | The v1.3 header is the v1.5 header; the v1.5.0 changelog states that the wire format is unchanged. PBS_LINK 0.1.4 is used. |
| A2 | PBS-ENV-01 Section 4 says reserved bytes "MUST be 0x00"; Section 18 says receivers ignore reserved fields; neither says whether a receiver discards non-zero reserved bytes. | Senders write zero. Receivers do not discard; the bytes stay covered by CRC32 and are forwarded unchanged. RX-12. |
| A3 | Flags bits 1 to 7: senders write zero, receivers ignore (Sections 7, 18). | Applied as written. RX-13 (Flags `0xFF`) is accepted with ACK_REQ = 1. |
| A4 | PBS-ENV-01 Section 9 limits the Source ID to 16 bytes; Section 19 and PBS_LINK truncate a longer encoding at byte 16, which can split a UTF-8 character; Sections 14 and 17 give no discard rule for invalid UTF-8 or non-zero bytes after the terminator. | Senders refuse IDs longer than 16 bytes (Endpoint A checks before calling PBS_LINK). Receivers keep the 16 raw bytes for comparison and logging; for display they decode up to the first `0x00` with replacement characters. No discard for encoding errors. |
| A5 | PBS-ENV-01 Section 8 gives no first value, no step size, no rule across restarts, and does not say whether one counter serves all priorities. | One counter per Source ID per run, first value 1, step 1, 65535 to 0. Receivers compute gaps per run after the run, because priority scheduling reorders arrivals. Envelopes are identified by (Source ID, Sequence, Timestamp). |
| A6 | PBS-ENV-01 Section 12.2 does not say whether `current_time` is an integer or has a fractional part. | Exact integer microseconds: expired iff TTL > 0 and `now_us - Timestamp > TTL * 1000000`. Matches the PBS-CONFORMANCE-01 Section 8 statements at whole seconds. PBS_LINK 0.1.4 uses floating-point seconds; in the generator run it gave the same decision as this rule for RX-01 to RX-06, including RX-03 (1 us past expiry). |
| A7 | PBS-ENV-01 Sections 14 and 17 do not name an envelope shorter than `44 + Size`. | Discarded at step 5 (payload extraction) with reason `LENGTH`. RX-14, RX-16. |
| A8 | No clause covers bytes after `44 + Size`. | In BP carriage the payload block holds exactly one envelope (PBS-DTN-MAP-01 Sections 5.1, 6.2); a payload of any other length is discarded with `LENGTH`. RX-15; PBS_LINK 0.1.4 `parse()` accepts it, so Endpoint A checks the length. |
| A9 | Maximum payload size is implementation-defined (PBS-ENV-01 Sections 11, 16.3). | 65536 bytes at both endpoints; larger `Size` is discarded with `LENGTH`. |
| A10 | PBS-ENV-01 Section 13 names the polynomial and zlib but not the initial value or final XOR. | zlib parameters (CRC-32/ISO-HDLC): init `0xFFFFFFFF`, reflected input and output, final XOR `0xFFFFFFFF`; check value `0xCBF43926`. |
| A11 | PBS-ENV-01 Section 7 defines ACK_REQ but no acknowledgement message. | The flag is carried and logged; no acknowledgement is generated. |
| A12 | PBS-MUX-01 Section 3 makes the payload a MUX container "when an implementation uses PBS-MUX"; the header has no field that says so. | Demo profile: every envelope with `Size > 0` carries a MUX container. |
| A13 | PBS-MUX-01 Section 4.1 defines `mux_version` without a value. | `0x01`; other values are rejected with `MUX_VERSION` (RX-17). |
| A14 | PBS-MUX-01 Section 9 says malformed frames are discarded and do not cause envelope rejection "unless integrity is compromised", but a frame whose length runs past the payload leaves no boundary for later frames. | A TLV structure error (frame past the end, fewer than `frame_count` frames) discards the whole envelope with `MUX_MALFORMED` (RX-18, RX-19). A frame with intact TLV but bad content is discarded alone; for a protected command a malformed Authority Context gives `AUTH_CTX_MALFORMED`. |
| A15 | Frame value content is application-defined (PBS-MUX-01 Sections 5.1 to 5.4). | ASCII text; commands are `<OPERATION>[ <arguments>]`. |
| A16 | PBS-SEC-B-01 Sections 3 and 4 refer to "registered" suites; PBS v1.5.0 contains no registry and no private-use range. | Two unregistered test suites, `0xFF01` and `0xFF02`, used only in this demo. |
| A17 | The Security frame `flags` field ("security services enabled") has no bit assignments. | Bit 0 = authentication. Only `0x01` is accepted. |
| A18 | `key_id` format is suite-defined and no suite exists. | ASCII, 1 to 255 bytes; lookup by (suite_id, key_id). |
| A19 | Nonce requirements are suite-defined. | `nonce_len` = 0 for both suites; replay protection uses `security_sequence`. |
| A20 | PBS-SEC-B-01 Section 5 lists what is protected but leaves the canonical representation to the suite. | `auth_input = envelope[0 : 44 + Size - auth_len]` with a single Security frame placed last and no padding (Section 6.2). Covers the header, the AUTH frame completely (PBS-AUTH-01 Section 2), the payload and the Security frame metadata. |
| A21 | PBS-SEC-B-01 and PBS-AUTH-01 do not state byte order. | Big-endian, as PBS-MUX-01 Section 4, PBS-ENV-01 Section 4 and PBS-SVC-01 Section 3. |
| A22 | PBS-SECB-REQ-003 lists replay mechanisms without choosing one. | `security_sequence` with a 64-entry window per (Source ID, key_id); 0 is invalid; state updated after authentication, before authorization (Section 6.4). |
| A23 | "Protected command" is not defined, and without a Service Intent frame (`security_profile`, PBS-SVC-01 Section 11) a receiver has no field telling it SEC-B is required. | Rover policy: every envelope with a Command frame requires a Security frame and an Authority Context frame. Service Intent frames are not used; PBS-SVC-01 is not claimed. |
| A24 | PBS-AUTH-01 does not define the encoding of identifiers in namespace 3 or of roles; authorization policy is mission-defined (Section 5). | ASCII identifiers and roles; the policy of Section 5 of this document. |
| A25 | PBS-SECB-REQ-007 defers the audit format to mission logging policy. | One JSON line per received envelope (Section 6.7). |
| A26 | PBS-SEC-B-01 gives no order between its checks and PBS-ENV-01 Section 14. | PBS-ENV-01 Section 14 first, then MUX, Security frame, authentication, replay, Authority Context, authorization (Section 6.6). An expired command is rejected before authentication (SB-11). |
| A27 | PBS-DTN-MAP-01 Section 4 describes gateways as translators, not originators; here each endpoint originates and encapsulates on its own node. | The gateway rules of PBS-DTN-MAP-01 apply to each endpoint's BP send and receive path. |
| A28 | Known issue in the PBS v1.5.0 changelog: PBS-DTN-MAP-01 Section 8 allows mapping a Source ID to an EID of another node, which conflicts with RFC 9171 Section 5.2. | Each Source ID maps to an EID of the node the endpoint runs on (Section 7.1). ION 4.2.0 `bp_send()` refuses a foreign node number. |
| A29 | PBS-DTN-MAP-01 Section 6.1 counts lifetime in ms from the creation time; ION 4.2.0 `bp_send()` takes whole seconds and assigns the creation time inside the call. | 1 s latency margin, floor to whole seconds, no send below 1 s (`GW_LIFETIME_UNDER_1S`) or below 1 ms (`GW_EXPIRED`), clamp at 2147483647 s (Section 7.4). Dropping is permitted by PBS-DTN-MAP-01 Section 10. |
| A30 | The no-expiry lifetime is gateway-specific (PBS-DTN-MAP-01 Section 6.1.1). | 2147483647 s, the largest `int` lifespan of ION 4.2.0 `bp_send()`, documented in `docs/MAPPING-PROFILE.md`. S1 and S5 check that ION delivers TTL 0 envelopes sent with it (PBS-C02-T016); this document reports no result for that check. |
| A31 | PBS-DTN-MAP-01 Section 6.3 maps to the RFC 4838 classes; ION also has an ordinal within its expedited class. | The Section 6.3 table onto ION classes 2, 1, 0; ordinal 0 for all. No other treatment is requested. |
| A32 | No clause requires a receiver to compare the bundle source EID with the Source ID. | Logged with a match flag; not a discard reason. |
| A33 | PBS-CONFORMANCE-01 has no test identifiers; its Section 8 statements describe a relay with a settable clock, and the demo has no PBS relay. | Cited by section and statement number. Only the receiver halves of statements 1 and 2 are exercised: RX-01, RX-02, RX-04 and RX-05 check the receiver's decision at the statements' clock values, as unit tests with an injected clock. The relay steps (store, forward at T+30 s with the 44 header bytes unchanged, discard at T+31 s, forward the TTL 0 envelope after 365 days) are not exercised; the endpoints' gateway rule of PBS-DTN-MAP-01 6.1 does not encapsulate a TTL 30 envelope at T+29 s or later (LT-05, LT-06: `GW_EXPIRED`). |
| A34 | PBS-CONFORMANCE-02 Section 3 uses method letters T, D, A, I without defining them. | Test, Demonstration, Analysis, Inspection. |
| A35 | PBS-C02-T013 and PBS-DTN-MAP-02 Section 10 test SEC-B with BPSec enabled. | BPSec is not configured; not exercised. |
| A36 | PBS-ENV-01 Section 12.2 requires periodic expiry checks of stored messages; the endpoints hold no envelopes after handing them to ION, where they are stored as bundles. | Endpoints apply the check immediately before `bp_send()` (as part of the Section 7.4 computation) and on receipt; storage expiry is governed by the bundle lifetime. |
| A37 | PBS-ENV-01 Section 15 requires synchronized clocks but the deployment chooses the clock source; future Timestamps are not addressed. | All nodes share one host clock; clock source and uncertainty are recorded in REPORT.md (PBS-CONFORMANCE-02 Section 4). A Timestamp later than `now_us` gives a negative age and is not expired. |
| A38 | PBS-MUX-01 Section 4.3 makes the bytes after the last of `frame_count` frames padding, to be ignored; a receiver MAY accept padding of any value. `frame_count` is covered by `auth_data`, but the receiver reads it before it knows whether the container has a Security frame. Lowering `frame_count` of a protected command in transit turns its Command and Security frames into padding: before this rule both endpoints accepted CMD-01 with `frame_count` 1 and 0 as unprotected data (no command ran; no security-failure record was written). | Rover policy (mission profile, PBS-SEC-B-01 Section 3): a container without a Security frame is rejected with `SEC_MISSING` when it has an Authority Context frame, padding after the last frame, or a Source ID bound to a provisioned key (`Earth-MCC`), as well as a Command frame (Section 6.6 R8, R8a). A header-only envelope (Size 0) is accepted at R6a. Senders write no padding, and Endpoint B signs every Earth-MCC envelope that has a payload. Vectors CMD-19 and CMD-20 (CMD-01 with `frame_count` 1 and 0), CMD-21 (unsigned Earth-MCC telemetry), CMD-22 (signed Earth-MCC telemetry, accepted), RX-20 (padding, rejected), RX-23 (Authority Context without a Security frame, rejected); S6 cases S6-23 and S6-24. |
