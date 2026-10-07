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
| S3 | 5 % random loss on all four directions; unscheduled blackouts Gateway-Earth 40-55 s and rover-Gateway 75-80 s; 110 s | as S2 (BULK 3000 bytes), PING every 5 s | every envelope delivered and accepted, byte-identical; every receipt is a sent envelope (repeated deliveries counted, not failed); losses and blackout drops on every direction; LTP data retransmissions observed | C02 T011, T002; C01 4.2 |
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

## Recorded run

`results/` and `REPORT.md` in this repository are the output of one
`./run_demo.sh` in a fresh `git clone` of commit `88747a8` into
`/home/user/PBS-ION-DEMO-run`, started 2026-10-07 04:07 UTC on the host
described in REPORT.md (4 vCPU Intel Xeon 2.80 GHz, Linux 6.18, Python
3.13.16). It took 861 s in all: 102 s to clone and build ION, 16 s for the
Python environment, PBS_LINK and Endpoint B, 5 s for the self-checks, and
65 to 150 s per scenario (S1 80 s, S2 144 s, S3 150 s, S4 99 s, S5 110 s,
S6 82 s, S7 65 s). All 7 scenarios passed all 85 of their required
criteria, and every ION stop was clean without the fallback. Paths inside
`results/` (`run.json`, `argv` fields) are those of that clone.

Headline numbers from that run (REPORT.md has the rest):

| | Measured |
|---|---|
| S1 | 46 of 46 envelopes delivered and accepted, byte-identical; latency 1.571 to 1.601 s against the 1.55 s emulated propagation |
| S2 | 127 of 127 delivered; 21 rover envelopes held at the rover at 45 s and 18 bundles at the Gateway at 80 s (`bplist`); rover envelopes sent during the rover-Gateway outage delivered 3.19 to 3.56 s after it ended, those sent during the Gateway-Earth outage 2.68 to 2.82 s after it ended |
| S3 | 119 of 119 delivered; the emulator dropped 52 datagrams at random and 91 in the two blackouts; LTP sent 88 data segments again; the Gateway cancelled 1 LTP block and BP forwarded that bundle again; maximum latency 24.15 s |
| S4 | CRITICAL 1.95 to 2.66 s, HIGH 2.45 to 2.92 s, NORMAL 1.98 to 2.46 s while the 40 BULK envelopes took 2.30 to 31.70 s (median 17.16 s); LOW 16.62 to 28.48 s, behind the BULK envelopes sent before them |
| S5 | TTL 1 and TTL 2 not sent at either endpoint (`GW_EXPIRED`, `GW_LIFETIME_UNDER_1S`); the three fault-injected expired envelopes rejected with `TTL_EXPIRED`; 0 of 11 TTL 10/20 envelopes delivered and 9 of 9 TTL 60/0/30 envelopes delivered after the outage; `bpstats` counted 9 expiries at the rover and 5 at Earth |
| S6 | 22 of 22 commands with the expected disposition; 5 executed |
| S7 | 34 of 34 built vectors identical between the endpoints and the vector file; 22 of 22 receiver cases and 30 of 30 SEC-B deliveries with the expected result at both endpoints; both cross-checks exit 0 |

Six complete runs were made from fresh clones on 2026-10-07: commit
`70a05d7` at 02:51 UTC, `ce737fa` at 03:06, `6843d33` at 03:20, `cba2de1` at
03:35, and `88747a8` at 03:51 (in another directory) and at 04:07 (the
recorded run). The later commits added `ltpstats` to the runner and changed
the report, the S5 TTL 2/TTL 3 item (now an observation without an expected
outcome) and the S3 duplicate criterion; traffic, links and outages were the
same in all six. Every scenario passed in every run except S3 at 03:35
(`cba2de1`): its criterion then required no duplicate receipt, and Earth
received one BULK envelope twice (observation 4). The tables below compare
the runs.

## Observations

1. **ION starts sending about 1.1 to 1.3 s after a scheduled contact
   starts.** ION 4.2.0 schedules the start of transmission on a contact at
   the contact start plus `maxClockError` (1 s by default) and the end
   `maxClockError` before the contact end (`ici/library/rfx.c`, contact
   insertion, about lines 880 to 930; default in `ici/library/ion.c` line
   970). In the six runs the first datagram after an S2 outage reached the
   emulator 1.240 to 1.272 s (rover-Gateway) and 1.141 to 1.343 s
   (Gateway-Earth) after the outage end, and no node sent a datagram into a
   scheduled outage (S2-15: 0 blackout drops in every run).

2. **LTP screening can add one checkpoint timeout (about 5 s) after a
   contact resumes.** ION's LTP engine discards segments from a peer while
   the contact plan gives that peer no transmission to the local node
   (`m screening`, on by default; `ltp/library/libltpP.c` line 6716; counted
   by `ltpstats` as `in_seg_screened`). The receiving node opens reception
   at the contact start plus the contact-plan range
   (`ici/daemon/rfxclock.c` lines 430 to 445), and rfxclock processes its
   events in a loop that sleeps 1 s (line 720). The ranges are whole
   seconds, 2 s for the 1.3 s Gateway-Earth delay and 1 s for the 0.25 s
   rover-Gateway delay. After the S2 outages:

   | Run | Gateway to Earth: blocks started in the 3 s after the leg resumed / checkpoint resent before a report | Screened at Earth | Rover envelopes held at the Gateway, delivered after the outage end | Gateway to rover: blocks / resent | Screened at the rover | Earth commands held at the Gateway, delivered after the outage end |
   |---|---|---|---|---|---|---|
   | `70a05d7` 02:51 | 30 / 23 | not recorded | 7.71 to 12.68 s | 5 / 0 | not recorded | 1.55 to 1.59 s |
   | `ce737fa` 03:06 | 30 / 24 | 34 | 7.69 to 10.40 s | 5 / 0 | 0 | 1.56 to 1.59 s |
   | `6843d33` 03:20 | 30 / 0 | 0 | 2.64 to 2.74 s | 5 / 4 | 4 | 4.52 to 4.55 s |
   | `cba2de1` 03:35 | 30 / 24 | 34 | 7.66 to 15.30 s | 5 / 0 | 0 | 1.54 to 1.59 s |
   | `88747a8` 03:51 | 30 / 24 | 34 | 7.67 to 12.67 s | 5 / 0 | 0 | 1.52 to 1.59 s |
   | `88747a8` 04:07 (recorded) | 30 / 0 | 0 | 2.68 to 2.82 s | 5 / 0 | 0 | 1.57 to 1.65 s |

   (`ltpstats` was added in `ce737fa`.) In the runs with resent
   checkpoints towards Earth, the unanswered blocks reached Earth 2.63 to
   2.75 s after the outage end, 0.63 to 0.75 s after the reception start
   computed from the range, and blocks reaching Earth from 3.56 s on were
   answered at once; in `6843d33` and in the recorded run, blocks reaching
   Earth 2.63 and 2.68 s after the end were answered. Which leg was
   affected, and whether at all, varied between runs with identical inputs,
   which is consistent with the once-per-second rfxclock loop. Every
   envelope was delivered in every run. ION offers `m screening n`
   (ltprc(5) recommends it for research use only); it is not used here.
   In the recorded run two rover blocks sent after the rover-Gateway outage
   were also answered only after the rover resent their checkpoints 2.74 s
   later, while the Gateway counted no screened segment; the cause of those
   two was not identified.

3. **ION keeps a bundle at its source node when no route arrives before
   its lifetime ends.** In S5 the Earth node held its three TTL 10
   envelopes (bundle lifetime 9 s) from the start of the outage until they
   expired (`bplist` at 26.4 s: 3, at 36.4 s: 0; `bpstats` counts them as
   expired at Earth), although the Gateway-Earth leg was up; the rover-Gateway
   leg was down until 50 s. The Earth TTL 60 envelopes and the TTL 30 command
   sent at the same time went to the Gateway and waited there.

4. **A bundle can be delivered twice after an LTP block is cancelled.**
   Under S3's loss, the Gateway cancelled LTP blocks towards Earth and BP
   forwarded those bundles again (`rfw` in `bpstats`): 3, 0, 1, 2, 1 and 1
   bundles in the six runs. At 03:35 (`cba2de1`), Earth received one BULK
   envelope twice, 0.61 s apart, with the same bundle creation time; both
   copies were byte-identical and accepted (telemetry has no replay
   protection in PBS; a repeated command would be rejected by the
   PBS-SEC-B-01 replay window). S3 now counts and lists repeats (S3-8).

5. **Very short lifetimes are at the mercy of whole-second ION timing.** The
   S5 observation envelopes over the 1.55 s path, whose contact-plan ranges
   sum to 3 s:

   | Run | A, TTL 3 (lifetime 1 s) | B, TTL 3 (lifetime 2 s) | B, TTL 2 at its Timestamp (lifetime 1 s) |
   |---|---|---|---|
   | `70a05d7` 02:51 | not delivered | delivered, accepted | not delivered |
   | `ce737fa` 03:06 | not delivered | not delivered | not delivered |
   | `6843d33` 03:20 | not delivered | delivered, accepted | not delivered |
   | `cba2de1` 03:35 | not delivered | delivered, accepted | not delivered |
   | `88747a8` 03:51 | not delivered | not delivered | not delivered |
   | `88747a8` 04:07 (recorded) | not delivered | not delivered | not delivered |

   Endpoint A and Endpoint B give TTL 3 different lifetimes, and TTL 2
   different outcomes, while both follow SPEC-DIGEST.md 7.4: Endpoint A
   reads the clock again after building the envelope (bound 1999 ms and
   999 ms, floored to 1 s and 0 s), Endpoint B computes from the clock
   reading it used as Timestamp (2000 ms and 1000 ms). S5 therefore gives
   Endpoint B's `GW_LIFETIME_UNDER_1S` case a Timestamp 1 ms in the past.

## Limits

- One demo per host: the nodes use fixed shared-memory keys and UDP ports.
- All nodes, endpoints and the emulator share one host clock, so latency has
  no clock-offset error; it does include host scheduling.
- ION contact times and ranges are whole seconds. The contact plan uses
  ranges of 1 s and 2 s for one-way delays of 0.25 s and 1.3 s.
- The pass thresholds that involve time (S1-7, S4-5, S4-6) were set from the
  emulated propagation and the measured runs on the host described in
  REPORT.md; a much slower host can exceed them.
