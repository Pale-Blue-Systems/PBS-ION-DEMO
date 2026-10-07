# Report tools

| File | Purpose |
|---|---|
| `collect.py` | joins the endpoint logs of one run and writes CSV tables |
| `make_report.py` | writes REPORT.md, `results/summary.json`, `results/all-envelopes.csv` and charts |
| `charts.py` | PNG charts (matplotlib; skipped when it cannot be imported) |
| `compare_runs.py` | sets two or more complete runs side by side (repeatability) |

Both read only files written during a run: the endpoint logs
(`docs/LOG-SCHEMA.md`), `emulator-stats.json` (`emulator/README.md`),
`run.json` and the probe outputs (`scenarios/README.md`), the copied
`ion.log` files, and `verdict.json` from `scenarios/evaluate.py`.

```sh
python3 report/collect.py results/S2              # results/S2/*.csv
python3 report/make_report.py                     # REPORT.md from results/
python3 report/make_report.py --results results --out results/REPORT.md
python3 report/compare_runs.py --run "run D=results/repeat/run-D" --run "run E=results"
python3 report/make_report.py --name "run E" --compare "run D=results/repeat/run-D"
```

The last command wrote the REPORT.md in this repository (and, with
`--out results/REPORT.md`, its copy in `results/`). `results/` holds run E;
`results/repeat/` and `results/control/` are not scenario directories, so
`make_report.py` reads only run E from `results/`.

`compare_runs.py` reads complete results directories of `./run_demo.sh`
(`<S>/run.json`, `<S>/verdict.json`, the logs and `run_demo.log`) and lists,
in the order the runs were made: verdicts and wall time per scenario; the
measured value of every criterion in each run, marked where the text
differs; latency min, median and max per direction and priority with the
range across runs; and the emulator, `bpstats` and `ltpstats` counters
whose values differ. `make_report.py --compare` puts the same text into
REPORT.md as a Repeatability section. `run_demo.sh` does not call either
option; a new run replaces `results/` and REPORT.md without it.

## Joining send and receive records

Every `send` record of one endpoint is matched with a `recv` record of the
other endpoint that has the same envelope identity (Source ID bytes,
Sequence, Timestamp; `docs/LOG-SCHEMA.md` Section 4) and was written after
it. Send records are taken in time order and each takes the earliest
unmatched receipt, so a replayed envelope, which is byte-identical to its
original, is matched with the second receipt. A `recv` record left without a
`send` record is a duplicate or unknown envelope (criterion `no_unmatched`).

## Tables

`envelopes.csv`, one row per `send` record:

| Column | Meaning |
|---|---|
| `dir`, `sender` | `A->B` (rover to Earth) or `B->A` |
| `label`, `priority`, `priority_name`, `ttl_s`, `size`, `envelope_len`, `sequence`, `timestamp_us` | from the send record |
| `send_t_s` | send time, seconds after the traffic origin |
| `send_result`, `send_reason` | `SENT OK`, or `NOT_SENT` with `GW_EXPIRED` / `GW_LIFETIME_UNDER_1S` |
| `lifespan_s`, `lifespan_rule`, `class_of_service` | what was passed to `bp_send()` |
| `commands`, `suite_id`, `key_id`, `security_sequence` | protected command fields |
| `delivered`, `n_recv` | 1 when matched; receipts with this identity |
| `recv_t_s`, `latency_s` | receive time (scenario seconds); receive time minus Timestamp |
| `recv_result`, `recv_reason` | the receiver's disposition |
| `sha256_match`, `header_match` | SHA-256 of all bytes and the 44 header bytes equal at both ends |
| `eid_match`, `bundle_source_eid`, `bundle_ttl_field_ms`, `bundle_creation_lag_s` | bundle metadata at the receiver; creation lag = bundle creation time minus Timestamp |
| `exec_count` | `exec` records the receiver wrote for this envelope |

`receipts.csv` has one row per `recv` record; `links.csv` one row per
emulated link direction; `ionstats.csv` the last `bpstats` report of each
node (`src`, `fwd`, `xmt`, `rcv`, `dlv`, `rfw`, `exp` per ION priority class
0, 1, 2 and total).

## REPORT.md

For each scenario: the result and every criterion with its measured value;
the conditions (delays, loss, rate limits, outage windows, contact rates,
seed, traffic mix, plan origins); delivery and latency per direction and
priority (min, median, p95, max); for envelopes sent during an outage, the
time held beyond the 1.55 s propagation and the delivery time after the
outage ended; the `bplist` probes; S5 expiry outcomes per label; S6 command
dispositions; S7 offline comparison and cross-check output; emulator and
ION counters; charts; the conformance items exercised; and the file list.
The header records the test environment as PBS-CONFORMANCE-02 Section 4 asks:
implementations and versions, ION version and commit, emulator, host, clock
source, cryptographic suites and the SHA-256 of `docs/test-vectors.json`.

Charts (`results/charts/`): `<S>-timeline.png` (latency against send time,
outages shaded, envelopes never received marked), `<S>-latency.png`
(latency per priority), `S4-arrivals.png` (send and receive time per
priority). Priority is drawn with one blue ramp (CRITICAL darkest) and a
marker shape per priority; direction with filled (rover to Earth) or open
(Earth to rover) markers.
