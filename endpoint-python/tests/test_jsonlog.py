"""The log writer and docs/LOG-SCHEMA.md list the same fields."""

import json
import os
import re

import pytest

from pbs_endpoint_a import jsonlog

from conftest import REPO_ROOT

SCHEMA_DOC = os.path.join(REPO_ROOT, "docs", "LOG-SCHEMA.md")


def doc_tables():
    """{section key: [field names]} from the first column of each table."""
    out, key = {}, None
    for line in open(SCHEMA_DOC, encoding="utf-8"):
        m = re.match(r"## (\d+)\.", line)
        if m:
            key = {"3": "common", "4": "envelope", "5": "send", "6": "recv", "7": "exec",
                   "8": "run_start"}.get(m.group(1))
            continue
        if line.startswith("`run_end`:"):
            key = "run_end"
            continue
        if key and line.startswith("| `"):
            first = line.split("|")[1]
            out.setdefault(key, []).extend(re.findall(r"`([a-z_0-9]+)`", first))
    return out


def test_doc_and_code_list_the_same_fields():
    doc = doc_tables()
    assert tuple(doc["common"]) == jsonlog.COMMON_FIELDS
    assert tuple(doc["envelope"]) == jsonlog.ENVELOPE_FIELDS
    for event in ("send", "recv"):
        assert tuple(doc[event]) == jsonlog.EVENT_FIELDS[event][len(jsonlog.ENVELOPE_FIELDS):]
    for event in ("exec", "run_start", "run_end"):
        assert tuple(doc[event]) == jsonlog.EVENT_FIELDS[event], event


def test_writer_fills_listed_fields_and_refuses_unknown(tmp_path):
    log = jsonlog.JsonlLog(str(tmp_path / "a.jsonl"), "A", "ipn:99.1", "S0")
    log.write("exec", 5, command="PING", x_note="extra fields need the x_ prefix")
    with pytest.raises(KeyError):
        log.write("exec", 6, colour="red")
    log.close()
    rec = json.loads(open(tmp_path / "a.jsonl").readline())
    assert rec["schema"] == "pbs-ion-demo-log/1" and rec["scenario"] == "S0"
    assert set(rec) == set(jsonlog.COMMON_FIELDS) | set(jsonlog.EVENT_FIELDS["exec"]) | {"x_note"}
    assert rec["role"] is None and rec["command"] == "PING"


def test_envelope_fields_hex_limit():
    small = jsonlog.envelope_fields(b"\x10" + b"\0" * 43)
    assert small["envelope_hex"] == "10" + "00" * 43 and small["envelope_len"] == 44
    big = jsonlog.envelope_fields(b"\x10" + b"\0" * 5000)
    assert big["envelope_hex"] is None and len(big["envelope_sha256"]) == 64
    short = jsonlog.envelope_fields(b"\x10\x00")
    assert short["header_hex"] is None and short["sequence"] is None


def test_hex_limit_option_and_latency_range():
    data = b"\x10" + b"\0" * 5000
    try:
        jsonlog.set_hex_max(65580)
        assert jsonlog.envelope_fields(data)["envelope_hex"] == data.hex()
    finally:
        jsonlog.set_hex_max(jsonlog.ENVELOPE_HEX_MAX)
    assert jsonlog.envelope_fields(data)["envelope_hex"] is None
    t = 1767225600000000
    assert jsonlog.latency_us(t, t - 5) == 5
    assert jsonlog.latency_us(t, 1 << 63) == t - (1 << 63)
    assert jsonlog.latency_us(t, (1 << 64) - 1) is None
