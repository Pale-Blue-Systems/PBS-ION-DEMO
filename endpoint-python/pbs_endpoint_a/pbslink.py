"""Import PBS_LINK 0.1.4 and check that it is the pinned release.

Search order for the package:

1. ``$PBS_LINK_DIR`` (a directory that contains ``PBS_LINK/``);
2. ``<repository>/build/pbs-link`` (written by ``endpoint-python/fetch_pbs_link.sh``);
3. a ``PBS_LINK`` package already importable (for example installed with pip).

The SHA-256 of ``PBS_LINK/__init__.py`` and ``PBS_LINK/core.py`` must match
the pinned release; anything else raises ImportError.
"""

import hashlib
import importlib
import os
import pathlib
import sys

VERSION = "0.1.4"
REPO_URL = "https://github.com/Pale-Blue-Systems/PBS_LINK.git"
COMMIT = "d3b81d32f91c10d30b5a834b7fb64a353285fd27"
PACKAGE_TREE = "fa7b29276da8cb406b3aff6f93f038c4e8386798"
FILE_SHA256 = {
    "__init__.py": "5cd058eb3a33ff13293027242460bd5d95dcecdb46c87e3ee889d0b446c3cfef",
    "core.py": "8862a4685cd8cac2b2ba100c197e042a61aa5e04c430405894094891df038185",
}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO_ROOT / "build" / "pbs-link"


def _candidates():
    env = os.environ.get("PBS_LINK_DIR")
    if env:
        yield pathlib.Path(env)
    yield DEFAULT_DIR


def _sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def verify(module):
    """Raise ImportError unless ``module`` is the pinned PBS_LINK release."""
    if getattr(module, "__version__", None) != VERSION:
        raise ImportError("PBS_LINK %s found at %s; %s is required"
                          % (getattr(module, "__version__", "?"), module.__file__, VERSION))
    pkg = pathlib.Path(module.__file__).resolve().parent
    for name, want in FILE_SHA256.items():
        got = _sha256(pkg / name)
        if got != want:
            raise ImportError("PBS_LINK/%s at %s has SHA-256 %s, expected %s (pinned commit %s)"
                              % (name, pkg, got, want, COMMIT))
    return module


def load():
    """Return the verified PBS_LINK module."""
    if "PBS_LINK" not in sys.modules:
        for d in _candidates():
            if (d / "PBS_LINK" / "__init__.py").is_file():
                sys.path.insert(0, str(d))
                break
    try:
        module = importlib.import_module("PBS_LINK")
    except ImportError as ex:
        raise ImportError("PBS_LINK %s not found; run endpoint-python/fetch_pbs_link.sh or set "
                          "PBS_LINK_DIR (%s)" % (VERSION, ex)) from ex
    return verify(module)


PBS_LINK = load()
