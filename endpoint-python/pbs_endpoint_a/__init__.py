"""Endpoint A of the PBS ION demo: the rover-side PBS endpoint (Python).

Envelopes are built and parsed with PBS_LINK 0.1.4. PBS-MUX-01 containers,
PBS-AUTH-01 Authority Context and PBS-SEC-B-01 authentication are
implemented here from docs/SPEC-DIGEST.md, because PBS_LINK 0.1.4 does not
implement them. The rover program talks to its ION node through a ctypes
binding to ION's libbp (pbs_endpoint_a.ion).
"""

__version__ = "0.1.0"
IMPL = "endpoint-python " + __version__
