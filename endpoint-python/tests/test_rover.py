"""Rover send/receive logic with an in-memory transport in place of ION."""

import json
import queue
import threading

import pytest

from pbs_endpoint_a import envelope as E
from pbs_endpoint_a import ion, jsonlog, plan, secb
from pbs_endpoint_a.rover import Rover

T = 1767225600000000
SEC = 1_000_000


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.inbox = queue.Queue()
        self.fail = None

    def send(self, dest, payload, lifespan_s, cos):
        if self.fail:
            raise self.fail
        self.sent.append((dest, bytes(payload), lifespan_s, cos))
        return 1

    def receive(self):
        return self.inbox.get()

    def interrupt(self):
        self.inbox.put(ion.Delivery(ion.BpReceptionInterrupted, E.now_us()))


class Clock:
    """Fixed clock; with ``step`` each reading advances by that many microseconds."""

    def __init__(self, t, step=0):
        self.t = t
        self.step = step

    def __call__(self):
        t = self.t
        self.t += self.step
        return t


def delivery(payload, t_us, src="ipn:23.1", creation_us=None):
    creation_us = creation_us if creation_us is not None else t_us - 1_550_000
    return ion.Delivery(ion.BpPayloadPresent, t_us, src, creation_us // 1000 - E.DTN_EPOCH_OFFSET_MS,
                        0, 28000, False, payload)


@pytest.fixture
def setup(tmp_path, rover_rx):
    path = tmp_path / "rover.jsonl"
    log = jsonlog.JsonlLog(str(path), "A", "ipn:99.1", "TEST")
    tr = FakeTransport()
    clock = Clock(T)
    rover = Rover(tr, log, rover_rx, clock=clock)

    def records():
        log.close()
        return [json.loads(line) for line in open(path)]
    return rover, tr, clock, records


def test_nominal_sends(setup):
    rover, tr, clock, records = setup
    for item in plan.expand(plan.nominal()):
        rover.send_planned(item)
    recs = records()
    assert [r["sequence"] for r in recs] == [1, 2, 3, 4, 5]
    assert [r["class_of_service"] for r in recs] == [2, 2, 1, 0, 0]
    assert [r["lifespan_s"] for r in recs] == [2147483647, 59, 59, 3599, 86399]
    assert all(r["result"] == "SENT" and r["lifespan_rule"] == "DTN-MAP-01-6.1" for r in recs)
    assert [cos for _, _, _, cos in tr.sent] == [2, 2, 1, 0, 0]
    for r, (dest, env, lifespan, _) in zip(recs, tr.sent):
        assert dest == "ipn:23.1" and env.hex() == r["envelope_hex"] and lifespan == r["lifespan_s"]
        assert E.check_envelope(env, T)[0] == "OK"
        assert r["source_id"] == "Rover-Alpha" and r["timestamp_us"] == T
        assert r["frame_types"] == ([4] if r["priority"] == 0 else [1])
        assert r["bp_send_return_us"] == T


def test_not_sent_outcomes(setup):
    rover, tr, clock, records = setup
    clock.step = 1                                        # hand-over 1 us after Timestamp
    rover.send(2, b"", 1)                                 # bound -1 ms -> GW_EXPIRED
    rover.send(2, b"", 2)                                 # bound 999 ms -> under 1 s
    rover.send(2, b"", 5, lifespan_override_s=60)         # fault injection
    tr.fail = ion.ZcoError("full")
    rover.send(2, b"", 60)
    tr.fail = ion.BpSendError(0)
    rover.send(2, b"", 60)
    recs = records()
    got = [(r["result"], r["reason"], r["lifespan_s"], r["lifespan_rule"]) for r in recs]
    assert got == [("NOT_SENT", "GW_EXPIRED", None, "DTN-MAP-01-6.1"),
                   ("NOT_SENT", "GW_LIFETIME_UNDER_1S", None, "DTN-MAP-01-6.1"),
                   ("SENT", "OK", 60, "OVERRIDE"),
                   ("NOT_SENT", "ZCO_FAILED", None, "DTN-MAP-01-6.1"),
                   ("NOT_SENT", "BP_SEND_FAILED", 58, "DTN-MAP-01-6.1")]
    assert recs[0]["bp_send_return_us"] is None and recs[3]["bp_send_return_us"] is None
    assert [r["sequence"] for r in recs] == [1, 2, 3, 4, 5]


def test_commands(setup, messages):
    rover, tr, clock, records = setup
    now = T + 2 * SEC
    rover.on_delivery(delivery(messages["CMD-01"], now))          # accept, exec
    rover.on_delivery(delivery(messages["CMD-01"], now + 1))      # replay
    rover.on_delivery(delivery(messages["CMD-03"], now))          # tampered command byte
    rover.on_delivery(delivery(messages["CMD-07"], now))          # wrong key
    denied = secb.build_command(E.EnvelopeBuilder("Earth-MCC"), "DRIVE 10",
                                secb.test_signing_key("TEST-ED25519-01"), 2,
                                role=b"SCIENCE-OPS", timestamp_us=T)
    rover.on_delivery(delivery(denied, now))                      # role not permitted
    rover.on_delivery(delivery(messages["CMD-02"], T + 31 * SEC))  # expired
    rover.on_delivery(delivery(b"\x10\x00", now, src="ipn:50.1"))  # not an envelope
    recs = records()
    recv = [r for r in recs if r["event"] == "recv"]
    assert [(r["result"], r["reason"]) for r in recv] == [
        ("ACCEPT", "OK"), ("REJECT", "SEC_REPLAY"), ("REJECT", "SEC_AUTH_FAIL"),
        ("REJECT", "SEC_AUTH_FAIL"), ("REJECT", "AUTHZ_DENIED"), ("REJECT", "TTL_EXPIRED"),
        ("REJECT", "LENGTH")]
    ok = recv[0]
    assert ok["commands"] == ["DRIVE 10"] and ok["suite_id"] == "0xFF02"
    assert ok["key_id"] == "TEST-ED25519-01" and ok["security_sequence"] == 1
    assert ok["authority_id"] == "PBS-DEMO-MCC" and ok["role"] == "FLIGHT-OPS"
    assert ok["policy_epoch"] == 1 and ok["frame_types"] == [8, 2, 10]
    assert ok["expected_eid"] == "ipn:23.1" and ok["eid_match"] is True
    assert ok["latency_us"] == 2 * SEC
    assert ok["bundle_creation_us"] == now - 1_550_000
    assert recv[4]["role"] == "SCIENCE-OPS" and recv[4]["commands"] is None
    assert recv[5]["suite_id"] is None and recv[5]["frame_types"] is None
    assert recv[6]["source_id"] is None and recv[6]["eid_match"] is None
    execs = [r for r in recs if r["event"] == "exec"]
    assert len(execs) == 1
    assert execs[0]["command"] == "DRIVE 10" and execs[0]["operation"] == "DRIVE"
    assert execs[0]["action"] == "LOGGED" and execs[0]["sequence"] == ok["sequence"]
    assert tr.sent == []


def test_ping_reply(tmp_path, rover_rx, messages):
    log = jsonlog.JsonlLog(str(tmp_path / "r.jsonl"), "A", "ipn:99.1", None)
    tr = FakeTransport()
    rover = Rover(tr, log, rover_rx, clock=Clock(T + 2 * SEC), reply_ping=True)
    rover.on_delivery(delivery(messages["CMD-16"], T + 2 * SEC))  # PING with frame 0x80
    log.close()
    recs = [json.loads(line) for line in open(tmp_path / "r.jsonl")]
    assert [r["event"] for r in recs] == ["recv", "exec", "send"]
    assert recs[0]["frame_types"] == [8, 128, 2, 10]
    assert recs[2]["label"] == "PING-REPLY" and recs[2]["priority"] == 1
    assert len(tr.sent) == 1 and b"PONG" in tr.sent[0][1]


def test_run_loop_sends_on_schedule_and_receives(tmp_path, rover_rx, messages):
    log = jsonlog.JsonlLog(str(tmp_path / "r.jsonl"), "A", "ipn:99.1", None)
    tr = FakeTransport()
    rover = Rover(tr, log, rover_rx)
    sends = plan.expand({"items": [{"priority": p, "at_s": 0.05 * p} for p in range(5)]})
    stop = threading.Event()
    rover.start_receiving()
    cmd = E.EnvelopeBuilder("Earth-MCC")
    env = secb.build_command(cmd, "PING", secb.test_signing_key("TEST-ED25519-01"), 1)
    tr.inbox.put(delivery(env, E.now_us()))
    rover.run(sends, stop, duration_s=0.5)
    rover.stop_receiving()
    log.close()
    recs = [json.loads(line) for line in open(tmp_path / "r.jsonl")]
    assert [r["priority"] for r in recs if r["event"] == "send"] == [0, 1, 2, 3, 4]
    assert [r["reason"] for r in recs if r["event"] == "recv"] == ["OK"]
    assert not rover._rx_thread.is_alive()


def test_stop_event_ends_run(tmp_path, rover_rx):
    log = jsonlog.JsonlLog(str(tmp_path / "r.jsonl"), "A", "ipn:99.1", None)
    rover = Rover(FakeTransport(), log, rover_rx)
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    sends = plan.expand({"items": [{"priority": 2, "at_s": 30}]})
    rover.run(sends, stop, duration_s=None)
    log.close()
    assert open(tmp_path / "r.jsonl").read() == ""
