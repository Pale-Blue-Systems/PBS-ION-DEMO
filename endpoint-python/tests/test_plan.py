import pytest

from pbs_endpoint_a import plan
from pbs_endpoint_a.mux import parse_container


def test_nominal_plan_has_five_priorities():
    sends = plan.expand(plan.nominal(count=2, interval_s=3.0))
    assert len(sends) == 10
    assert sorted({s.priority for s in sends}) == [0, 1, 2, 3, 4]
    assert [s.ttl_s for s in sends[:5]] == [0, 60, 60, 3600, 86400]
    assert sends[5].at_s == pytest.approx(3.0)


def test_payload_bytes_sets_size_exactly():
    sends = plan.expand({"items": [{"priority": 4, "payload_bytes": 4000, "count": 2}]})
    for i, s in enumerate(sends, 1):
        assert len(s.payload) == 4000
        frames, pad = parse_container(s.payload)
        assert pad == 0 and frames[0].value.startswith(b"TLM IMAGE-CHUNK N=%d" % i)


def test_header_only_and_overrides():
    s = plan.expand({"items": [{"priority": 3, "payload_bytes": 0, "ttl_s": 5,
                                "lifespan_override_s": 60, "ack_req": True, "label": "X"}]})[0]
    assert s.payload == b"" and s.frame_types == () and s.ttl_s == 5
    assert s.lifespan_override_s == 60 and s.ack_req and s.label == "X"


def test_plan_errors():
    with pytest.raises(ValueError):
        plan.expand({"items": [{"priority": 5}]})
    with pytest.raises(ValueError):
        plan.expand({"items": [{"priority": 1, "colour": "red"}]})
    with pytest.raises(ValueError):
        plan.expand({"items": [{"priority": 1, "payload_bytes": 70000}]})
    with pytest.raises(ValueError):
        plan.expand({"items": [{"priority": 1, "frame": "command"}]})


def test_items_ordered_by_time():
    sends = plan.expand({"items": [{"priority": 4, "at_s": 2.0}, {"priority": 0, "at_s": 1.0}]})
    assert [s.priority for s in sends] == [0, 4]


def test_builtin_specs():
    assert plan.load("builtin:none") == {"items": []}
    assert len(plan.expand(plan.load("builtin:nominal:3:1"))) == 15


def test_example_plan_file():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "plans", "example.json")
    sends = plan.expand(plan.load(path))
    assert len(sends) == 18 and sorted({s.priority for s in sends}) == [0, 1, 2, 3, 4]


def test_extra_frames_before_the_main_frame():
    s = plan.expand({"items": [{"priority": 2, "text": "TLM X",
                                "extra_frames": [{"type": "0x80", "hex": "00ff00ff"}]}]})[0]
    frames, pad = parse_container(s.payload)
    assert [(f.type, f.value) for f in frames] == [(0x80, b"\x00\xff\x00\xff"), (1, b"TLM X N=1")]
    assert pad == 0 and s.frame_types == (0x80, 1)
    with pytest.raises(ValueError):
        plan.expand({"items": [{"priority": 2, "payload_bytes": 100,
                                "extra_frames": [{"type": "0x80", "hex": "00"}]}]})
