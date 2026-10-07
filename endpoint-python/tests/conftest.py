import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(ENDPOINT_DIR)
if ENDPOINT_DIR not in sys.path:
    sys.path.insert(0, ENDPOINT_DIR)

import pytest  # noqa: E402

from pbs_endpoint_a import conformance  # noqa: E402


@pytest.fixture(scope="session")
def vectors():
    return conformance.load_vectors()


@pytest.fixture(scope="session")
def messages(vectors):
    return {m["id"]: bytes.fromhex(m["envelope_hex"]) for m in vectors["sec_b"]["messages"]}


@pytest.fixture
def rover_rx():
    return conformance.rover_receiver()
