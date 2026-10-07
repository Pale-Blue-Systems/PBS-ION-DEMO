# Endpoint A: rover endpoint (Python)

Endpoint A is the PBS endpoint on the rover. It runs on the ROVER ION node,
uses that node's own EID `ipn:99.1`, and does four things:

- sends telemetry envelopes at the five PBS priorities to Mission Control
  (`ipn:23.1`), one envelope per bundle;
- receives command envelopes and verifies them with PBS-SEC-B-01 and
  PBS-AUTH-01;
- logs accepted commands (no actuator is driven) and rejects every other
  envelope with a reason code;
- writes each send, receive and execution as one JSON line in the format
  of [`docs/LOG-SCHEMA.md`](../docs/LOG-SCHEMA.md).

Rules and reason codes come from
[`docs/SPEC-DIGEST.md`](../docs/SPEC-DIGEST.md). Section numbers below refer to that file; A1 to
A37 are its numbered resolutions.

## How the work is split

| Function | Done by |
|---|---|
| Build the 44-byte header, CRC-32, Sequence counter, Timestamp | PBS_LINK 0.1.4 `PBSLink.send()` (Timestamp passed in exact microseconds through its `clock_source` hook) |
| Parse: length >= 44, magic, CRC-32, priority, payload extraction | PBS_LINK 0.1.4 `PBSLink.parse()` |
| Checks PBS_LINK 0.1.4 does not make: TTL before payload length (PBS-ENV-01 Section 14), integer-microsecond expiry (A6), trailing bytes (A8), Size above 65536 (A9), Source ID longer than 16 bytes refused before `send()` (A4) | `pbs_endpoint_a/envelope.py` |
| PBS-MUX-01 container | `pbs_endpoint_a/mux.py` |
| PBS-SEC-B-01 Security frame, suites `0xFF01` (HMAC-SHA-256, Python `hmac`) and `0xFF02` (Ed25519, `cryptography`), replay window, PBS-AUTH-01 Authority Context, rover policy, receive steps R1 to R14 | `pbs_endpoint_a/secb.py` |
| Bundle lifetime from TTL (PBS-DTN-MAP-01 6.1, 6.1.1; Section 7.4), priority to ION class of service (Section 7.5) | `pbs_endpoint_a/envelope.py` |
| Access to the ROVER ION node | `pbs_endpoint_a/ion.py`, a ctypes binding to ION's `libbp` and `libici` |
| Rover program | `pbs_endpoint_a/rover.py` (`bin/rover`) |
| Telemetry plans | `pbs_endpoint_a/plan.py`, `plans/example.json` |
| Log writer | `pbs_endpoint_a/jsonlog.py` |
| Vector runner, envelope build/parse, log cross-check (S7) | `pbs_endpoint_a/conformance.py`, `pbs_endpoint_a/tool.py` (`bin/tool`) |
| Earth-side stand-in and fault injector for testing Endpoint A (not Endpoint B) | `pbs_endpoint_a/harness.py` (`bin/harness`) |

## PBS_LINK: pinned git URL

PBS_LINK is fetched from its git repository at a pinned commit. It is not
taken from a local path. `fetch_pbs_link.sh` clones
`https://github.com/Pale-Blue-Systems/PBS_LINK.git` at commit
`d3b81d32f91c10d30b5a834b7fb64a353285fd27` ("Release PBS_LINK 0.1.4") into
`build/pbs-link`. It then checks the SHA-256 of `PBS_LINK/__init__.py`
(`5cd058eb...cfef`) and `PBS_LINK/core.py` (`8862a468...8185`).
`pbs_endpoint_a/pbslink.py` repeats the same check on every import, so the
import fails if any other PBS_LINK is found. The `bin/` wrappers run the fetch
on first use.

`docs/test-vectors.json` was generated from commit `54ae20e`. On
2026-10-07 `git ls-remote` did not list `54ae20e` on the GitHub repository,
so the fetch pins `d3b81d3`, the head of the published branch
`v1.5.0-update` (pull request #5). The commits between `d3b81d3` and `54ae20e` change only
documentation. The `PBS_LINK/` package tree is the same in both
(`git rev-parse <commit>:PBS_LINK` gives `fa7b2927...6798` for each).

To use another checkout, set `PBS_LINK_DIR=<dir containing PBS_LINK/>`.
That checkout must pass the same hash check.

## Access to ION

The rover is one process. It loads `build/ion/lib/libbp.so` and
`libici.so` with ctypes and calls these functions:

- `bp_attach` and `bp_open("ipn:99.1")`: the process owns delivery for the
  endpoint and sends with it as the source.
- for each send: `sdr_begin_xn`, `Sdr_malloc`, `Sdr_write`, `sdr_end_xn`,
  then `ionCreateZco(ZcoSdrSource, ...)` and `bp_send(sap, "ipn:23.1",
  NULL, lifespan_s, class_of_service, NoCustodyRequested, 0, 0, NULL, zco,
  ...)`. Ancillary data is NULL, so the ordinal is 0 (A31).
- for receiving: `bp_receive(sap, &dlv, BP_BLOCKING)` in a receiver thread,
  `zco_source_data_length` / `zco_start_receiving` / `zco_receive_source`
  to read the payload, then `bp_release_delivery`. The main thread wakes it
  with `bp_interrupt`.
- at exit: `bp_close` and `bp_detach`.

This is the same sequence of calls that ION's own `bpsource`, `bpsink` and
`bpchat` make. The ctypes struct layouts (`BpDelivery`, `BpTimestamp`,
`ZcoReader`) are compared with the C compiler's offsets in
`tests/test_pins.py`, which compiles a probe against `build/ion/include`
with the flags from `build/ion/lib/pkgconfig/ion.pc`.

The rover uses this binding, not ION's command-line tools. `bpsendfile`
opens its source EID with `bp_open` (SPEC-DIGEST 7.1), and ION allows only
one process to hold an endpoint open, so `bpsendfile` cannot send from
`ipn:99.1` while a receiver holds it open. With one process, the send and
receive times are also taken from one clock in one place.

The process attaches to the node named by `ION_NODE_WDNAME`, and
`ION_NODE_LIST_DIR` must be the same string the node was started with
(`ion/README.md`, gotcha 1). `bin/rover` sets both from `--ion-node-dir` /
`--ion-node-list-dir`. When those options are absent it uses
`$ION_NODE_WDNAME`, then `$ION_RUN_DIR/rover`, then `build/run/ion/rover`.

Before `bp_attach`, the binding checks that the node is listed in
`$ION_NODE_LIST_DIR/ion_nodes` and that its working-memory segment (the
node's `wmKey`) exists. If either check fails, the rover exits with status 2
and does not call `bp_attach`. The check is needed because, with no node
running, `bp_attach` in ION 4.2.0 fails only after it has created the
host-global semaphore table. That left shared memory `0xee08` and
`/dev/shm/sem.ion:GLOBAL:0` behind in one run on 2026-10-07, and
`ion/start_nodes.sh` then refuses to start until they are removed
(`ipcrm -M 0xee08; rm /dev/shm/sem.ion:GLOBAL:0`). With the check in
place, the same run left nothing behind.

## Send procedure (telemetry)

For each planned envelope the rover does the following:

1. It reads the host clock and builds the envelope with PBS_LINK: Source ID
   `Rover-Alpha`, the next Sequence (the first is 1, one counter for all
   priorities, A5), Timestamp = that clock reading, the priority, the TTL,
   and a MUX container with one Telemetry, Event or Status frame.
2. It reads the clock again and computes the bundle lifetime by the
   Section 7.4 rule:
   - TTL 0 gives 2147483647 s.
   - Otherwise, `floor((TTL*1e6 - max(0, now + 1 s - Timestamp)) / 1e6)`
     seconds.
   - Below 1 ms the envelope is not sent (`GW_EXPIRED`). Below 1 s it is not
     sent either (`GW_LIFETIME_UNDER_1S`).
3. It calls `bp_send` with the class of service from the Section 7.5 table:
   CRITICAL and HIGH use 2, NORMAL uses 1, LOW and BULK use 0.
4. It writes a `send` record.

The envelope is handed over a few microseconds after its Timestamp. Because
of the 1 s margin and the rounding down, TTL 60 gives a 58 s lifetime, TTL
3600 gives 3598 s and TTL 86400 gives 86398 s. These values were measured in
the live run below.

A plan item may set `lifespan_override_s`. The lifetime passed to
`bp_send` is then that value instead of the Section 7.4 value, and the
record has `lifespan_rule: OVERRIDE`. This is fault injection, used to make a
receiver see an expired envelope. It does not conform to PBS-DTN-MAP-01 6.1.

## Receive procedure (commands)

Each delivered bundle payload goes through steps R1 to R14 of Section 6.6
(`secb.Receiver.receive`):

1. Envelope checks (R1 to R6): `LENGTH`, `MAGIC`, `CRC`, `PRIORITY`,
   `TTL_EXPIRED`, `LENGTH`.
2. MUX container (R7): `MUX_VERSION`, `MUX_MALFORMED`.
3. Data with no Command frame and no Security frame (R8) is accepted.
4. A Command frame without a Security frame (R8a) is rejected with
   `SEC_MISSING`.
5. Security frame (R9 to R9c): `SEC_MALFORMED`, `SEC_SUITE_UNKNOWN`,
   `SEC_KEY_UNKNOWN`.
6. Authentication over `envelope[0 : 44 + Size - auth_len]` (R10):
   `SEC_AUTH_FAIL`.
7. 64-entry replay window per (Source ID, key_id) (R11): `SEC_REPLAY`. The
   window is updated before authorization.
8. Authority Context (R12): `AUTH_CTX_MISSING`, `AUTH_CTX_MALFORMED`.
9. Authorization against the key binding and the policy (R13):
   `AUTHZ_DENIED`.
10. Accept (R14): one `exec` record per Command frame, with action
    `LOGGED`.

The rover holds the keys in `config/rover-keys.json`: the Ed25519 public key
of `TEST-ED25519-01` (no signing key) and the HMAC secret `TEST-HMAC-01`.
The policy is in `config/rover-policy.json`. **These are TEST KEYS.** They
are derived as SHA-256 of public labels, anyone can recompute them, and they
are not for operational use.

In R13 the Source ID is compared as the 16 raw header bytes with the
null-padded bound Source ID (A4). The reference receiver in
`docs/gen_test_vectors.py` compares the text before the first `0x00`
instead. The two methods give the same result on every vector. They differ
only for a Source ID with non-zero bytes after a `0x00`, which this
receiver rejects.

With `--reply-ping`, an accepted `PING` is answered with a Status envelope
(`PONG SEQ=<n> SECSEQ=<n>`, priority 1, TTL 60).

## Usage

```sh
endpoint-python/fetch_pbs_link.sh                 # optional; bin/* do it on first use
python3 -m pip install -r endpoint-python/requirements.txt   # cryptography; pytest for tests

# Rover on the ROVER node (nodes started with ion/start_nodes.sh)
endpoint-python/bin/rover --log results/S1/endpoint-a.jsonl --scenario S1 \
    --plan builtin:nominal:3:5 --duration 30
endpoint-python/bin/rover --log a.jsonl --plan endpoint-python/plans/example.json  # until SIGTERM
```

| Option | Default | Meaning |
|---|---|---|
| `--log` | required | JSON Lines log (appended) |
| `--scenario` | none | label written to every record |
| `--plan` | `builtin:none` | JSON plan file, `builtin:nominal[:COUNT[:INTERVAL_S]]` (one envelope per priority per round), or `builtin:none` |
| `--duration` | until SIGINT/SIGTERM | run time in seconds; plan items later than this are not sent |
| `--eid` / `--dest` / `--source-id` | `ipn:99.1` / `ipn:23.1` / `Rover-Alpha` | |
| `--sequence-start` | 1 | Sequence of the first envelope |
| `--ion-node-dir`, `--ion-node-list-dir` | see "Access to ION" | |
| `--keys`, `--policy` | `config/rover-keys.json`, `config/rover-policy.json` | |
| `--reply-ping` | off | answer accepted PING commands |
| `--ready-file` | none | created once `ipn:99.1` is open and the receiver thread runs |

The rover prints `rover: READY ...` when it is receiving. On SIGINT or SIGTERM
it stops sending, interrupts the receiver, writes `run_end`, closes the
endpoint and detaches from ION. Exit status: 0 for a normal end, 1 if
`bp_receive` failed while running, 2 if the endpoint could not be opened
(node not running, or `ipn:99.1` already held by another process).

Plan format: the docstring of `pbs_endpoint_a/plan.py` and
`plans/example.json`. Each item gives `at_s`, `count`, `interval_s`,
`priority`, `ttl_s` (default per PBS-ENV-01 12.4: 0, 60, 60, 3600, 86400),
`frame` (`telemetry`, `event`, `status`), `text`, `payload_bytes` (the exact
envelope Size; 0 gives a header-only envelope), `ack_req`, `label` and
`lifespan_override_s`.

Tools:

```sh
endpoint-python/bin/tool vectors --out a-vectors.json   # S7: all vectors; 'produced' = bytes built by A
endpoint-python/bin/tool build --source-id Earth-MCC --priority 2 --sequence 1 \
    --timestamp-us 1767225602000000 --ttl 60 --payload-hex 010101000e544c4d2050494e472d5245504c59 --ack
endpoint-python/bin/tool parse --now-us 1767225602000000 <hex> [<hex> ...]
endpoint-python/bin/tool crosscheck endpoint-b.jsonl --own-log endpoint-a.jsonl
```

`tool crosscheck` decodes every `envelope_hex` in the other endpoint's
`send` records with Endpoint A and compares the result field by field with
that endpoint's own values. With `--own-log`, it also finds the other
endpoint's `recv` record for each envelope Endpoint A sent, matching on
(Source ID, Sequence, Timestamp), and compares the SHA-256 and the header
bytes.

Test harness (Earth-side stand-in; signs with the TEST KEYS):

```sh
endpoint-python/bin/harness listen --duration 20 --log h.jsonl        # receive on ipn:23.1
endpoint-python/bin/harness inject --command "DRIVE 10" --secseq 7    # signed command to ipn:99.1
endpoint-python/bin/harness inject --command "DRIVE 10" --secseq 8 --tamper command
endpoint-python/bin/harness inject --command "DRIVE 10" --secseq 9 --sign-with TEST-ED25519-ROGUE
endpoint-python/bin/harness inject --command "PING" --secseq 10 --timestamp-offset-s -29.5 --lifespan 30
endpoint-python/bin/harness inject --hex <captured envelope>          # replay
```

`inject` opens its EID with `bp_open_source`, which only sends. It
therefore works while Endpoint B holds `ipn:23.1` open. Its log records
carry `endpoint: "H"`.

## Tests

```sh
endpoint-python/run_tests.sh                    # unit tests
PBS_ION_LIVE=1 endpoint-python/run_tests.sh     # plus the live test on three ION nodes
```

`run_tests.sh` fetches PBS_LINK. If `python3` cannot import `cryptography`
and `pytest`, it creates `build/venv-endpoint-python` and installs
`requirements.txt` into it.

| File | Content |
|---|---|
| `tests/test_vectors.py` | one test per check of `docs/test-vectors.json`, 237 checks in all. ENV-01 to ENV-15 are built from their inputs and compared byte for byte, then parsed. RX-01 to RX-22 (receiver cases), LT-01 to LT-10 (lifetime cases), the class-of-service table and the CRC-32 check value are run. The 19 CMD messages that have inputs are rebuilt, with `auth_input` and `auth_data` compared. The 30 deliveries of SB-01 to SB-21 are run with the rover's provisioned keys. |
| `tests/test_envelope.py` | header layout against an independent `struct`/`zlib` build; Source ID length refusal; Sequence start and wrap; TTL-before-length order; trailing byte; Size limit; lifetime rule and its bound |
| `tests/test_secb.py` | key file and policy against the vectors and the label derivations; one-bit flips at every bit position 0 and 7 of every byte of CMD-01, all rejected; replay window; replay state not consumed by rejected commands; authorization of every Command frame; malformed or duplicate Authority Context; two Security frames; MUX errors |
| `tests/test_rover.py` | rover logic with an in-memory transport: send records, `NOT_SENT` outcomes, command dispositions and `exec` records, PING reply, scheduler and receiver thread, stop |
| `tests/test_plan.py`, `tests/test_jsonlog.py` | plan expansion; log fields against the tables of `docs/LOG-SCHEMA.md` |
| `tests/test_pins.py` | PBS_LINK pin; ctypes layouts against ION's headers (needs gcc and a built ION) |
| `tests/test_live_ion.py` | opt-in (`PBS_ION_LIVE=1`): the rover on the three ION nodes; see below |

### Results (runs on 2026-10-07)

Host: 4 vCPU Intel Xeon 2.80 GHz, Ubuntu 24.04, Python 3.13.16, pytest
9.1.1, cryptography 50.0.1, PBS_LINK 0.1.4 (`d3b81d3`), ION
`ion-open-source-4.2.0` (`568df88`) built by `ion/build_ion.sh`.

- `endpoint-python/run_tests.sh`: 294 passed, 1 skipped (the live test), in
  1.13 s. `bin/tool vectors`: 237 checks, 237 passed. From a fresh `git
  clone` with ION not built, the script fetched PBS_LINK, created
  `build/venv-endpoint-python`, and gave 292 passed, 3 skipped (the live
  test and the two ION library tests).
- `PBS_ION_LIVE=1`: the live test passed in two runs. Run 1 ran the live
  test alone (49.0 s), before the pre-attach node check was added. Run 2
  ran the whole suite on the committed code (295 passed in 50.6 s). Output
  is in `build/run/endpoint-a-livetest-{1,2}`, which is not committed.

  Conditions:
  - three ION nodes on one host, LTP over UDP;
  - `ion/udp_forward.py` with fixed one-way delays of 0.25 s
    (rover-gateway) and 1.3 s (gateway-earth), no loss, no rate limit;
  - contact plan `ion/contacts/always_on.ionrc` (125000 B/s, ranges 1 s
    and 2 s).

  Results, the same in both runs except for the latencies:
  - The rover sent 11 envelopes: plan `builtin:nominal:2:2` (two rounds of
    priorities 0 to 4) plus one PING reply.
  - All 11 reached the harness on `ipn:23.1`, and each had the same SHA-256
    and header bytes as sent.
  - For the TTL 0 envelopes, `BpDelivery.timeToLive` read 4294966296. That
    is 2147483647000 mod 2^32, as A30 predicts. These bundles were sent
    with the 2147483647 s lifetime and were delivered.
  - Eight commands were injected from the EARTH node. The rover's
    dispositions are in the table below. Every received command had
    `eid_match: true` (bundle source `ipn:23.1`).

| Latency (envelope Timestamp to receipt) | Run 1 | Run 2 |
|---|---|---|
| Telemetry at Earth, min / median / max | 1.5696 / 1.5728 / 1.5792 s | 1.5683 / 1.5720 / 1.5855 s |
| Commands at the rover (neither expired nor replayed), min / max | 1.576 / 1.752 s | 1.571 / 1.770 s |

| Injected | Rover result |
|---|---|
| valid `PING`, Ed25519, security_sequence 1 | `ACCEPT OK`, `exec` `PING`, PING reply sent |
| `DRIVE 10` with the command byte changed after signing | `REJECT SEC_AUTH_FAIL` |
| `DRIVE 10` signed with `TEST-ED25519-ROGUE`, labelled `TEST-ED25519-01` | `REJECT SEC_AUTH_FAIL` |
| `DRIVE 10` with key_id `TEST-ED25519-ROGUE` | `REJECT SEC_KEY_UNKNOWN` |
| valid `SET_HEATER ON`, HMAC-SHA-256 | `ACCEPT OK`, `exec` `SET_HEATER ON` |
| `DRIVE 10`, role `SCIENCE-OPS` | `REJECT AUTHZ_DENIED` |
| `DRIVE 5`, Timestamp 29.5 s in the past, TTL 30, bundle lifetime override 30 s | `REJECT TTL_EXPIRED` |
| the bytes of the first `PING` again, 1 s later | `REJECT SEC_REPLAY` |

Two more checks were run on the same nodes, delays and plan (2026-10-07):

- The rover ran with `--plan builtin:nominal:100:1` and no `--duration`.
  It received SIGTERM 3.1 s after its first send, after 17 sends. It wrote
  `run_end` 17 ms after its last send and exited with status 0.
  `ion/stop_nodes.sh` then reported a clean stop.
- A second rover was started on the same node while the first held
  `ipn:99.1`. It exited with status 2 and the message
  `bp_open(ipn:99.1) failed`.

### PBS conformance tests exercised

The unit and live tests above exercise the following items. This is not a
conformance claim and not a certification.

| Reference | Exercised by |
|---|---|
| PBS-CONFORMANCE-01 4.1 to 4.3, 5.1 (header, receive order, CRC-32) | ENV and RX vectors; `test_envelope.py` |
| PBS-CONFORMANCE-01 7 (Sequence) | ENV-11, ENV-12; Sequence wrap test |
| PBS-CONFORMANCE-01 8, statements 1 and 2 (TTL 30, TTL 0) | RX-01 to RX-05 with an injected clock |
| PBS-CONFORMANCE-01 9 (discard and continue) | receiver cases; live run (rejections followed by acceptances) |
| PBS-CONFORMANCE-02 T002 (header unchanged over BPv7) | live run: 11 of 11 byte-identical |
| PBS-CONFORMANCE-02 T004 (expiry) | RX-03, RX-04, RX-06, SB-11; live `TTL_EXPIRED` |
| PBS-CONFORMANCE-02 T006 (tamper) | CMD-03 to CMD-06, bit-flip test; live tampered command |
| PBS-CONFORMANCE-02 T007 (replay) | SB-10, SB-21; live replay |
| PBS-CONFORMANCE-02 T008 (authorization) | SB-12 to SB-14; live `AUTHZ_DENIED` |
| PBS-CONFORMANCE-02 T010 (one class per priority) | class-of-service table; `class_of_service` in every `send` record |
| PBS-CONFORMANCE-02 T014 (unknown frame skipped) | RX-21, CMD-16 |
| PBS-CONFORMANCE-02 T015 (traceable dispositions) | `recv` and `exec` records |
| PBS-CONFORMANCE-02 T016 (TTL 0, no-expiry lifetime) | LT-08; live TTL 0 envelopes delivered |

## Limits

- One rover process per host and per node: ION lets one process hold
  `ipn:99.1` open, and the demo nodes use fixed IPC keys and ports.
- Replay state is kept in memory only. A restarted rover accepts
  security_sequence values it accepted before the restart.
- The PBS_LINK Sequence counter is kept in memory only. A restarted rover
  starts again from `--sequence-start`.
- `getIonVersionNbr()` returns an empty string until the process has
  attached. The rover reads it after `bp_attach`.
