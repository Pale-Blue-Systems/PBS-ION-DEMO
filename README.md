# PBS ION demo

Two independently written PBS endpoints exchange PBS v1.5.0 envelopes over
three NASA/JPL ION nodes (rover `ipn:99`, LUNAR GATEWAY relay `ipn:50`,
Earth `ipn:23`; BPv7 over LTP over UDP), through a user-space link emulator
that adds delay, loss, rate limits and blackouts. Seven automated scenarios
measure delivery, latency, storage across outages, expiry and command
authentication, and `./run_demo.sh` reproduces all of them.

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
code, requires a Security frame on every Earth-MCC envelope that has a
payload (`docs/SPEC-DIGEST.md` A38), and writes JSON lines in the format of
`docs/LOG-SCHEMA.md`.

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
Ed25519, HMAC-SHA-256 and SHA-256. `pbs_earth` sends commands and telemetry
signed with the TEST KEYS from a JSON-lines plan. The plan can also inject
tampered, replayed, wrongly signed, unauthorized and expired commands.
`pbs_earth` validates every envelope it receives (CRC32, length, expiry,
authentication where present) and writes JSON lines in the format of
`docs/LOG-SCHEMA.md`. `pbs_tool` builds, parses and crosschecks envelopes
without ION.

In the scenarios, signed envelopes travel only from Earth to the rover: the
rover signs nothing. In the scenarios Endpoint B's SEC-B-01 verification is
therefore exercised only offline, on the 34 SEC-B deliveries of
`docs/test-vectors.json` (S7-1); outside them, its unit tests and
`endpoint-c/tests/ion_smoke.sh`, which delivers one signed envelope to it
through ION, exercise it.

```sh
make -C endpoint-c && make -C endpoint-c test     # 812 unit checks on docs/test-vectors.json and negative cases
endpoint-c/tests/ion_smoke.sh                     # three ION nodes, ION tools on the rover side
endpoint-c/tests/ion_interop.sh                   # three ION nodes, Endpoint A on the rover
ION_NODE_WDNAME="$ION_RUN_DIR/earth" endpoint-c/build/pbs_earth run \
  --plan endpoint-c/examples/s6_auth.jsonl --scenario S6 --duration 40 --log endpoint-b.jsonl
```

Details, plan format, options and measured results: `endpoint-c/README.md`.

## How independent the two endpoints are

Endpoint A and Endpoint B are separate code bases in different languages,
with different cryptographic libraries and CRC implementations. Endpoint A
calls no C code, and Endpoint B's author did not open PBS_LINK or Endpoint
A's protocol modules (`endpoint-c/README.md`). Both endpoints implement one
interpretation document, `docs/SPEC-DIGEST.md`, which fixes the receive
steps R1 to R14, every reason code and the demo choices A1 to A38. The
digest was written, and `docs/test-vectors.json` generated, by a separate
step that ran PBS_LINK 0.1.4; the expected results in the vectors come from
the reference receiver in `docs/gen_test_vectors.py`, written with Endpoint
A. The endpoints therefore agree on those choices by construction. S7 tests
two code bases against that shared interpretation; it does not show that two
independent readings of PBS v1.5.0 agree.

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
Endpoint A on the rover node and Endpoint B on the Earth node. After each
traffic phase the runner waits until every envelope handed to ION has been
received or its bundle lifetime has ended (at most 30 s), then stops the
endpoints and the nodes cleanly. `scenarios/evaluate.py` applies each
scenario's pass/fail criteria and `report/make_report.py` writes
`REPORT.md`, `results/summary.json`, `results/all-envelopes.csv` and charts
in `results/charts/`. A full run removes only what an earlier run wrote in
`results/`. Exit status 0 means every self-check and every scenario passed.

Prerequisites (Ubuntu): `git autoconf automake libtool pkgconf gcc make
python3 python3-venv libssl-dev procps`, and network access to GitHub (ION,
PBS_LINK) and PyPI on the first run. `pkgconf` provides `pkg.m4`, which
`autoreconf` needs for ION's `configure.ac`; `run_demo.sh` checks for it.

| Scenario | Shows |
|---|---|
| S1 | telemetry at 5 priorities rover to Earth; signed commands, signed MCC telemetry and a TTL 0 heartbeat Earth to rover; PING replies |
| S2 | scheduled outages in the contact plan: envelopes held at the rover, the Gateway and Earth, delivered after the contact resumes |
| S3 | 5 % random loss and unscheduled blackouts: LTP retransmission delivers every envelope |
| S4 | rate-limited rover link: CRITICAL, HIGH and NORMAL overtake a BULK backlog; LOW waits with BULK |
| S5 | expiry: short-TTL envelopes lost to bundle lifetime during an outage, expired envelopes rejected by receivers |
| S6 | authenticated commands: tampered (including a lowered MUX `frame_count`), replayed, wrong-key, unknown-key, expired, unauthorized-role and unknown-authority commands rejected |
| S7 | cross-implementation: identical bytes for identical inputs; each endpoint decodes every envelope the other logged and accepts every envelope the other sent |

The mapping of PBS priority to ION class of service, ION's queue treatment,
behaviour under congestion and when the treatment is unavailable, and the
no-expiry bundle lifetime are documented in `docs/MAPPING-PROFILE.md`
(PBS-DTN-MAP-01 6.3, PBS-DTN-MAP-02 Sections 4 and 5).

## Recorded runs

`results/` and `REPORT.md` are run F: `./run_demo.sh` in a fresh clone of
commit `25ffe97` on 2026-10-07 (started 07:20:27 UTC, 866 s including
cloning and building ION). `runs/run-G/` is run G, a second fresh-clone run
of the same commit (07:35:04 UTC, 869 s). Both passed all 86 required
criteria of the seven scenarios. REPORT.md's Repeatability section compares
the two criterion by criterion; it was written with
`report/make_report.py --name "run F" --compare "run G=runs/run-G"`, and
`./run_demo.sh` alone writes REPORT.md without that section
(`report/README.md`).

`runs/` holds recorded runs that `run_demo.sh` never touches: `runs/run-G/`,
`runs/run-D/` (a run of the earlier commit `7095caa`) and `runs/control/`
(two S2 runs of `7095caa` with ION's default LTP delivery backlog limit,
`ion/README.md` item 15). `scenarios/README.md` lists every run made, the
headline numbers and what was observed about ION; `docs/CRITERIA-CHANGES.md`
lists every change to a pass/fail criterion made after a run.

## Container and CI

`Dockerfile` (`docker build -t pbs-ion-demo . && docker run --rm
--shm-size=256m -v "$PWD/docker-results:/demo/results" pbs-ion-demo`) and
`.github/workflows/demo.yml` run the same `./run_demo.sh` and keep
`results/`. Both install `pkgconf`.

No Docker daemon is available on the host of the recorded runs, so the
image was emulated on 2026-10-07 (07:50 UTC, commit `25ffe97`): a chroot of
`ubuntu-base-24.04.5-base-amd64` (SHA-256 checked) run with private mount,
IPC and PID namespaces and a 256 MB `/dev/shm`; the Dockerfile's apt
package list installed with `--no-install-recommends` (199 packages,
`pkg.m4` present); the build context copied from a clone with the
`.dockerignore` exclusions. The Dockerfile's `RUN` step passed in 133 s
(ION built in 104 s), and its `CMD`, `./run_demo.sh`, passed in 635 s: 4 of
4 self-checks and all 86 required criteria of S1 to S7, with Python 3.12.3.
The only change to the image setup was network access through the host's
HTTPS proxy (apt over https with the proxy's CA file). That run's
`run.json` records a modified tree, because the image leaves out the
tracked `runs/` directory; `scenarios/runner.py` now leaves `runs/` out of
that check. The GitHub workflow has not been run: the GitHub repository
does not exist yet. Details: `scenarios/README.md`, `emulator/README.md`,
`report/README.md`.

## License

Apache-2.0, see `LICENSE`.
