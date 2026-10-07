# Endpoint log schema

Both PBS endpoints write the same log format: Endpoint A (rover, Python,
`endpoint-python/`) and Endpoint B (Earth, C, `endpoint-c/`). The report
tools read only this format. This document is the agreement between the two
implementations; a field is added or renamed here first.

Schema identifier: `pbs-ion-demo-log/1`.

## 1. File format

- JSON Lines: one JSON object per line, UTF-8, `\n` line ends, no
  pretty-printing. Each line is complete when written (the writer flushes
  after every line).
- One file per endpoint process. The file name is chosen by whoever starts
  the process (`--log PATH`). Suggested layout:
  `results/<scenario>/endpoint-a.jsonl` and `results/<scenario>/endpoint-b.jsonl`.
- Readers ignore keys they do not know. Writers may add keys whose names
  start with `x_` without changing this document.
- A field that does not apply, or whose value is unknown at that step, is
  written as `null`. A field listed for an event type is always present.
- Integers are JSON numbers (Unix epoch microseconds stay below 2^53).
  Byte strings are lowercase hex without separators. Booleans are `true` /
  `false`.

## 2. Time

All `*_us` times are Unix epoch microseconds read from the host clock
(`CLOCK_REALTIME`; Python `time.time_ns() // 1000`; C
`clock_gettime(CLOCK_REALTIME)`). The three ION nodes and both endpoints run
on one host and read the same clock (SPEC-DIGEST.md A37).

## 3. Fields present in every record

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `pbs-ion-demo-log/1` |
| `event` | string | `run_start`, `send`, `recv`, `exec` or `run_end` (Sections 4 to 8) |
| `t_us` | int | time of the event (defined per event type below) |
| `endpoint` | string | `A` (rover) or `B` (Earth); `H` for Endpoint A's test harness (`endpoint-python/pbs_endpoint_a/harness.py`, an Earth-side stand-in and fault injector, not Endpoint B) |
| `node_eid` | string | EID the endpoint opened: `ipn:99.1` (A) or `ipn:23.1` (B) |
| `scenario` | string or null | scenario label given on the command line, e.g. `S1` |

## 4. Envelope fields

Used by `send` and `recv`. In a `recv` record they are `null` when the
bundle payload is shorter than 44 bytes; otherwise they are decoded from the
44 header bytes as received, even when the envelope is rejected later.

| Field | Type | Meaning |
|---|---|---|
| `source_id` | string | Source ID for display: bytes up to the first `0x00`, decoded as UTF-8 with replacement characters (SPEC-DIGEST.md A4) |
| `source_id_hex` | string | the 16 raw Source ID bytes (32 hex digits) |
| `sequence` | int | Sequence (0 to 65535) |
| `priority` | int | Priority byte (0 to 4; a rejected envelope may show 5 to 255) |
| `flags` | int | Flags byte |
| `timestamp_us` | int | Timestamp (Unix epoch microseconds) |
| `ttl_s` | int | TTL (seconds, 0 = never expires) |
| `size` | int | Size (payload bytes declared in the header) |
| `crc32` | string | CRC32 field as stored, `0x` and 8 upper-case hex digits |
| `header_hex` | string | the 44 header bytes (88 hex digits) |
| `envelope_len` | int | number of bytes in the bundle payload (send: bytes handed to ION) |
| `envelope_sha256` | string | SHA-256 of all `envelope_len` bytes |
| `envelope_hex` | string or null | all `envelope_len` bytes when `envelope_len` <= 4096, else `null` |
| `frame_types` | array of int or null | MUX frame types in container order; `[]` for Size 0; `null` if the container was not parsed |

Envelope identity, used to join `send` and `recv` records, is
(`source_id_hex`, `sequence`, `timestamp_us`) (SPEC-DIGEST.md A5).

## 5. `send` record

One per envelope the endpoint built and handed, or decided not to hand, to
ION. `t_us` is read immediately before `bp_send()` is called (for a
`NOT_SENT` record: when the decision was made).

Envelope fields (Section 4), plus:

| Field | Type | Meaning |
|---|---|---|
| `result` | string | `SENT` (`bp_send()` returned 1) or `NOT_SENT` |
| `reason` | string | `OK`, or why it was not sent (Section 9) |
| `dest_eid` | string | destination EID passed to `bp_send()` |
| `lifespan_s` | int or null | lifetime in seconds passed to `bp_send()`; `null` when `bp_send()` was not called |
| `lifespan_rule` | string | `DTN-MAP-01-6.1` (computed per SPEC-DIGEST.md 7.4) or `OVERRIDE` (fault injection: a lifetime chosen by the operator, not conformant to PBS-DTN-MAP-01 6.1) |
| `class_of_service` | int | ION class of service passed to `bp_send()` (SPEC-DIGEST.md 7.5) |
| `ordinal` | int | ION ordinal (0) |
| `bp_send_return_us` | int or null | time `bp_send()` returned; `null` when not called |
| `label` | string or null | free-text tag from the traffic plan (e.g. `S4-BULK`) |
| `commands` | array of string or null | Command frame values (ASCII) when the envelope carries Command frames |
| `suite_id` | string or null | Security frame suite, e.g. `0xFF02` |
| `key_id` | string or null | Security frame key_id (ASCII) |
| `security_sequence` | int or null | Security frame security_sequence |

## 6. `recv` record

One per bundle payload delivered by `bp_receive()` to the endpoint's EID.
`t_us` is read immediately after `bp_receive()` returns with a payload.

Envelope fields (Section 4), plus:

| Field | Type | Meaning |
|---|---|---|
| `result` | string | `ACCEPT` or `REJECT` |
| `reason` | string | `OK`, or the reason code of the first failed check (Section 9) |
| `bundle_source_eid` | string | `BpDelivery.bundleSourceEid` |
| `bundle_creation_ms` | int | `BpDelivery.bundleCreationTime.msec` (ms since 2000-01-01T00:00:00Z) |
| `bundle_creation_count` | int | `BpDelivery.bundleCreationTime.count` |
| `bundle_creation_us` | int | `(bundle_creation_ms + 946684800000) * 1000` |
| `bundle_ttl_field_ms` | int | `BpDelivery.timeToLive` as delivered (unsigned 32-bit; does not hold lifetimes above 4294967295 ms, SPEC-DIGEST.md 7.4) |
| `bundle_admin_record` | bool | `BpDelivery.adminRecord` non-zero |
| `expected_eid` | string or null | EID the Source ID maps to (SPEC-DIGEST.md 7.1); `null` for an unknown Source ID |
| `eid_match` | bool or null | `bundle_source_eid == expected_eid`; `null` when `expected_eid` is `null` (A32: logged, not a discard reason) |
| `latency_us` | int or null | `t_us - timestamp_us` |
| `commands` | array of string or null | Command frame values of an accepted command envelope |
| `suite_id` | string or null | from the Security frame, once its structure has been checked (step R9) |
| `key_id` | string or null | as above |
| `security_sequence` | int or null | as above |
| `authority_id` | string or null | from the Authority Context frame, once its structure has been checked (step R12a) |
| `role` | string or null | as above |
| `policy_epoch` | int or null | as above |

## 7. `exec` record

One per Command frame of an accepted command envelope, written after the
`recv` record, in frame order. The demo rover executes nothing; it records
the command.

| Field | Type | Meaning |
|---|---|---|
| `source_id_hex`, `sequence`, `timestamp_us` | | identity of the envelope that carried the command |
| `command` | string | Command frame value |
| `operation` | string | `command` up to the first space |
| `key_id` | string | key that authenticated the command |
| `security_sequence` | int | |
| `role` | string | authorized role |
| `action` | string | `LOGGED` (no actuator is driven in the demo) |

## 8. `run_start` and `run_end` records

`run_start` is the first line of the file, `run_end` the last (absent if the
process was killed).

`run_start`:

| Field | Type | Meaning |
|---|---|---|
| `impl` | string | implementation name and version, e.g. `endpoint-python 0.1.0` |
| `pbs_link_version` | string or null | PBS_LINK version used (Endpoint A); `null` for Endpoint B |
| `ion_version` | string | `getIonVersionNbr()` |
| `source_id` | string | Source ID this endpoint sends with |
| `dest_eid` | string | default destination EID |
| `pid` | int | process ID |
| `argv` | array of string | command line |
| `keys` | array of string | key_ids loaded (signing keys at B, verification keys at A) |

`run_end`:

| Field | Type | Meaning |
|---|---|---|
| `counts` | object | event and reason counters, e.g. `{"send:SENT": 50, "recv:ACCEPT:OK": 12, "recv:REJECT:SEC_REPLAY": 1, "exec": 9}` |

## 9. Reason codes

Receive side (SPEC-DIGEST.md 2.5 and 6.6, in check order): `OK`, `LENGTH`,
`MAGIC`, `CRC`, `PRIORITY`, `TTL_EXPIRED`, `MUX_VERSION`, `MUX_MALFORMED`,
`SEC_MISSING`, `SEC_MALFORMED`, `SEC_SUITE_UNKNOWN`, `SEC_KEY_UNKNOWN`,
`SEC_AUTH_FAIL`, `SEC_REPLAY`, `AUTH_CTX_MISSING`, `AUTH_CTX_MALFORMED`,
`AUTHZ_DENIED`.

Send side:

| Code | Meaning |
|---|---|
| `OK` | handed to ION, `bp_send()` returned 1 |
| `GW_EXPIRED` | lifetime bound below 1 ms (SPEC-DIGEST.md 7.4) |
| `GW_LIFETIME_UNDER_1S` | lifetime bound below 1 s, which `bp_send()` cannot express |
| `ZCO_FAILED` | ION could not store the payload (`sdr_malloc` or `ionCreateZco` failed) |
| `BP_SEND_FAILED` | `bp_send()` returned 0 or -1 |

## 10. Example lines

```
{"schema":"pbs-ion-demo-log/1","event":"send","t_us":1767225600100250,"endpoint":"A","node_eid":"ipn:99.1","scenario":"S1","source_id":"Rover-Alpha","source_id_hex":"526f7665722d416c7068610000000000","sequence":1,"priority":0,"flags":0,"timestamp_us":1767225600100000,"ttl_s":0,"size":28,"crc32":"0x1234ABCD","header_hex":"...","envelope_len":72,"envelope_sha256":"...","envelope_hex":"...","frame_types":[1],"result":"SENT","reason":"OK","dest_eid":"ipn:23.1","lifespan_s":2147483647,"lifespan_rule":"DTN-MAP-01-6.1","class_of_service":2,"ordinal":0,"bp_send_return_us":1767225600100900,"label":"S1-CRITICAL","commands":null,"suite_id":null,"key_id":null,"security_sequence":null}
{"schema":"pbs-ion-demo-log/1","event":"recv","t_us":1767225603700000,"endpoint":"A","node_eid":"ipn:99.1","scenario":"S6","source_id":"Earth-MCC","source_id_hex":"45617274682d4d434300000000000000","sequence":1,"priority":1,"flags":0,"timestamp_us":1767225602000000,"ttl_s":30,"size":144,"crc32":"0x010E8C49","header_hex":"...","envelope_len":188,"envelope_sha256":"...","envelope_hex":"...","frame_types":[8,2,10],"result":"ACCEPT","reason":"OK","bundle_source_eid":"ipn:23.1","bundle_creation_ms":820540802001,"bundle_creation_count":0,"bundle_creation_us":1767225602001000,"bundle_ttl_field_ms":28000,"bundle_admin_record":false,"expected_eid":"ipn:23.1","eid_match":true,"latency_us":1700000,"commands":["DRIVE 10"],"suite_id":"0xFF02","key_id":"TEST-ED25519-01","security_sequence":1,"authority_id":"PBS-DEMO-MCC","role":"FLIGHT-OPS","policy_epoch":1}
```

(`...` stands for hex elided in this document; real records carry the full
value.)
