# PBS ION demo

## Endpoint A (rover, Python)

`endpoint-python/` is the PBS endpoint on the rover. It runs on the ROVER ION
node, uses the rover's own EID `ipn:99.1`, and reaches ION through a ctypes
binding to ION's `libbp`. It builds and parses envelopes with PBS_LINK 0.1.4,
which `endpoint-python/fetch_pbs_link.sh` fetches from GitHub at commit
`d3b81d3` and checks by SHA-256. PBS-MUX-01, PBS-AUTH-01 and PBS-SEC-B-01 are
implemented in Python from `docs/SPEC-DIGEST.md`. The rover sends telemetry at
all five priorities, verifies commands signed by Earth (Ed25519 or
HMAC-SHA-256, TEST KEYS), and logs accepted commands. It rejects tampered,
replayed, wrongly signed, unauthorized and expired commands with a reason
code, and writes JSON lines in the format of `docs/LOG-SCHEMA.md`.

```sh
endpoint-python/run_tests.sh                     # unit tests and docs/test-vectors.json
PBS_ION_LIVE=1 endpoint-python/run_tests.sh      # plus a run on the three ION nodes
endpoint-python/bin/rover --log endpoint-a.jsonl --scenario S1 --plan builtin:nominal:3:5 --duration 30
endpoint-python/bin/tool vectors --out endpoint-a-vectors.json
```

Details, options and measured results: `endpoint-python/README.md`.

## Endpoint B (Earth, C)

`endpoint-c/` is the PBS endpoint at Mission Control. It runs on the EARTH
ION node and opens `ipn:23.1` with ION's `bp_open()`, `bp_send()` and
`bp_receive()`. It is written in C11 from the PBS v1.5.0 specification text and
`docs/SPEC-DIGEST.md`, without PBS_LINK, and uses OpenSSL libcrypto for
Ed25519, HMAC-SHA-256 and SHA-256. `pbs_earth` sends telemetry requests and
commands signed with the TEST KEYS from a JSON-lines plan. The plan can also
inject tampered, replayed, wrongly signed, unauthorized and expired commands.
`pbs_earth` validates every envelope it receives (CRC32, length, expiry,
authentication where present) and writes JSON lines in the format of
`docs/LOG-SCHEMA.md`. `pbs_tool` builds, parses and crosschecks envelopes
without ION.

```sh
make -C endpoint-c && make -C endpoint-c test     # 757 unit checks on docs/test-vectors.json and negative cases
endpoint-c/tests/ion_smoke.sh                     # three ION nodes, ION tools on the rover side
endpoint-c/tests/ion_interop.sh                   # three ION nodes, Endpoint A on the rover
ION_NODE_WDNAME="$ION_RUN_DIR/earth" endpoint-c/build/pbs_earth run \
  --plan endpoint-c/examples/s6_auth.jsonl --scenario S6 --duration 40 --log endpoint-b.jsonl
```

Details, plan format, options and measured results: `endpoint-c/README.md`.

## Running the demo

```sh
./run_demo.sh            # build, self-checks, scenarios S1 to S7, results/ and REPORT.md
./run_demo.sh S4         # one scenario; other results/<S>/ directories are kept
```

`run_demo.sh` builds ION at the pinned commit into `build/ion` (skipped when
already built), creates `build/venv-demo` (cryptography, pytest, matplotlib),
fetches PBS_LINK, builds Endpoint B into `build/endpoint-c`, runs the
emulator, scenario-definition and endpoint unit tests, then runs each
scenario: the link emulator (`emulator/`), the three ION nodes (`ion/`),
Endpoint A on the rover node and Endpoint B on the Earth node, stopped
cleanly after the traffic phase. `scenarios/evaluate.py` applies each
scenario's pass/fail criteria and `report/make_report.py` writes
`REPORT.md`, `results/summary.json`, `results/all-envelopes.csv` and charts
in `results/charts/`. Exit status 0 means every self-check and every
scenario passed. Prerequisites (Ubuntu): `git autoconf automake libtool gcc
make python3 python3-venv libssl-dev procps`, and network access to GitHub
(ION, PBS_LINK) and PyPI on the first run.

| Scenario | Shows |
|---|---|
| S1 | nominal telemetry (5 priorities) and signed commands both ways |
| S2 | scheduled outages in the contact plan: envelopes held at the rover, the Gateway and Earth, delivered after the contact resumes |
| S3 | 5 % random loss and unscheduled blackouts: LTP retransmission delivers every envelope |
| S4 | rate-limited rover link: CRITICAL, HIGH and NORMAL overtake a BULK backlog |
| S5 | expiry: short-TTL envelopes lost to bundle lifetime during an outage, expired envelopes rejected by receivers |
| S6 | authenticated commands: tampered, replayed, wrong-key, unknown-key, expired, unauthorized commands rejected |
| S7 | cross-implementation: identical bytes for identical inputs, each endpoint parses the other's envelopes |

`results/` and `REPORT.md` in this repository are one recorded run: a fresh
clone of commit `88747a8` on 2026-10-07, 861 s including the ION build, all
seven scenarios passed. `scenarios/README.md` gives the headline numbers and
what was observed about ION across six runs.

Container and CI: `Dockerfile` (`docker build -t pbs-ion-demo . && docker
run --rm --shm-size=256m -v "$PWD/docker-results:/demo/results"
pbs-ion-demo`) and `.github/workflows/demo.yml` run the same script and keep
`results/`. Neither has been run yet: no Docker daemon was available on the
host of the recorded run, and the GitHub repository does not exist yet.
Both call the same `./run_demo.sh` that produced the recorded run. Details:
`scenarios/README.md`, `emulator/README.md`, `report/README.md`.
