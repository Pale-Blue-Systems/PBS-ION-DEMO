"""PBS_LINK pin and ION struct layout."""

import os
import re
import shutil
import subprocess

import pytest

from pbs_endpoint_a import ion, pbslink
from pbs_endpoint_a.pbslink import PBS_LINK

from conftest import ENDPOINT_DIR, REPO_ROOT


def test_pbs_link_is_the_pinned_release():
    assert PBS_LINK.__version__ == "0.1.4"
    pbslink.verify(PBS_LINK)                      # raises on a hash mismatch
    script = open(os.path.join(ENDPOINT_DIR, "fetch_pbs_link.sh")).read()
    assert 'COMMIT="%s"' % pbslink.COMMIT in script
    assert 'SHA_INIT="%s"' % pbslink.FILE_SHA256["__init__.py"] in script
    assert 'SHA_CORE="%s"' % pbslink.FILE_SHA256["core.py"] in script


def test_vectors_were_made_with_this_pbs_link(vectors):
    assert vectors["sources"]["pbs_link"]["version"] == PBS_LINK.__version__


LAYOUT_C = r"""
#include <stdio.h>
#include <stddef.h>
#include "bp.h"
#define F(T, f) printf("%s.%s %zu %zu\n", #T, #f, offsetof(T, f), sizeof(((T *)0)->f))
int main(void) {
  printf("BpDelivery %zu\nBpTimestamp %zu\nZcoReader %zu\n",
         sizeof(BpDelivery), sizeof(BpTimestamp), sizeof(ZcoReader));
  F(BpDelivery, result); F(BpDelivery, bundleSourceEid); F(BpDelivery, bundleCreationTime);
  F(BpDelivery, timeToLive); F(BpDelivery, ackRequested); F(BpDelivery, adminRecord);
  F(BpDelivery, adu); F(BpDelivery, metadataType); F(BpDelivery, metadataLen);
  F(BpDelivery, metadata); F(BpTimestamp, msec); F(BpTimestamp, count);
  F(ZcoReader, zco); F(ZcoReader, trackFileOffset); F(ZcoReader, headersLengthCopied);
  F(ZcoReader, sourceLengthCopied); F(ZcoReader, trailersLengthCopied);
  F(ZcoReader, lengthCopied);
  printf("CONST %d %d %d %d %d %d %d %d\n", BP_POLL, BP_BLOCKING, ZcoSdrSource, ZcoOutbound,
         BpPayloadPresent, BpReceptionTimedOut, BpReceptionInterrupted, BpEndpointStopped);
  return 0;
}
"""


def _ion_cflags():
    pc = os.path.join(str(ion.ion_prefix()), "lib", "pkgconfig", "ion.pc")
    if not os.path.isfile(pc):
        return None
    text = open(pc).read()
    include = os.path.join(str(ion.ion_prefix()), "include")
    line = re.search(r"^Cflags:(.*)$", text, re.M).group(1)
    return line.replace("${includedir}", include).split()


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_ctypes_layout_matches_ion_headers(tmp_path):
    cflags = _ion_cflags()
    if cflags is None:
        pytest.skip("ION not built (build/ion/lib/pkgconfig/ion.pc missing)")
    src = tmp_path / "layout.c"
    src.write_text(LAYOUT_C)
    exe = tmp_path / "layout"
    subprocess.run(["gcc", *cflags, "-o", str(exe), str(src)], check=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    c = {}
    for line in out.splitlines():
        parts = line.split()
        c[parts[0]] = [int(x) for x in parts[1:]]
    import ctypes
    for name, cls in (("BpDelivery", ion.BpDelivery), ("BpTimestamp", ion.BpTimestamp),
                      ("ZcoReader", ion.ZcoReader)):
        assert c[name] == [ctypes.sizeof(cls)], name
        for fname, _ in cls._fields_:
            fld = getattr(cls, fname)
            assert c["%s.%s" % (name, fname)] == [fld.offset, fld.size], (name, fname)
    assert c["CONST"] == [ion.BP_POLL, ion.BP_BLOCKING, ion.ZcoSdrSource, ion.ZcoOutbound,
                          ion.BpPayloadPresent, ion.BpReceptionTimedOut,
                          ion.BpReceptionInterrupted, ion.BpEndpointStopped]


def test_ion_libraries_export_the_bound_symbols():
    lib_dir = os.path.join(str(ion.ion_prefix()), "lib")
    if not os.path.isfile(os.path.join(lib_dir, "libbp.so")):
        pytest.skip("ION not built")
    lib = ion.IonLibrary.load()
    for name in ("bp_attach", "bp_open", "bp_open_source", "bp_send", "bp_receive",
                 "bp_interrupt", "bp_release_delivery", "bp_close", "bp_detach"):
        assert getattr(lib.bp, name).restype is not None or name in ("bp_detach", "bp_close",
                                                                     "bp_interrupt",
                                                                     "bp_release_delivery")
    # getIonVersionNbr() returns "" until a process attaches; the live test
    # checks the attached value (run_start.ion_version).


def test_repo_root():
    assert os.path.isfile(os.path.join(REPO_ROOT, "docs", "test-vectors.json"))


def test_node_running_check(tmp_path, monkeypatch):
    """No bp_attach unless the node is listed in ion_nodes with its working memory."""
    monkeypatch.delenv("ION_NODE_WDNAME", raising=False)
    monkeypatch.delenv("ION_NODE_LIST_DIR", raising=False)
    assert ion.node_running()[0] is False
    node = str(tmp_path / "rover")
    ok, detail = ion.node_running(node, str(tmp_path))
    assert not ok and "cannot read" in detail
    (tmp_path / "ion_nodes").write_text("50 131122 ion50 %s 65330\n" % (tmp_path / "gateway"))
    ok, detail = ion.node_running(node, str(tmp_path))
    assert not ok and "not listed" in detail
    (tmp_path / "ion_nodes").write_text("99 2147483000 ion99 %s 65379\n" % node)
    ok, detail = ion.node_running(node, str(tmp_path))
    assert not ok and "working memory" in detail
    monkeypatch.setenv("ION_NODE_WDNAME", node)
    monkeypatch.setenv("ION_NODE_LIST_DIR", str(tmp_path))
    with pytest.raises(ion.IonError, match="ION node not running"):
        ion.BpEndpoint("ipn:99.1")
