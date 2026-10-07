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
| S3 | 5 % random loss on all four directions; unscheduled blackouts Gateway-Earth 40-55 s and rover-Gateway 75-80 s; 110 s | as S2 (BULK 3000 bytes), PING every 5 s | every envelope delivered and accepted, byte-identical; every receipt is a sent envelope, and a repeated delivery only as an ION re-forward of the same bundle; losses and blackout drops on every direction; LTP data retransmissions observed | C02 T011, T002; C01 4.2 |
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

## Recorded runs

`results/` and `REPORT.md` are the output of `./run_demo.sh` in a fresh
`git clone` of commit `7095caa` in `/home/user/pbs-ion-demo-runs/run-E`
(run E, started 2026-10-07 05:44:59 UTC). `results/repeat/run-D/` holds a
second complete run from another fresh clone of the same commit in
`/home/user/pbs-ion-demo-runs/run-D` (run D, 05:30:42 UTC). REPORT.md's
Repeatability section, written by `report/compare_runs.py`, compares the
two criterion by criterion. `results/control/` holds two S2 runs made in
run E's clone after run E with the `m maxbacklog` lines removed from
`ion/nodes/*/node.ltprc`, that is with ION's default LTP delivery backlog
limit (observation 6). Paths inside `results/` (`run.json`, `argv` fields)
are those of the clones. `7095caa` differs from the commit holding these
files only in documentation, comments in `ion/nodes/*/node.ltprc`,
REPORT.md and `results/`.

Host (REPORT.md has the details): 4 vCPU Intel Xeon 2.80 GHz, Linux 6.18,
Python 3.13.16, ION 4.2.0 built by `ion/build_ion.sh`. Run E took 853 s,
of which 97 s cloning and building ION; run D 857 s, of which 99 s for ION.
Both passed all 85 required criteria of the 7 scenarios, and every ION stop
was clean without the fallback.

| | Run E (run D where it differs) |
|---|---|
| S1 | 46 of 46 envelopes delivered and accepted, byte-identical; latency 1.569 to 1.612 s (D: 1.569 to 1.597 s) against the 1.55 s emulated propagation |
| S2 | 127 of 127 delivered; at 45 s the rover held 20 rover envelopes sent since the rover-Gateway outage began (22 bundles in all), at 80 s the Gateway held 17 sent since 63 s (`bplist`); rover envelopes sent during the rover-Gateway outage arrived 3.14 to 3.43 s after it ended (D: 3.01 to 3.28 s), those sent during the Gateway-Earth outage 7.70 to 10.39 s after it ended (D: 2.62 to 2.69 s). In run E Earth screened the Gateway's first 34 LTP segments after that outage, which cost one checkpoint timeout (observation 2); in run D it screened none. Neither run discarded a segment for delivery backlog |
| S3 | 119 of 119 delivered; the emulator dropped 53 datagrams at random and 84 in the two blackouts (D: 52 and 84); LTP sent 85 data segments again (same in D); the Gateway cancelled 3 LTP blocks towards Earth and BP re-forwarded 3 bundles (D: 2 and 2); in each run one BULK envelope reached Earth twice as the same bundle (E: sent at 76.0 s, copies 0.14 s apart; D: sent at 41.0 s, copies 0.61 s apart), which S3-8 admits (observation 4); maximum latency 22.17 s (D: 26.60 s) |
| S4 | CRITICAL 2.06 to 2.66 s, HIGH 2.57 to 2.92 s, NORMAL 2.10 to 2.45 s while the 40 BULK envelopes took 2.30 to 31.83 s (median 16.95 s); LOW 16.74 to 28.59 s, behind the BULK envelopes sent before them. Every one of these figures differs by at most 0.014 s in run D |
| S5 | TTL 1 and TTL 2 not sent at either endpoint (`GW_EXPIRED`, `GW_LIFETIME_UNDER_1S`); the three fault-injected expired envelopes rejected with `TTL_EXPIRED`; 0 of 11 TTL 10/20 envelopes delivered and 9 of 9 TTL 60/0/30 envelopes delivered after the outage; `bpstats` counted 9 expiries at the rover and 4 at Earth (D: 9 and 5) |
| S6 | 22 of 22 commands with the expected disposition; 5 executed (same in D) |
| S7 | 34 of 34 built vectors identical between the endpoints and the vector file; 22 of 22 receiver cases and 30 of 30 SEC-B deliveries with the expected result at both endpoints; both cross-checks exit 0 (same in D) |

### All runs on 2026-10-07

| Started (UTC) | Commit | Where | Total | Result |
|---|---|---|---|---|
| 02:51 | `70a05d7` | fresh clone | | all 7 passed |
| 03:06 | `ce737fa` | fresh clone | | all 7 passed |
| 03:20 | `6843d33` | fresh clone | | all 7 passed |
| 03:35 | `cba2de1` | fresh clone | | S3 failed: a repeated delivery, under the criterion of that time (observation 4) |
| 03:51 | `88747a8` | fresh clone | | all 7 passed |
| 04:07 | `88747a8` | fresh clone | 861 s | all 7 passed (`results/` in commit `20e6f9a`) |
| 04:24 | `20e6f9a` | working tree, ION already built; the report tools, `evaluate.py` and `S3.json` were edited during the run, so S3 to S7 record a modified tree (each scenario is judged by the definition copied at its start) | 745 s | all 7 passed |
| 04:37 | `9dfb41f` | fresh clone (run A) | 868 s | S2 failed: S2-1 and S2-4, 109 of 111 rover envelopes delivered in the traffic phase (observation 6); the other 6 passed |
| 04:51 | `9dfb41f` with the `de92b9c` LTP change, not yet committed | working tree, S2 only | 159 s | S2 passed |
| 04:55 | `de92b9c` | fresh clone (run B) | 862 s | all 7 passed |
| 05:09 | `de92b9c` | fresh clone (run C) | 859 s | all 7 passed |
| 05:30 | `7095caa` | fresh clone (run D) | 857 s | all 7 passed (`results/repeat/run-D/`) |
| 05:44 | `7095caa` | fresh clone (run E) | 853 s | all 7 passed (`results/`) |
| 05:59 | `7095caa`, `m maxbacklog` lines removed | run E's clone, S2 only (control 1) | 148 s | S2 passed (`results/control/maxbacklog-default-1/`) |
| 06:01 | `7095caa`, `m maxbacklog` lines removed | run E's clone, S2 only (control 2) | 148 s | S2 passed (`results/control/maxbacklog-default-2/`) |

The runs up to 04:07 are described by the earlier version of this file;
their files were not kept except those of 04:07. The runs from 04:24 to
05:09 were made in a temporary directory whose path every log records;
their files are not in the repository, and the values quoted from them
below were read from those files before they were set aside. Between
`70a05d7` and `7095caa` the traffic, links and outages of every scenario
were the same; the commits changed the runner, the report, two criteria
(`docs/CRITERIA-CHANGES.md`) and, in `de92b9c`, the LTP delivery backlog
limit of the nodes.

## Observations

1. **ION starts sending about 1.1 to 1.3 s after a scheduled contact
   starts.** ION 4.2.0 schedules the start of transmission on a contact at
   the contact start plus `maxClockError` (1 s by default) and the end
   `maxClockError` before the contact end (`ici/library/rfx.c`, contact
   insertion, about lines 880 to 930; default in `ici/library/ion.c` line
   970). In the six runs up to 04:07 the first datagram after an S2 outage
   reached the emulator 1.240 to 1.272 s (rover-Gateway) and 1.141 to
   1.343 s (Gateway-Earth) after the outage end, and in the nine S2 runs
   from 04:24 to 06:01 1.228 to 1.283 s and 1.285 to 1.362 s. No node sent
   a datagram into a scheduled outage (S2-15: 0 blackout drops in every
   run).

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
   | `88747a8` 04:07 | 30 / 0 | 0 | 2.68 to 2.82 s | 5 / 0 | 0 | 1.57 to 1.65 s |
   | `20e6f9a` 04:24 | 31 / 2 | 2 | 2.72 to 7.67 s | 5 / 0 | 0 | 1.56 to 1.61 s |
   | `9dfb41f` 04:37 (run A) | 30 / 24 | 34 | 7.72 to 15.39 s; two envelopes sent after the outage arrived after the traffic phase | 5 / 0 | 0 | 1.56 to 1.64 s |
   | `9dfb41f` and `de92b9c` LTP change, 04:51 (S2 only) | 30 / 23 | 33 | 7.74 to 10.44 s | 5 / 0 | 0 | 1.53 to 1.59 s |
   | `de92b9c` 04:55 (run B) | 30 / 24 | 34 | 7.67 to 10.42 s | 5 / 0 | 0 | 1.58 to 1.66 s |
   | `de92b9c` 05:09 (run C) | 30 / 24 | 34 | 7.68 to 10.42 s | 5 / 0 | 0 | 1.52 to 1.57 s |
   | `7095caa` 05:30 (run D) | 31 / 0 | 0 | 2.62 to 2.69 s | 5 / 0 | 0 | 1.50 to 1.52 s |
   | `7095caa` 05:44 (run E) | 31 / 24 | 34 | 7.70 to 10.39 s | 5 / 0 | 0 | 1.56 to 1.59 s |
   | `7095caa` default backlog limit, 05:59 (control 1) | 31 / 24 | 34 | 7.67 to 10.40 s | 5 / 1 | 1 | 1.50 to 4.51 s |
   | `7095caa` default backlog limit, 06:01 (control 2) | 30 / 23 | 33 | 7.69 to 15.34 s | 5 / 0 | 0 | 1.54 to 1.64 s |

   (`ltpstats` was added in `ce737fa`.) In the runs up to 04:07 with
   resent checkpoints towards Earth, the unanswered blocks reached Earth
   2.63 to 2.75 s after the outage end, 0.63 to 0.75 s after the reception
   start computed from the range, and blocks reaching Earth from 3.56 s on
   were answered at once; in `6843d33` and at 04:07, blocks reaching Earth
   2.63 and 2.68 s after the end were answered. In the nine S2 runs from
   04:24 the Gateway's first blocks reached Earth 2.59 to 2.66 s after the
   outage end; Earth screened the whole first burst (33 or 34 segments) in
   seven of them, 2 segments at 04:24 and none in run D, whose first block
   arrived earliest (2.59 s). Which leg was affected, and whether at all,
   varied between runs with identical inputs, which is consistent with the
   once-per-second rfxclock loop. Screening alone delayed envelopes but lost none; in run A
   it combined with the LTP delivery backlog limit and two envelopes missed
   the traffic phase (observation 6). ION offers `m screening n` (ltprc(5)
   recommends it for research use only); it is not used here. At 04:07 two
   rover blocks sent after the rover-Gateway outage were also answered only
   after the rover resent their checkpoints 2.74 s later, while the Gateway
   counted no screened segment; the cause of those two was not identified.

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
   bundles in the six runs up to 04:07, and 1, 2, 2, 0, 2 and 3 in the
   complete runs at 04:24, 04:37, 04:55, 05:09, 05:30 (run D) and 05:44
   (run E). Earth received one BULK envelope twice in three of these twelve
   runs: at 03:35 (`cba2de1`) 0.61 s apart; in run D sequence 48, sent at
   41.0 s, at 67.618 and 68.230 s; in run E sequence 90, sent at 76.0 s, at
   92.248 and 92.384 s. Each time both copies had the same bundle creation
   time and count, were byte-identical and were accepted (telemetry has no
   replay protection in PBS; a repeated command would be rejected by the
   PBS-SEC-B-01 replay window). S3-8 admits a repeat only when it is the
   same bundle and ION re-forwarded at least as many bundles
   (`docs/CRITERIA-CHANGES.md`); it passed in runs D and E on that basis.

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
   | `88747a8` 04:07 | not delivered | not delivered | not delivered |
   | `20e6f9a` 04:24 | not delivered | not delivered | not delivered |
   | `9dfb41f` 04:37 (run A) | not delivered | not delivered | not delivered |
   | `de92b9c` 04:55 (run B) | not delivered | delivered, accepted | not delivered |
   | `de92b9c` 05:09 (run C) | not delivered | delivered, accepted | not delivered |
   | `7095caa` 05:30 (run D) | not delivered | delivered, accepted | not delivered |
   | `7095caa` 05:44 (run E) | not delivered | delivered, accepted | not delivered |

   Endpoint A and Endpoint B give TTL 3 different lifetimes, and TTL 2
   different outcomes, while both follow SPEC-DIGEST.md 7.4: Endpoint A
   reads the clock again after building the envelope (bound 1999 ms and
   999 ms, floored to 1 s and 0 s), Endpoint B computes from the clock
   reading it used as Timestamp (2000 ms and 1000 ms). S5 therefore gives
   Endpoint B's `GW_LIFETIME_UNDER_1S` case a Timestamp 1 ms in the past.

6. **LTP discards checkpoints while its delivery backlog is full.** ION's
   LTP engine discards every red segment, checkpoints included, while the
   blocks waiting for delivery to BP number `m maxbacklog` (default 10;
   counted as `in_seg_too_fast`). In run A, Earth screened the Gateway's
   burst of 24 blocks after the S2 Gateway-Earth outage (observation 2);
   the Gateway resent the 24 checkpoints within 21 ms, Earth answered
   exactly 10 and discarded the others, 19 segments over two checkpoint
   rounds, each costing a 5.0 s checkpoint timeout. Two BULK envelopes,
   sent by the rover at 86.1 and 91.1 s, were discarded in both rounds;
   their data reached Earth again at 105.32 s and Earth reported both
   blocks complete at 105.33 s, after the endpoints had stopped at the end
   of the traffic phase (105.04 s), and S2 failed. The 32 export sessions
   per span let a peer have 32 blocks open towards a node, so a limit of 10
   could not absorb a full window. `de92b9c` sets `m maxbacklog` to the
   blocks the peers can have open (32 at the rover and Earth, 64 at the
   Gateway). The five S2 runs with the raised limit (04:51, runs B, C, D
   and E) discarded no segment for backlog on any node. Four of them had
   the same screening (33 or 34 segments) and delivered the rover
   envelopes held at the Gateway at most 10.44 s after the outage end; run
   D had no screening and delivered them at most 2.69 s after it. Two
   control runs of S2 with the `m maxbacklog` lines removed
   (`results/control/`), both with the same screening, discarded 1 segment
   from the rover at the Gateway (control 1) and 5 segments at Earth
   (control 2); in control 2 the held envelopes took up to 15.34 s. Both
   controls passed S2, so the default limit does not make S2 fail every
   time. It discarded segments in all three runs with it that had the
   screening (run A: 19 at Earth; control 1: 1 at the Gateway; control 2:
   5 at Earth), and no run with the raised limit did. `ion/README.md`
   item 15 has the details; REPORT.md's LTP table shows the counter for
   every scenario.

## Limits

- One demo per host: the nodes use fixed shared-memory keys and UDP ports.
- All nodes, endpoints and the emulator share one host clock, so latency has
  no clock-offset error; it does include host scheduling.
- ION contact times and ranges are whole seconds. The contact plan uses
  ranges of 1 s and 2 s for one-way delays of 0.25 s and 1.3 s.
- The pass thresholds that involve time (S1-7, S4-5, S4-6) were set from the
  emulated propagation and the measured runs on the host described in
  REPORT.md; a much slower host can exceed them.
