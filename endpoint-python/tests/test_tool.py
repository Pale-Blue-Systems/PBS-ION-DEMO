"""tool crosscheck: exit status 0 only when every peer send record was decoded
and, with --own-log, every own envelope was delivered byte-identical."""

import json

from pbs_endpoint_a import envelope as E
from pbs_endpoint_a import jsonlog, tool
from pbs_endpoint_a.mux import build_container

T = 1767225600000000


def _send(seq):
    env = E.build_envelope("Earth-MCC", 2, build_container([(0x01, b"TLM %d" % seq)]), 60, seq, T)
    return dict(event="send", result="SENT", **jsonlog.envelope_fields(env, frame_types=[1]))


def _recv(rec):
    keys = ("source_id_hex", "sequence", "timestamp_us", "envelope_sha256", "header_hex")
    return dict({k: rec[k] for k in keys}, event="recv")


def _write(path, recs):
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return str(path)


def test_crosscheck_requires_every_record_decoded(tmp_path):
    sends = [_send(i) for i in range(1, 5)]
    out = tmp_path / "rep.json"
    assert tool.main(["crosscheck", _write(tmp_path / "peer.jsonl", sends), "--out", str(out)]) == 0
    assert json.load(open(out))["decoded"] == 4
    sends[2]["envelope_hex"] = None
    assert tool.main(["crosscheck", _write(tmp_path / "peer2.jsonl", sends), "--out", str(out)]) == 1
    rep = json.load(open(out))
    assert (rep["peer_send_records"], rep["decoded"], rep["no_hex"], rep["ok"]) == (4, 3, 1, False)


def test_crosscheck_requires_every_own_envelope_delivered(tmp_path):
    own = [_send(i) for i in range(1, 5)]
    own_log = _write(tmp_path / "own.jsonl", own)
    peer_all = _write(tmp_path / "peer_all.jsonl", [_recv(r) for r in own])
    peer_three = _write(tmp_path / "peer_three.jsonl", [_recv(r) for r in own[:3]])
    out = tmp_path / "rep.json"
    assert tool.main(["crosscheck", peer_all, "--own-log", own_log, "--out", str(out)]) == 0
    assert tool.main(["crosscheck", peer_three, "--own-log", own_log, "--out", str(out)]) == 1
    rep = json.load(open(out))
    assert (rep["own_sent"], rep["delivered"], rep["byte_identical"], rep["ok"]) == (4, 3, 3, False)
