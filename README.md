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
