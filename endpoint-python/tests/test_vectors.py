"""Every check of docs/test-vectors.json, one pytest case each.

Envelopes ENV-01..ENV-15 are built from their inputs with PBS_LINK and
compared byte for byte, then parsed; receiver cases RX-01..RX-22, lifetime
cases LT-01..LT-10, the class-of-service table, SEC-B messages CMD-* (built
from inputs, auth_input and auth_data compared) and SEC-B cases SB-01..SB-21
are run through Endpoint A's code with the rover's provisioned keys.
"""

import pytest

from pbs_endpoint_a import conformance

RESULTS, PRODUCED = conformance.run()


@pytest.mark.parametrize("result", RESULTS, ids=["%s:%s" % (r["id"], r["check"]) for r in RESULTS])
def test_vector(result):
    assert result["ok"], result


def test_vector_coverage(vectors):
    ids = {r["id"].split("/")[0] for r in RESULTS}
    for vec in vectors["envelopes"] + vectors["receiver_cases"] + vectors["lifetime_cases"]:
        assert vec["id"] in ids
    for m in vectors["sec_b"]["messages"]:
        assert m["id"] in ids
    for c in vectors["sec_b"]["cases"]:
        assert c["id"] in ids
    deliveries = sum(len(c["deliveries"]) for c in vectors["sec_b"]["cases"])
    assert sum(1 for r in RESULTS if r["id"].startswith("SB-")) == deliveries == 30
    # every message with inputs was rebuilt byte for byte
    with_inputs = [m["id"] for m in vectors["sec_b"]["messages"] if "inputs" in m]
    assert sorted(with_inputs) == sorted(k for k in PRODUCED if k.startswith("CMD-"))
    assert len(with_inputs) == 19


def test_rx_order_differs_from_pbs_link(vectors):
    """RX-15: PBS_LINK 0.1.4 parse() accepts a trailing byte; Endpoint A rejects it (A8)."""
    rx15 = [c for c in vectors["receiver_cases"] if c["id"] == "RX-15"][0]
    assert rx15["pbs_link_0_1_4_parse"].startswith("parsed")
    assert rx15["expected"]["reason"] == "LENGTH"
    assert any(r["id"] == "RX-15" and r["ok"] for r in RESULTS)
