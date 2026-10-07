"""Endpoint A over the three ION nodes (opt-in: PBS_ION_LIVE=1, about 50 s).

Starts ion/udp_forward.py (fixed delays 0.25 s rover<->gateway, 1.3 s
gateway<->earth, no loss) and ion/start_nodes.sh with the always-on contact
plan, runs the rover program on the ROVER node, and uses the Endpoint A test
harness (pbs_endpoint_a.harness, not Endpoint B) on the EARTH node to
receive telemetry and to inject valid, tampered, wrongly signed, unknown-key,
unauthorized, expired and replayed commands. Then stops everything and
checks the logs.

Only one ION instance can run per host (fixed IPC keys and ports); the test
fails at start_nodes.sh if another one is running. PBS_ION_LIVE_OUT sets
the output directory (default: a pytest temporary directory); the summary
is written to <out>/live-summary.json.
"""

import collections
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import ENDPOINT_DIR, REPO_ROOT

pytestmark = pytest.mark.skipif(os.environ.get("PBS_ION_LIVE") != "1",
                                reason="set PBS_ION_LIVE=1 to run against three ION nodes")

ION = os.path.join(REPO_ROOT, "ion")
BIN = os.path.join(ENDPOINT_DIR, "bin")
LINKS = ["rover-gateway:29950:15013:0.25", "gateway-rover:25099:19913:0.25",
         "gateway-earth:25023:12313:1.3", "earth-gateway:22350:15013:1.3"]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(scope="module")
def nodes(tmp_path_factory):
    out = os.environ.get("PBS_ION_LIVE_OUT") or str(tmp_path_factory.mktemp("live"))
    os.makedirs(out, exist_ok=True)
    env = dict(os.environ)
    env["ION_RUN_DIR"] = os.path.join(out, "ion")
    env["ION_NODE_LIST_DIR"] = env["ION_RUN_DIR"]
    fwd_args = [sys.executable, os.path.join(ION, "udp_forward.py"),
                "--stats", os.path.join(out, "forwarder-stats.json")]
    for link in LINKS:
        fwd_args += ["--link", link]
    fwd = subprocess.Popen(fwd_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.path.join(out, "start.log"), "w") as fh:
        rc = subprocess.run([os.path.join(ION, "start_nodes.sh")], env=env, stdout=fh,
                            stderr=subprocess.STDOUT).returncode
    if rc != 0:
        fwd.terminate()
        pytest.fail("ion/start_nodes.sh failed (another ION instance running?); see %s/start.log"
                    % out)
    try:
        yield env, out
    finally:
        with open(os.path.join(out, "stop.log"), "w") as fh:
            stop_rc = subprocess.run([os.path.join(ION, "stop_nodes.sh")], env=env, stdout=fh,
                                     stderr=subprocess.STDOUT).returncode
        fwd.send_signal(signal.SIGTERM)
        fwd.wait(10)
        assert stop_rc == 0, "stop_nodes.sh left ION state behind"


def inject(env, out, *args):
    r = subprocess.run([os.path.join(BIN, "harness"), "inject", "--log",
                        os.path.join(out, "harness-inject.jsonl"), "--scenario", "LIVE", *args],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    result, reason, hexstr = r.stdout.split()
    assert (result, reason) == ("SENT", "OK")
    return hexstr


def test_rover_over_ion(nodes):
    env, out = nodes
    rover_log = os.path.join(out, "endpoint-a.jsonl")
    earth_log = os.path.join(out, "harness-earth.jsonl")
    ready = os.path.join(out, "rover.ready")
    listen = subprocess.Popen([os.path.join(BIN, "harness"), "listen", "--duration", "18",
                               "--log", earth_log, "--scenario", "LIVE"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    rover = subprocess.Popen([os.path.join(BIN, "rover"), "--log", rover_log, "--scenario", "LIVE",
                              "--plan", "builtin:nominal:2:2", "--duration", "15",
                              "--reply-ping", "--ready-file", ready], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if os.path.exists(ready):
            break
        time.sleep(0.1)
    assert os.path.exists(ready), "rover did not open ipn:99.1"

    t_inject = time.time()
    ping = inject(env, out, "--command", "PING", "--secseq", "1", "--label", "VALID-ED25519")
    inject(env, out, "--command", "DRIVE 10", "--secseq", "2", "--tamper", "command",
           "--label", "TAMPERED")
    inject(env, out, "--command", "DRIVE 10", "--secseq", "3", "--sign-with",
           "TEST-ED25519-ROGUE", "--label", "WRONG-KEY")
    inject(env, out, "--command", "DRIVE 10", "--secseq", "1", "--key", "TEST-ED25519-ROGUE",
           "--label", "UNKNOWN-KEY")
    inject(env, out, "--command", "SET_HEATER ON", "--key", "TEST-HMAC-01", "--secseq", "1",
           "--label", "VALID-HMAC")
    inject(env, out, "--command", "DRIVE 10", "--secseq", "4", "--role", "SCIENCE-OPS",
           "--label", "UNAUTHORIZED-ROLE")
    inject(env, out, "--command", "DRIVE 5", "--secseq", "5", "--timestamp-offset-s", "-29.5",
           "--lifespan", "30", "--label", "EXPIRED")
    time.sleep(1.0)
    inject(env, out, "--hex", ping, "--label", "REPLAY")
    rover_rc = rover.wait(40)
    listen_rc = listen.wait(40)
    assert rover_rc == 0, rover.stderr.read()
    assert listen_rc == 0, listen.stderr.read()

    a = read(rover_log)
    h = read(earth_log)
    inj = read(os.path.join(out, "harness-inject.jsonl"))
    assert a[0]["event"] == "run_start" and a[-1]["event"] == "run_end"
    assert a[0]["ion_version"] == "ION-OPEN-SOURCE-4.2.0" and a[0]["pbs_link_version"] == "0.1.4"
    sends = [r for r in a if r["event"] == "send"]
    recvs = [r for r in a if r["event"] == "recv"]
    execs = [r for r in a if r["event"] == "exec"]

    # Commands at the rover
    by_label = {}
    for s in inj:
        by_label.setdefault(s["label"], s)
    reasons = collections.Counter(r["reason"] for r in recvs)
    assert reasons == collections.Counter({"OK": 2, "SEC_AUTH_FAIL": 2, "SEC_KEY_UNKNOWN": 1,
                                           "AUTHZ_DENIED": 1, "TTL_EXPIRED": 1,
                                           "SEC_REPLAY": 1})
    assert sorted(e["command"] for e in execs) == ["PING", "SET_HEATER ON"]
    assert all(r["eid_match"] is True and r["bundle_source_eid"] == "ipn:23.1" for r in recvs)
    sha_reason = {r["envelope_sha256"]: r["reason"] for r in recvs if r["reason"] != "SEC_REPLAY"}
    assert sha_reason[by_label["VALID-ED25519"]["envelope_sha256"]] == "OK"
    assert sha_reason[by_label["TAMPERED"]["envelope_sha256"]] == "SEC_AUTH_FAIL"
    assert sha_reason[by_label["WRONG-KEY"]["envelope_sha256"]] == "SEC_AUTH_FAIL"
    assert sha_reason[by_label["UNKNOWN-KEY"]["envelope_sha256"]] == "SEC_KEY_UNKNOWN"
    assert sha_reason[by_label["VALID-HMAC"]["envelope_sha256"]] == "OK"
    assert sha_reason[by_label["UNAUTHORIZED-ROLE"]["envelope_sha256"]] == "AUTHZ_DENIED"
    assert sha_reason[by_label["EXPIRED"]["envelope_sha256"]] == "TTL_EXPIRED"
    replay = [r for r in recvs if r["reason"] == "SEC_REPLAY"][0]
    assert replay["envelope_sha256"] == by_label["REPLAY"]["envelope_sha256"] \
        == by_label["VALID-ED25519"]["envelope_sha256"]

    # Telemetry from the rover at Earth: every envelope delivered, byte-identical (T002)
    assert len(sends) == 11 and all(s["result"] == "SENT" for s in sends)
    assert sorted({s["priority"] for s in sends}) == [0, 1, 2, 3, 4]
    got = {(r["source_id_hex"], r["sequence"], r["timestamp_us"]): r for r in h
           if r["event"] == "recv"}
    for s in sends:
        r = got[(s["source_id_hex"], s["sequence"], s["timestamp_us"])]
        assert r["result"] == "ACCEPT" and r["envelope_sha256"] == s["envelope_sha256"]
        assert r["header_hex"] == s["header_hex"] and r["bundle_source_eid"] == "ipn:99.1"
        if s["ttl_s"] == 0:
            assert r["bundle_ttl_field_ms"] == 4294966296      # 2147483647000 mod 2^32
        else:
            assert r["bundle_ttl_field_ms"] == s["lifespan_s"] * 1000
    lat = sorted(r["latency_us"] for r in h if r["event"] == "recv")
    cmd_lat = sorted(r["latency_us"] for r in recvs if r["reason"] not in ("TTL_EXPIRED",
                                                                           "SEC_REPLAY"))
    summary = {
        "conditions": {"forwarder_delays_s": {"rover-gateway": 0.25, "gateway-earth": 1.3},
                       "loss": 0, "contact_plan": "ion/contacts/always_on.ionrc",
                       "ion_version": a[0]["ion_version"],
                       "pbs_link_version": a[0]["pbs_link_version"]},
        "rover_sends": len(sends),
        "earth_received": len(got), "earth_byte_identical": len(sends),
        "telemetry_latency_us": {"min": lat[0], "median": lat[len(lat) // 2], "max": lat[-1]},
        "command_latency_us": {"min": cmd_lat[0], "max": cmd_lat[-1]},
        "rover_recv_reasons": dict(reasons), "rover_exec": [e["command"] for e in execs],
        "inject_started_unix_s": round(t_inject, 3),
    }
    with open(os.path.join(out, "live-summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    assert 1_550_000 <= lat[0] and lat[-1] < 3_000_000
