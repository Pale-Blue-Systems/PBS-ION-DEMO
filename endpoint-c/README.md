# Endpoint B (Earth, C)

Endpoint B is the Mission Control endpoint of the PBS ION demo. It runs on the
EARTH ION node and opens `ipn:23.1` with ION's `bp_open()`. It sends envelopes
to the rover with `bp_send()`, receives with `bp_receive()`, runs the PBS
receive path on every bundle payload, and writes the JSON Lines log of
`docs/LOG-SCHEMA.md`.

It is written in C11 from the PBS v1.5.0 specification text (PBS-ENV-01 1.5,
PBS-MUX-01 1.4, PBS-AUTH-01 1.4, PBS-SEC-B-01 1.5, PBS-DTN-MAP-01 1.5) and
`docs/SPEC-DIGEST.md`. `docs/test-vectors.json` is used as test data. The
PBS_LINK source, the vector generator `docs/gen_test_vectors.py` and Endpoint
A's protocol code (envelope, MUX and SEC-B modules) were not opened while
writing it. Of Endpoint A, only these were read: the shared log schema
`docs/LOG-SCHEMA.md`, its log writer `jsonlog.py` (for the `run_end` counter
names), the argument parsers of `rover.py` and `tool.py`, and the `crosscheck`
and decode functions of `tool.py`. They were read so that both endpoints
write the same log, `pbs_tool crosscheck` compares the same fields, and the
interoperability test can drive Endpoint A.

Cryptography is OpenSSL libcrypto: HMAC-SHA-256 with `HMAC()`, Ed25519 with
`EVP_DigestSign`/`EVP_DigestVerify`, SHA-256 with `EVP_Digest`. The keys are the
demo TEST KEYS of `docs/SPEC-DIGEST.md` Section 6.3, derived at start-up from
their public labels. They are not for operational use.

## Files

| File | Content |
|---|---|
| `src/pbs.h`, `src/pbs.c` | Library: envelope encode/decode, CRC-32, TTL check, bundle lifetime and class of service (digest 7.4, 7.5), MUX container, Authority Context, Security frame, HMAC-SHA-256 and Ed25519 suites, replay window, receiver policy, receive path R1 to R14 (digest 6.6) |
| `src/earth.h`, `src/earth.c` | Plan items to envelopes (including test-only fault injection), counter state file, plan file loader, log records of `docs/LOG-SCHEMA.md` |
| `src/json.h`, `src/json.c` | JSON reader (vectors, plans, logs) and JSON Lines writer |
| `src/pbs_earth.c` | The ION program |
| `src/pbs_tool.c` | Offline tool (no ION): build, receive, plan dry run, lifetime, crosscheck |
| `tests/test_pbs.c` | Unit tests (`make test`) |
| `tests/test_pbs_tool.sh` | Command-line tests of `pbs_tool` (`make test`) |
| `tests/ion_smoke.sh` | Live test on the three ION nodes with ION's own tools on the rover |
| `tests/ion_interop.sh` | Live test on the three ION nodes against Endpoint A |
| `examples/*.jsonl` | Plan files for S1, S5 and S6 |
| `Makefile` | Build, tests, sanitizer build |

## Build

Prerequisites: a C11 compiler (gcc 13.3.0 and clang 18.1.3 used), GNU make,
OpenSSL 3 libcrypto with headers (Ubuntu package `libssl-dev`; 3.0.13 used),
and `python3` for the shell tests. `pbs_earth` also needs ION 4.2.0 installed
by `ion/build_ion.sh` in `build/ion` (or `ION_PREFIX=...`).

```sh
make -C endpoint-c              # build/libpbs.a, build/pbs_tool, build/test_pbs, build/pbs_earth
make -C endpoint-c test         # unit tests and pbs_tool tests; no ION needed
make -C endpoint-c asan         # the same under AddressSanitizer and UndefinedBehaviorSanitizer
make -C endpoint-c CC=clang BUILD=build/clang
```

Every file compiles with `-std=c11 -Wall -Wextra -Werror -Wshadow
-Wstrict-prototypes -Wmissing-prototypes -Wformat=2 -Wvla` under gcc 13.3.0
and clang 18.1.3. ION's headers are included with `-isystem`. `make all` took
3.0 s on the host described under Results.

ION's installed `platform.h` defaults `SPACE_ORDER` to 2 (32-bit
addresses), but ION itself is compiled with `-DSPACE_ORDER=3` on 64-bit
hosts. A program that includes `bp.h` must use the same value. The Makefile
derives it from `getconf LONG_BIT`, and `pbs_earth.c` fails to compile if
`WORD_SIZE` differs from the pointer size.

## pbs_earth

```
pbs_earth [options] [run|recv|send]

  run   receive, and execute the plan if --plan is given (default)
  recv  receive only
  send  execute the plan only; opens the endpoint with bp_open_source(),
        so a separate receiving process may hold it

  --eid EID          own endpoint (default ipn:23.1)
  --dest EID         default destination (default ipn:99.1)
  --source-id ID     PBS Source ID (default Earth-MCC)
  --plan FILE        JSON-lines plan of envelopes to send
  --log FILE         JSON-lines log (docs/LOG-SCHEMA.md), appended (default stdout)
  --scenario LABEL   value of the "scenario" field of every record
  --state FILE       keep the Sequence and security_sequence counters in FILE
  --t0-us US         plan time origin, Unix epoch microseconds (default: start)
  --duration S       stop S seconds after t0
  --linger S         after the plan, keep receiving S seconds
  --idle S           after the plan, stop when nothing arrived for S seconds
  --expect N         stop after N received bundle payloads
  --map SRC=EID      Source ID to EID entry for expected_eid
                     (default Rover-Alpha=ipn:99.1 and Earth-MCC=ipn:23.1)
  --node-dir DIR     ION node working directory (sets ION_NODE_WDNAME)
```

Without `--duration`, `--linger`, `--idle` or `--expect`, `run` and `recv`
keep receiving until SIGINT or SIGTERM. On either signal `pbs_earth` stops
the receiver with `bp_interrupt()`, writes `run_end`, closes the endpoint
and exits with status 0. Exit status 1 means ION could not be attached or
the endpoint could not be opened; status 2 means a usage, plan or state-file
error.

Running it on the demo's EARTH node:

```sh
source ion/env.sh                     # sets ION_NODE_LIST_DIR and ION_RUN_DIR
ION_NODE_WDNAME="$ION_RUN_DIR/earth" endpoint-c/build/pbs_earth run \
  --plan endpoint-c/examples/s6_auth.jsonl --scenario S6 --duration 40 \
  --log results/S6/endpoint-b.jsonl &
```

To signal the process later, start it with `ION_NODE_WDNAME=... pbs_earth`,
as above, not through the `ion_node` shell function. With `ion_node`, `$!`
is the PID of a bash subshell, and a SIGTERM to it leaves `pbs_earth`
running.

Each `pbs_earth` process starts the Sequence counter and each key's
`security_sequence` counter at 1 (digest A5, 6.4). The rover keeps replay
state for the whole run, so a scenario that starts `pbs_earth` more than once
in one run passes the same `--state FILE` to every invocation. Without it,
the rover rejects the second invocation's commands with `SEC_REPLAY`.

### Send path

For each plan entry, at `t0 + at_s`:

1. Build the envelope. The Timestamp is the host clock at build time. For a
   command, the payload is a MUX container (version 1) with an Authority
   Context frame (namespace 3, `PBS-DEMO-MCC`, role, epoch 1), any
   `extra_frames`, the Command frame and a Security frame (suite `0xFF02`
   unless `suite` is given). `auth_data` covers `envelope[0 : 44 + Size -
   auth_len]` (digest 6.2, 6.5).
2. Decide the bundle lifetime by digest 7.4. The time margin is 1 s and the
   lifetime is a whole number of seconds; TTL 0 gives 2147483647 s. If less
   than 1 ms remains, the reason is `GW_EXPIRED`; if less than 1 s remains,
   `GW_LIFETIME_UNDER_1S`. In both cases the envelope is not handed to ION
   and the `send` record says `NOT_SENT`.
3. Set the ION class of service from the priority (digest 7.5): 0 and 1 give
   2 (expedited), 2 gives 1, 3 and 4 give 0. The ordinal is 0.
4. Copy the envelope into an SDR ZCO with `ionCreateZco(..., NULL)`. This is
   non-blocking: if ION has no ZCO space, the reason is `ZCO_FAILED`. Then
   call `bp_send(sap, dest, NULL, lifespan, class, NoCustodyRequested, 0, 0,
   NULL, zco, &bundle)`: no report-to EID, no status reports, no custody, and
   no BP application acknowledgement flag. PBS ACK_REQ is carried in the
   envelope only (A11). ION assigns the creation timestamp.
5. Write the `send` record. `t_us` is read immediately before `bp_send()`.

### Plan file

One JSON object per line; blank lines and lines starting with `#` are
ignored. `pbs_tool plan` prints what `pbs_earth` would send for a plan.

| Field | Meaning | Default |
|---|---|---|
| `at_s` | seconds after t0 | 0 |
| `count`, `interval_s` | repeat the item `count` times, `interval_s` apart | 1, 0 |
| `kind` | `telemetry`, `status`, `event` (one frame of type 1, 3, 4), `command`, `heartbeat` (Size 0), `frames` (explicit `frames` list), `raw` (bytes from `hex`), `replay` (bytes of an earlier `label`) | `telemetry` |
| `text`, `hex`, `pad_to` | frame value: ASCII text or hex; `pad_to` pads the value to that many bytes with `0123456789...` (Size = `pad_to` + 5) | `""` |
| `frames` | `[{"type": 1 or "0x01", "text" or "hex": ...}]` | |
| `priority`, `ttl`, `flags` | header fields | command 1 / 30 / 0; other kinds 2 / 60 / 0 |
| `sequence` | fixed Sequence (the counter is not advanced) | counter |
| `timestamp_us`, `timestamp_offset_us` | absolute Timestamp, or offset from the clock | clock |
| `source_id`, `dest` | per-item Source ID and destination EID | `--source-id`, `--dest` |
| `command`, `role`, `authority_id`, `authority_id_type`, `policy_epoch`, `extra_frames` | command content | `FLIGHT-OPS`, `PBS-DEMO-MCC`, 3, 1 |
| `suite` | `ed25519`, `hmac` or a number such as `"0xFF02"` | `ed25519` |
| `key_id`, `signing_key` | key_id written in the frame; key used to sign | `TEST-ED25519-01` or `TEST-HMAC-01`; `signing_key` = `key_id` |
| `security_sequence` | fixed value (the counter is not advanced) | counter per key_id |
| `sign` | add a Security frame to a non-command item | false |
| `label`, `note` | `label` is logged and names the bytes for `replay`; `note` is logged as `x_note` | |
| `class_of_service` | override of the class passed to `bp_send()` | from priority |

Test-only fault injection. Each of these is recorded in the `send` record
(`x_tamper`, `x_replay_of`, `x_signing_key`, `lifespan_rule: "OVERRIDE"`):

| Field | Effect |
|---|---|
| `tamper` | applied after the lifetime decision, as a change in transit: `command` (last byte of the Command value), `authority` (last byte of the Authority Context value), `priority` and `ttl` and `timestamp` (CRC32 recomputed), `security_sequence` (+1), `auth_data` (last byte), `crc` (CRC32 field) |
| `kind: "replay"`, `replay_of` | sends the exact bytes of the earlier item with that `label` |
| `signing_key` other than `key_id` | wrong key (`TEST-ED25519-ROGUE`, `TEST-HMAC-ROGUE`) |
| `key_id` not provisioned at the rover | unknown key |
| `unsigned`, `omit_auth_ctx` | command without Security frame, or without Authority Context |
| `lifetime_s` | bundle lifetime in seconds instead of the digest 7.4 value. This does not conform to PBS-DTN-MAP-01 6.1; it lets an expired envelope reach a receiver, for the PBS-ENV-01 12.2 check |

`examples/s6_auth.jsonl` sends a valid Ed25519 command, a valid HMAC
command, and then 13 rejection cases. Each line's `note` gives the rover
verdict that digest Section 6.6 requires.

### Receive path

The receiver thread blocks in `bp_receive()`; `t_us` is read when it returns.
It copies up to 65,581 bytes (44 + 65,536 + 1) of the ZCO and computes the
SHA-256 over the whole payload. It then runs digest Section 6.6 steps R1 to
R14 with the demo policy of digest Section 5. The policy holds the
`TEST-ED25519-01` public key and `TEST-HMAC-01`, both bound to `Earth-MCC`
and `PBS-DEMO-MCC`, and a 64-entry replay window per (Source ID, key_id).

For each payload it writes one `recv` record, and one `exec` record per
Command frame of an accepted command envelope. A payload above 65,581 bytes
passes R1 to R5 on its header and is then rejected with `LENGTH`.
`x_duplicates_before` counts earlier payloads with the same envelope identity
(Source ID, Sequence, Timestamp). An administrative record delivered to the
endpoint goes through the same path, with `bundle_admin_record: true`.

### Log

The log follows `docs/LOG-SCHEMA.md` (`pbs-ion-demo-log/1`). Every record
has `"endpoint": "B"`; the records are `run_start`, `send`, `recv`, `exec`
and `run_end`. The `run_end` counts use the keys `send:SENT`,
`send:NOT_SENT`, `send:NOT_SENT:<reason>`, `recv:<result>:<reason>`, `exec`,
and `x_error` (errors are printed to standard error). Endpoint B adds these
fields:

| Record | Field | Content |
|---|---|---|
| `send` | `x_kind`, `x_tamper`, `x_replay_of`, `x_signing_key`, `x_role`, `x_note` | plan item |
| `send` | `x_lifetime_outcome`, `x_lifetime_bound_ms` | digest 7.4 outcome and the PBS-DTN-MAP-01 6.1 bound |
| `recv` | `x_crc32_computed`, `x_authenticated` | CRC-32 computed by the receiver; R10 result |
| `recv` | `x_duplicates_before` | see Receive path |
| `recv` | `x_frames` | per frame: type, length, ASCII text (up to 512 bytes) or hex (up to 64 bytes) |
| `run_start` | `x_mode`, `x_plan`, `x_plan_items`, `x_state`, `x_t0_us`, `x_duration_s`, `x_linger_s`, `x_idle_s`, `x_expect`, `x_source_id_eid_map`, `x_ion_node_wdname`, `x_ion_node_list_dir`, `x_keys_note` | configuration |
| `run_end` | `x_plan_items`, `x_plan_items_done` | |

Strings taken from received bytes are written as valid UTF-8. Bytes that are
not valid UTF-8 become U+FFFD.

## pbs_tool

`pbs_tool` uses the same library without ION.

| Command | Use |
|---|---|
| `pbs_tool build --vectors docs/test-vectors.json` | builds every vector that has `inputs` and prints `{"id", "envelope_hex", "matches_vector"}` |
| `pbs_tool build < inputs.jsonl` | same, for JSON lines in the `inputs` format of `docs/test-vectors.json` (optionally `{"id", "inputs", "security"}`) |
| `pbs_tool receive [--now-us N] [--fresh] < lines` | receive path on hex lines or `{"id", "hex", "now_us"}` lines, one replay state unless `--fresh`; prints `recv` records with `bundle_*` fields `null` |
| `pbs_tool plan [--now-us N] [--source-id ID] [--dest EID] < plan.jsonl` | dry run: `x_plan` records (send fields, `result` `TO_SEND` or `NOT_SENT`, `x_envelope_hex` with all bytes) |
| `pbs_tool lifetime TTL TIMESTAMP_US CLOCK_US` | digest 7.4 computation |
| `pbs_tool crosscheck PEER_LOG [--own-log OWN_LOG] [--out FILE]` | decodes the `envelope_hex` of every `send` record of the other endpoint's log and compares 13 fields (header fields, length, SHA-256, `frame_types`); runs Endpoint B's receive path on each at the peer's `t_us`; with `--own-log`, finds the peer's `recv` record for every envelope this endpoint sent (matched on Source ID, Sequence, Timestamp) and compares SHA-256 and header bytes. Exit status 0 when nothing differs |

## Choices made in Endpoint B

These choices are within the room `docs/SPEC-DIGEST.md` leaves:

- Replay state holds 256 (Source ID, key_id) pairs. When all are in use, a
  new pair is rejected with `SEC_REPLAY`.
- A key without a Source ID and authority binding cannot authorize a
  command (`AUTHZ_DENIED`). A Command frame whose operation is empty (an
  empty value, or one starting with a space) is not permitted for any role.
- The lookup in R9c is by (suite_id, key_id). `TEST-ED25519-01` presented
  with suite `0xFF01` gives `SEC_KEY_UNKNOWN`.
- Fault injection is applied after the lifetime and class-of-service
  decision, as a modification in transit would be.
- A Source ID whose UTF-8 encoding exceeds 16 bytes is refused when the
  envelope is built (A4).
- For `raw` items whose priority byte is above 4, the class of service is 1
  unless `class_of_service` is given.

## Results

All runs on 2026-10-07 on one host: 4 vCPU Intel Xeon 2.80 GHz, Ubuntu
24.04.5, gcc 13.3.0, clang 18.1.3, OpenSSL 3.0.13, ION
`ion-open-source-4.2.0` (commit `568df887cb9f18aa8ec013b1a566d83215e499ae`,
built by `ion/build_ion.sh`), `docs/test-vectors.json` SHA-256
`789ac1b4a02f38e8adf3d9bc1eeabe4d0bb7abc74a95b3dd8439633bcb3885ff`. All ION
nodes and endpoints read the same host clock (A37).

### Unit tests (`make test`)

`build/test_pbs docs/test-vectors.json`: 757 checks passed, 0 failed. The
same 757 checks passed in the clang build and in the `make asan` build;
AddressSanitizer, LeakSanitizer and UndefinedBehaviorSanitizer reported
nothing.

| Group | Checks | Content |
|---|---|---|
| constants and CRC-32 | 9 | `constants`, `crc32_check` (`0xCBF43926`) |
| envelopes | 166 | ENV-01 to ENV-15: built from `inputs` byte-identical to `envelope_hex`, header and CRC32, Source ID encoding, parse back to the inputs, MUX frames |
| receiver cases | 23 | RX-01 to RX-22: result and reason; ACK_REQ of RX-13 |
| lifetime cases | 39 | LT-01 to LT-10: outcome, bound, `c_us`, lifespan |
| class of service | 16 | `cos_map`, and priority 5 has no class |
| test keys | 17 | the four keys derived from their labels equal the vector values; provisioning and binding |
| rover policy | 7 | `sec_b.rover_policy` equals the built-in policy |
| SEC-B messages | 216 | CMD-01 to CMD-18 and CMD-W00 to CMD-W70: the 19 messages with `inputs` built byte-identical; `auth_input` and `auth_data` positions; signatures and tags verify with the signing key; MUX frames |
| SEC-B cases | 30 | SB-01 to SB-21, all 30 deliveries in order: result and reason |
| negative and edge cases | 90 | Source ID length; Size 65,537; MUX structure; Security frame not last, duplicated, short, wrong version, flags, nonce, auth_len, empty key_id, unknown suite; Authority Context missing, duplicated, wrong version or length; authorization (namespace, authority, role, operation, two commands, Source ID binding); replay window edges (H-64, H-63, shift by 64, 2^64-1, full table); replay state not consumed by a failed signature and consumed by an unauthorized command; TTL and lifetime edges |
| bit flips, truncation, fuzz | 9 | every single-bit flip and every truncation of CMD-01 (1,504 flips) and CMD-02 (1,264 flips); 24,400 seeded random mutations of all vector envelopes |
| plan items | 95 | every plan kind and fault-injection mode, built and then received under the rover policy |
| log records and JSON | 40 | `send`, `recv`, `exec` and `x_plan` records against the `docs/LOG-SCHEMA.md` field lists; hostile bytes; state file; JSON reader |

Of the 1,504 single-bit flips of CMD-01, 1,503 are rejected: 344 `CRC`, 8
`MAGIC`, 8 `MUX_VERSION`, 47 `MUX_MALFORMED`, 10 `SEC_MISSING`, 56
`SEC_MALFORMED`, 16 `SEC_SUITE_UNKNOWN`, 120 `SEC_KEY_UNKNOWN` and 894
`SEC_AUTH_FAIL`. The remaining flip is accepted. It changes `frame_count` from 3 to 1
(payload byte 1, bit 1). The receiver then parses only the Authority Context
frame and treats the Command and Security frames as padding (PBS-MUX-01
4.3). With no Command and no Security frame, R8 accepts the envelope as
unprotected data, and no command runs. CMD-02 gives the same single
accepted flip. The test asserts that no flip yields an accepted envelope
that carries a command. Every truncation of both messages is rejected.

`tests/test_pbs_tool.sh`:

- 34/34 vectors built byte-identical by `pbs_tool build`.
- 52/52 receiver and SEC-B deliveries as expected through `pbs_tool receive`.
- 10/10 lifetime cases.
- `crosscheck` reports 0 mismatches for a clean peer log, reports an altered
  `sequence` and `frame_types`, and reports 1 digest mismatch among 10
  delivered envelopes.
- The three example plans build.

### Live test with ION tools on the rover (`tests/ion_smoke.sh`)

Conditions:
- Nodes: the three ION nodes of `ion/` with contact plan
  `ion/contacts/always_on.ionrc` (125,000 B/s each way; ranges 1 s and 2 s).
- Links: `ion/udp_forward.py` with one-way delays of 0.25 s
  (rover-gateway) and 1.3 s (gateway-earth), no loss.
- Rover side: ION's `bprecvfile` and `bpsendfile` stand in for the rover
  endpoint.

Run at 02:00 UTC, 55.0 s wall clock, PASS:

- A, Earth to rover: `pbs_earth send` with `examples/s6_auth.jsonl` handed
  16 envelopes to ION, and `bprecvfile ipn:99.1` received 16, all
  byte-identical. `pbs_tool receive`, run in arrival order at the arrival
  times, gave the verdict in each plan note: 2 `ACCEPT OK`, then 6
  `SEC_AUTH_FAIL` (four tamper modes and two wrong keys), 1 `SEC_REPLAY`, 1
  `SEC_KEY_UNKNOWN`, 1 `TTL_EXPIRED`, 2 `AUTHZ_DENIED`, 1 `SEC_MISSING`, 1
  `AUTH_CTX_MISSING`, then `ACCEPT OK`.
  From envelope Timestamp to file write at the rover took 1.566 to 1.578 s
  (15 envelopes; the replay, sent 6 s after its original, 7.574 s).
- B, rover to Earth: `bpsendfile ipn:99.1` sent 10 envelopes built by
  `pbs_tool plan --source-id Rover-Alpha`. `pbs_earth run` received all 10,
  with `bundle_source_eid` `ipn:99.1` and `eid_match` true. It accepted 7
  (priorities 0 to 4, a 60,005-byte BULK envelope, a header-only envelope,
  and a signed telemetry envelope). It rejected 3: `CRC` (CRC32 field
  altered), `TTL_EXPIRED` (TTL 1, lifetime override 60 s) and `SEC_MISSING`
  (unsigned Command frame). From bundle creation to `bp_receive()` return
  took 1.570 to 1.580 s, and 1.626 s for the 60,005-byte envelope.
- C: three envelopes sent by `pbs_earth run` to `ipn:99.1` waited in the
  rover endpoint queue (rule `q`). `bprecvfile` collected all three
  byte-identical, including a TTL 0 envelope sent with the no-expiry
  lifetime of 2147483647 s.
- D: `pbs_earth recv` stopped on SIGTERM within the 10 s limit, with exit
  status 0 and a `run_end` record.
- E: all 35 records written by `pbs_earth` passed the `docs/LOG-SCHEMA.md`
  field check.

For the TTL 0 bundle (lifetime 2147483647 s), `BpDelivery.timeToLive` was
4294966296 ms (`bundle_ttl_field_ms`), the 32-bit wrap described in digest
7.4.

### Live test against Endpoint A (`tests/ion_interop.sh`)

Same nodes, links and contact plan. Endpoint A ran `endpoint-python/bin/rover`
with `endpoint-python/plans/example.json` and `--reply-ping`; Endpoint B ran
`pbs_earth run` with `examples/s6_auth.jsonl`; both for 40 s. Run at 02:03
UTC, 71.7 s wall clock, PASS:

- Endpoint A's verdict on each of the 16 Endpoint B envelopes equals the plan
  note. A accepted 3 commands (`DRIVE 10`, `SET_HEATER ON`, `PING
  AFTER-REJECTS`) and wrote 3 `exec` records. It rejected 6 with
  `SEC_AUTH_FAIL`, 1 `SEC_REPLAY`, 1 `SEC_KEY_UNKNOWN`, 1 `TTL_EXPIRED`, 2
  `AUTHZ_DENIED`, 1 `SEC_MISSING` and 1 `AUTH_CTX_MISSING`.
- Endpoint A sent 19 envelopes (its example plan and one ping reply).
  Endpoint B received and accepted all 19, with `eid_match` true. Latency
  from Timestamp to `bp_receive()` return was 1.569 / 1.577 / 1.589 s (min /
  median / max).
- Endpoint A's `tool crosscheck` decoded Endpoint B's 16 envelopes with 0
  mismatches, and found all 19 of its own envelopes byte-identical in
  Endpoint B's `recv` records. `pbs_tool crosscheck` decoded Endpoint A's 19
  envelopes with 0 mismatches; Endpoint B's receive path accepted all 19.
  It found all 16 of Endpoint B's envelopes byte-identical in Endpoint A's
  `recv` records.
- Endpoint A's log (40 records) and Endpoint B's log (37 records) passed the
  `docs/LOG-SCHEMA.md` field check.

Endpoint B's first command of that run took 2.248 s from Timestamp to
Endpoint A, against 1.571 to 1.588 s for the other 14 non-replayed
commands. The forwarder log shows the cause is on the EARTH node: the first
LTP datagram of that bundle left 0.671 s after `bp_send()` returned, while
the following commands' datagrams left 7 to 8 ms after it. The reason for
the first-bundle delay was not investigated. In the smoke run above, the
first command took 1.574 s.

Offline checks in the same session:
- Endpoint A's `tool vectors` and `pbs_tool build --vectors` produced
  byte-identical envelopes for all 34 vector inputs.
- Endpoint A's `tool parse --shared-state` on the 16 envelopes of smoke run
  A, in arrival order, gave the 16 verdicts of the plan notes.

## Conformance test identifiers exercised

The runs above exercise these test identifiers. Endpoint B makes no
conformance claim and has no certification.

| Identifier | Exercised by |
|---|---|
| PBS-CONFORMANCE-01 4.1, 4.3, 5.1 | ENV-01 to ENV-15 built and parsed; RX-07 to RX-09 |
| PBS-CONFORMANCE-01 4.2, 9 | RX-01 to RX-22 receive order and reason codes; random mutation |
| PBS-CONFORMANCE-01 7 | Sequence 65535 then 0 (plan test P06) |
| PBS-CONFORMANCE-01 8, statements 1 and 2 | RX-01, RX-02, RX-04 (TTL 30) and RX-05 (TTL 0) as unit tests with a set clock |
| PBS-CONFORMANCE-02 T002 | byte identity after BPv7 carriage: smoke test A, B, C; interop crosschecks |
| PBS-CONFORMANCE-02 T004 | RX-03, RX-04, RX-06, SB-11; expired envelopes rejected after ION delivery (smoke A, B; interop) |
| PBS-CONFORMANCE-02 T006 | SB-03 to SB-06; tampered commands rejected by both endpoints (smoke A, interop) |
| PBS-CONFORMANCE-02 T007 | SB-10, SB-21; replay rejected (smoke A, interop) |
| PBS-CONFORMANCE-02 T008 | SB-12, SB-13, SB-14; role and epoch (interop) |
| PBS-CONFORMANCE-02 T010 | `cos_map` and the class passed to `bp_send()` |
| PBS-CONFORMANCE-02 T014 | RX-21, SB-18 (unknown frame skipped, inside a protected command) |
| PBS-CONFORMANCE-02 T015 | `recv` and `exec` records with reason codes |
| PBS-CONFORMANCE-02 T016 | TTL 0 envelope sent with the 2147483647 s lifetime and delivered (smoke C) |

## Limits

- One demo instance per host: the ION scripts use fixed IPC keys and UDP
  ports. `tests/ion_smoke.sh` and `tests/ion_interop.sh` refuse to start
  when ION is already running.
- `pbs_earth` itself was not run under the sanitizers. The sanitizer runs
  cover the library and `pbs_tool`.
- BPSec and custody transfer are not used.
- The keys are TEST KEYS derived from public labels.
