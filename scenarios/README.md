# Scenarios

Seven automated scenarios run the two PBS endpoints over the three ION nodes
and the link emulator. Each one has its link conditions, contact plan,
traffic for both endpoints, pass/fail criteria and the PBS conformance items
it exercises in one file, `S1.json` to `S7.json`.

| File | Purpose |
|---|---|
| `S1.json` ... `S7.json` | scenario definitions |
| `scenario.py` | loads a definition; writes the contact plan, emulator configuration and both endpoint plans |
| `runner.py` | runs one scenario: emulator, ION nodes, endpoints, probes, clean stop, post-run commands |
| `evaluate.py` | applies the criteria to a run and writes `verdict.json` |
| `s7_compare.py` | S7 offline comparison of both endpoints against `docs/test-vectors.json` |
| `test_scenarios.py` | offline checks of the definitions (no ION needed) |

`./run_demo.sh` runs all of them; `./run_demo.sh S3` runs one. By hand:

```sh
python3 scenarios/runner.py scenarios/S3.json --out results/S3
python3 scenarios/evaluate.py results/S3
```

`runner.py` expects ION built in `build/ion`, Endpoint B in
`build/endpoint-c` (`PBS_EARTH_BIN`, `PBS_EARTH_TOOL` override) and a Python
with `cryptography` for Endpoint A (`PYTHON` overrides). `run_demo.sh` sets
all of these up.

## Time base

Scenario time 0 is the **traffic origin**, the instant both endpoint plans
start. All times in a scenario file are seconds of scenario time.

- The contact-plan reference time is `T0 = origin - lead_s` (default 10 s).
  `ion/start_nodes.sh` writes `@ T0` above the generated contact plan, so all
  three nodes load the same absolute windows. ION contact times are whole
  seconds, and so are the scheduled outage times.
- The emulator gets the origin as `--t0`; its blackout windows are in
  scenario time.
- Endpoint B gets the origin as `--t0-us`, so its plan items are sent at
  exactly `origin + at_s`.
- Endpoint A (`bin/rover`) starts its plan when it is ready. The runner
  launches it 0.1 s before the origin (its measured start-up time) and
  records the actual plan origin (`plan_origin_offset_s` in `run.json`).
- All evaluation uses the absolute times in the logs, so an offset of the
  rover plan does not change any verdict.

## Run sequence (`runner.py`)

1. Write `contacts.ionrc`, `emulator.json`, `rover-plan.json` and
   `earth-plan.jsonl` into the output directory.
2. Refuse to start if ION state already exists on the host (another demo or
   leftovers; `PBS_DEMO_WAIT_S` waits for it to clear).
3. Start the emulator, then the three nodes with `ion/start_nodes.sh`.
4. Start Endpoint A on the ROVER node (`bin/rover --ready-file ...`), then
   Endpoint B on the EARTH node (`pbs_earth run`).
5. Run the probes at their scenario times: `bplist` on a node lists the
   bundles it holds; `collect.py` reads each bundle's payload and identifies
   the PBS envelope by Sequence and Timestamp.
6. At `duration_s`: SIGTERM to both endpoints, `bpstats` on every node, copy
   each node's `ion.log`, `ion/stop_nodes.sh`, SIGTERM to the emulator.
7. Run the post-run commands (S7: offline cross-checks).
8. Write `run.json`: times, versions, exit statuses, probe records, and
   whether the ION stop was clean without the fallback.

Each scenario takes its traffic phase plus about 45 s (10 s lead, about 4 s
node start, about 27 s node stop).

## Definition format

| Key | Meaning |
|---|---|
| `id`, `title`, `summary` | text |
| `duration_s` | traffic phase length |
| `seed` | emulator loss seed |
| `links` | emulator settings per direction (`rover-gateway`, `gateway-rover`, `gateway-earth`, `earth-gateway`, or `*` for all): `delay_s`, `jitter_s`, `loss`, `rate_Bps`, `queue_bytes`. Default delay from `ion/topology.json` (0.25 s rover-Gateway, 1.3 s Gateway-Earth) |
| `contacts.rate_Bps` | contact-plan rate per leg (bytes/s; default 125000) |
| `outages` | `{leg, start_s, end_s, kind, label}`. `scheduled`: a gap in the contact plan in both directions and an emulator blackout in both directions. `unscheduled`: an emulator blackout only; ION is not told |
| `rover.plan`, `rover.args` | Endpoint A plan (`endpoint-python/pbs_endpoint_a/plan.py` format) and extra `bin/rover` arguments |
| `earth.plan` | Endpoint B plan items (`endpoint-c/README.md` format) |
| `probes` | `{at_s, node, tool, label}`, tool `bplist_detail` or `bplist` |
| `post` | commands run after the nodes stop |
| `criteria` | checks applied by `evaluate.py` (list of checks in its docstring); `informative: true` marks a measurement that does not decide the verdict |
| `conformance` | PBS-CONFORMANCE-01/02 and other clauses the scenario exercises |
| `charts` | extra charts for the report (`arrivals`) |

## The scenarios

Links run at the default delays (one-way 0.25 s rover-Gateway, 1.3 s
Gateway-Earth; 1.55 s rover to Earth) and the contact plan gives 125000
bytes/s per direction, unless a row says otherwise.

| | Conditions | Traffic | Pass criteria (summary) | Conformance items |
|---|---|---|---|---|
| S1 | no loss, no outage; 40 s | rover: 5 priorities x 6, a 20000-byte BULK envelope, a header-only envelope with ACK_REQ, PING replies; Earth: 8 signed commands (Ed25519 and HMAC, two roles), MCC telemetry, TTL 0 heartbeat | every envelope delivered once and byte-identical; all accepted; 8 commands executed in order; latency 1.55 to 3.0 s; TTL 0 sent with the 2147483647 s lifetime and delivered; class of service per priority; bundle source EID matches | C02 T002, T010, T015, T016; C01 4.1-4.3, 5.1, 6, 7 |
| S2 | scheduled outages: rover-Gateway 25-50 s, Gateway-Earth 65-85 s; 105 s | rover: 1 envelope/s rotating priorities (BULK 2000 bytes), PING replies; Earth: PING every 6 s | every envelope delivered and accepted; `bplist` shows envelopes held at the rover, the Gateway and Earth during the outages; envelopes sent during an outage arrive only after it ends; no datagram sent into a scheduled outage | C02 T011, T002; C01 4.2; DTN-MAP-01 9 |
| S3 | 5 % random loss on all four directions; unscheduled blackouts Gateway-Earth 40-55 s and rover-Gateway 75-80 s; 110 s | as S2 (BULK 3000 bytes), PING every 5 s | every envelope delivered and accepted, byte-identical; losses and blackout drops on every direction; LTP data retransmissions observed | C02 T011, T002; C01 4.2 |
| S4 | contact rate rover-Gateway 4000 bytes/s; emulator 4400 bytes/s with a 65536-byte queue on that leg; 60 s | rover: 40 BULK x 3000 bytes at 2 s, then 5 each of CRITICAL, HIGH, NORMAL, LOW 3 s apart; Earth: 2 PINGs | CRITICAL/HIGH/NORMAL within 4.0 s and ahead of earlier BULK; slowest CRITICAL/HIGH faster than median BULK; LOW (same ION class as BULK) never overtakes BULK; emulator queue never full | C02 T012, T010; C01 6; DTN-MAP-01 6.3 |
| S5 | scheduled outage rover-Gateway 20-50 s; 70 s | both: TTL 1 and TTL 2 (not sendable), TTL 1 with a 60 s lifetime override (fault injection), TTL 10/20 during the outage, TTL 60/0 and a TTL 30 command during the outage, TTL 3 observations | TTL 1 `GW_EXPIRED` and TTL 2 `GW_LIFETIME_UNDER_1S` at both endpoints; injected envelopes `TTL_EXPIRED` at both receivers; no TTL 10/20 envelope delivered; `bplist` shows them held and then gone; `bpstats` counts the expiries; control envelopes delivered after the outage | C02 T004, T011, T016; C01 4.2, 9 |
| S6 | no loss, no outage; 42 s | Earth: 22 commands (valid, 6 tamper modes plus CRC, replays, rogue keys, unknown key_id, expired, roles, wrong epoch, unsigned, no Authority Context, unknown frame); rover: telemetry, PING reply | every command gets its expected disposition; only the 5 accepted commands execute; every command has an audit record | C02 T004, T006, T007, T008, T014, T015; C01 9 |
| S7 | no loss, no outage; 25 s | both: all priorities, header-only, ACK_REQ, up to 65536 bytes (A) and 65000 bytes (B), unknown frame types | both endpoints build identical bytes for every vector input and give the expected disposition for every receiver case; each endpoint decodes the other's logged envelopes with identical fields; all delivered byte-identical and accepted | C02 T014, T002; C01 4.1, 4.3, 5.1, 7, 10 |

C01 = PBS-CONFORMANCE-01 (cited by section; it has no test identifiers),
C02 = PBS-CONFORMANCE-02 (test identifiers from its Section 3). The full
mapping per scenario is in each file's `conformance` list and in REPORT.md.
The demo exercises these items; it does not claim conformance or
certification.

Fault injection is marked in the logs: a bundle lifetime chosen by the
operator instead of the PBS-DTN-MAP-01 6.1 value is logged with
`lifespan_rule: OVERRIDE` (S5, S6), and Endpoint B's tamper modes alter an
envelope after it is built and signed, before it is handed to ION (S6).

## Limits

- One demo per host: the nodes use fixed shared-memory keys and UDP ports.
- All nodes, endpoints and the emulator share one host clock, so latency has
  no clock-offset error; it does include host scheduling.
- ION contact times and ranges are whole seconds. The contact plan uses
  ranges of 1 s and 2 s for one-way delays of 0.25 s and 1.3 s.
- The pass thresholds that involve time (S1-7, S4-5, S4-6) were set from the
  emulated propagation and the measured runs on the host described in
  REPORT.md; a much slower host can exceed them.
