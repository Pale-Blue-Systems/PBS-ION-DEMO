"""Thin ctypes binding to ION 4.2.0's application interface (libbp, libici).

Only the calls an application makes are bound: ``bp_attach``, ``bp_open``,
``bp_send``, ``bp_receive``, ``bp_interrupt``, ``bp_release_delivery``,
``bp_close``, ``bp_detach``, and the SDR / ZCO calls that ION's own
``bpsource``, ``bpsink`` and ``bpchat`` use to build and read a payload
(``sdr_begin_xn``, ``Sdr_malloc``, ``Sdr_write``, ``sdr_end_xn``,
``ionCreateZco``, ``zco_source_data_length``, ``zco_start_receiving``,
``zco_receive_source``, ``zco_destroy``).

Struct layouts follow ``build/ion/include/bp.h`` and ``zco.h`` on x86-64
Linux (``uvast``/``uaddr`` = unsigned long, ``vast`` = long);
tests/test_ion_layout.py compares them with the C compiler's layout.

The process must see the same ``ION_NODE_LIST_DIR`` as the ION node, and
``ION_NODE_WDNAME`` set to the node's working directory
(ion/README.md, gotcha 1). ``open_endpoint`` sets both from its arguments.
"""

import ctypes
import os
import pathlib
import threading
from ctypes import (POINTER, Structure, byref, c_char_p, c_int, c_long, c_size_t, c_ubyte,
                    c_uint, c_ulong, c_void_p)

from .envelope import now_us

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

BP_POLL = 0
BP_BLOCKING = -1

BpPayloadPresent = 1
BpReceptionTimedOut = 2
BpReceptionInterrupted = 3
BpEndpointStopped = 4
RESULT_NAMES = {1: "BpPayloadPresent", 2: "BpReceptionTimedOut",
                3: "BpReceptionInterrupted", 4: "BpEndpointStopped"}

ZcoSdrSource = 4
ZcoOutbound = 1
NoCustodyRequested = 0
BP_MAX_METADATA_LEN = 30
SDR_ERROR_OBJECT = (1 << 64) - 1          # (SdrObject) ERROR on 64-bit


class BpTimestamp(Structure):
    _fields_ = [("msec", c_ulong),        # uvast, ms since 2000-01-01
                ("count", c_uint)]


class BpDelivery(Structure):
    _fields_ = [("result", c_int),        # BpIndResult
                ("bundleSourceEid", c_void_p),
                ("bundleCreationTime", BpTimestamp),
                ("timeToLive", c_uint),
                ("ackRequested", c_int),
                ("adminRecord", c_int),
                ("adu", c_ulong),         # SdrObject
                ("metadataType", c_ubyte),
                ("metadataLen", c_ubyte),
                ("metadata", c_ubyte * BP_MAX_METADATA_LEN)]


class ZcoReader(Structure):
    _fields_ = [("zco", c_ulong),
                ("trackFileOffset", c_int),
                ("headersLengthCopied", c_long),
                ("sourceLengthCopied", c_long),
                ("trailersLengthCopied", c_long),
                ("lengthCopied", c_long)]


class IonError(RuntimeError):
    pass


class ZcoError(IonError):
    """ION could not store the payload (SDR allocation or ionCreateZco)."""


class BpSendError(IonError):
    def __init__(self, rc):
        super().__init__("bp_send returned %d" % rc)
        self.rc = rc


def ion_prefix():
    return pathlib.Path(os.environ.get("ION_PREFIX", REPO_ROOT / "build" / "ion"))


class IonLibrary:
    """libici.so and libbp.so with argument and return types declared."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def load(cls, lib_dir=None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(lib_dir)
            return cls._instance

    def __init__(self, lib_dir=None):
        lib_dir = pathlib.Path(lib_dir or ion_prefix() / "lib")
        mode = getattr(os, "RTLD_GLOBAL", ctypes.RTLD_GLOBAL)
        try:
            self.ici = ctypes.CDLL(str(lib_dir / "libici.so"), mode=mode)
            self.bp = ctypes.CDLL(str(lib_dir / "libbp.so"), mode=mode)
        except OSError as ex:
            raise IonError("cannot load ION libraries from %s (run ion/build_ion.sh): %s"
                           % (lib_dir, ex)) from ex
        self.lib_dir = lib_dir
        b, i = self.bp, self.ici
        self._decl(b.bp_attach, c_int)
        self._decl(b.bp_detach, None)
        self._decl(b.bp_open, c_int, c_char_p, POINTER(c_void_p))
        self._decl(b.bp_open_source, c_int, c_char_p, POINTER(c_void_p), c_int)
        self._decl(b.bp_close, None, c_void_p)
        self._decl(b.bp_get_sdr, c_void_p)
        self._decl(b.bp_send, c_int, c_void_p, c_char_p, c_char_p, c_int, c_int, c_int,
                   c_ubyte, c_int, c_void_p, c_ulong, POINTER(c_ulong))
        self._decl(b.bp_receive, c_int, c_void_p, POINTER(BpDelivery), c_int)
        self._decl(b.bp_interrupt, None, c_void_p)
        self._decl(b.bp_release_delivery, None, POINTER(BpDelivery), c_int)
        self._decl(i.sdr_begin_xn, c_int, c_void_p)
        self._decl(i.sdr_end_xn, c_int, c_void_p)
        self._decl(i.sdr_exit_xn, None, c_void_p)
        self._decl(i.sdr_cancel_xn, None, c_void_p)
        self._decl(i.Sdr_malloc, c_ulong, c_char_p, c_int, c_void_p, c_size_t)
        self._decl(i.Sdr_write, None, c_char_p, c_int, c_void_p, c_ulong, c_char_p, c_size_t)
        self._decl(i.ionCreateZco, c_ulong, c_int, c_ulong, c_long, c_long, c_ubyte, c_ubyte,
                   c_int, c_void_p)
        self._decl(i.zco_source_data_length, c_long, c_void_p, c_ulong)
        self._decl(i.zco_start_receiving, None, c_ulong, POINTER(ZcoReader))
        self._decl(i.zco_receive_source, c_long, c_void_p, POINTER(ZcoReader), c_long, c_void_p)
        self._decl(i.zco_destroy, None, c_void_p, c_ulong)
        self._decl(i.getIonVersionNbr, c_char_p)
        self._decl(i.getOwnFqnn, c_ulong)

    @staticmethod
    def _decl(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)


class Delivery:
    """A bundle delivered by ``bp_receive``."""

    __slots__ = ("result", "t_us", "source_eid", "creation_ms", "creation_count",
                 "ttl_ms", "admin_record", "payload")

    def __init__(self, result, t_us, source_eid=None, creation_ms=None, creation_count=None,
                 ttl_ms=None, admin_record=False, payload=None):
        self.result = result
        self.t_us = t_us
        self.source_eid = source_eid
        self.creation_ms = creation_ms
        self.creation_count = creation_count
        self.ttl_ms = ttl_ms
        self.admin_record = admin_record
        self.payload = payload


class BpEndpoint:
    """One open ION endpoint (``bp_open``): sends with that EID as source and
    takes delivery of bundles destined for it.

    ``send`` and ``receive`` may be called from different threads (as in
    ION's ``bpchat``); the binding's own SDR transactions are serialized by
    a lock.
    """

    def __init__(self, eid, lib=None, send_only=False, check_node=True):
        self.eid = eid
        self.send_only = send_only
        if check_node:
            ok, detail = node_running()
            if not ok:
                raise IonError("ION node not running: %s" % detail)
        self.lib = lib or IonLibrary.load()
        self._sdr_lock = threading.Lock()
        if self.lib.bp.bp_attach() < 0:
            raise IonError("bp_attach failed: is the ION node running, and are "
                           "ION_NODE_LIST_DIR / ION_NODE_WDNAME set for it?")
        self._sap = c_void_p()
        if send_only:
            # bp_open_source: send with this source EID without taking
            # delivery, so another process may hold the endpoint open.
            rc = self.lib.bp.bp_open_source(eid.encode("ascii"), byref(self._sap), 0)
        else:
            rc = self.lib.bp.bp_open(eid.encode("ascii"), byref(self._sap))
        if rc < 0:
            self.lib.bp.bp_detach()
            raise IonError("%s(%s) failed (endpoint not declared, or already open "
                           "in another process)"
                           % ("bp_open_source" if send_only else "bp_open", eid))
        self.sdr = self.lib.bp.bp_get_sdr()
        self.closed = False

    @property
    def ion_version(self):
        v = self.lib.ici.getIonVersionNbr()
        return v.decode("ascii", "replace") if v else None

    @property
    def own_node(self):
        return int(self.lib.ici.getOwnFqnn())

    def send(self, dest_eid, payload, lifespan_s, class_of_service, report_to=None):
        """Hand ``payload`` to ION as one bundle (``bp_send``).

        Raises ZcoError or BpSendError; returns 1 on success.
        """
        payload = bytes(payload)
        lib, sdr = self.lib, self.sdr
        with self._sdr_lock:
            if lib.ici.sdr_begin_xn(sdr) == 0:
                raise ZcoError("sdr_begin_xn failed")
            extent = lib.ici.Sdr_malloc(b"pbs_endpoint_a/ion.py", 0, sdr, len(payload))
            if extent:
                lib.ici.Sdr_write(b"pbs_endpoint_a/ion.py", 0, sdr, extent, payload, len(payload))
            if lib.ici.sdr_end_xn(sdr) < 0 or extent == 0:
                raise ZcoError("no SDR space for a %d-byte payload" % len(payload))
            zco = lib.ici.ionCreateZco(ZcoSdrSource, extent, 0, len(payload), class_of_service,
                                       0, ZcoOutbound, None)
        if zco in (0, SDR_ERROR_OBJECT):
            raise ZcoError("ionCreateZco failed for a %d-byte payload" % len(payload))
        new_bundle = c_ulong(0)
        rc = lib.bp.bp_send(self._sap, dest_eid.encode("ascii"),
                            report_to.encode("ascii") if report_to else None,
                            int(lifespan_s), int(class_of_service), NoCustodyRequested, 0, 0,
                            None, zco, byref(new_bundle))
        if rc <= 0:
            with self._sdr_lock:
                if lib.ici.sdr_begin_xn(sdr):
                    lib.ici.zco_destroy(sdr, zco)
                    lib.ici.sdr_end_xn(sdr)
            raise BpSendError(rc)
        return rc

    def receive(self, timeout=BP_BLOCKING):
        """Wait for one delivery (``bp_receive``). ``t_us`` of the result is
        read from the host clock immediately after ``bp_receive`` returns."""
        lib = self.lib
        dlv = BpDelivery()
        rc = lib.bp.bp_receive(self._sap, byref(dlv), int(timeout))
        t_us = now_us()
        if rc < 0:
            raise IonError("bp_receive failed")
        if dlv.result != BpPayloadPresent:
            return Delivery(dlv.result, t_us)
        try:
            eid = ctypes.string_at(dlv.bundleSourceEid).decode("ascii", "replace") \
                if dlv.bundleSourceEid else None
            with self._sdr_lock:
                if lib.ici.sdr_begin_xn(self.sdr) == 0:
                    raise IonError("sdr_begin_xn failed")
                n = lib.ici.zco_source_data_length(self.sdr, dlv.adu)
                reader = ZcoReader()
                lib.ici.zco_start_receiving(dlv.adu, byref(reader))
                buf = ctypes.create_string_buffer(max(n, 1))
                got = lib.ici.zco_receive_source(self.sdr, byref(reader), n, buf) if n > 0 else 0
                if lib.ici.sdr_end_xn(self.sdr) < 0 or got < 0:
                    raise IonError("cannot read bundle payload")
            return Delivery(BpPayloadPresent, t_us, eid, int(dlv.bundleCreationTime.msec),
                            int(dlv.bundleCreationTime.count), int(dlv.timeToLive),
                            bool(dlv.adminRecord), buf.raw[:got])
        finally:
            lib.bp.bp_release_delivery(byref(dlv), 1)

    def interrupt(self):
        """Wake a ``receive`` blocked in another thread (``bp_interrupt``)."""
        if not self.closed:
            self.lib.bp.bp_interrupt(self._sap)

    def close(self):
        if not self.closed:
            self.closed = True
            self.lib.bp.bp_close(self._sap)
            self.lib.bp.bp_detach()


def default_node_dir(node):
    """Working directory of a demo node as ion/start_nodes.sh creates it."""
    if os.environ.get("ION_RUN_DIR"):
        return os.path.join(os.environ["ION_RUN_DIR"], node)
    return str(REPO_ROOT / "build" / "run" / "ion" / node)


def node_environment(node_dir, node_list_dir=None):
    """Set ION_NODE_WDNAME and ION_NODE_LIST_DIR for this process.

    ION compares these as strings with the values the node was started
    with (ion/env.sh), so they are used as given, without resolving
    symbolic links. ``node_list_dir`` defaults to $ION_NODE_LIST_DIR, else
    the parent directory of ``node_dir``.
    """
    node_dir = str(node_dir)
    if node_list_dir is None:
        node_list_dir = os.environ.get("ION_NODE_LIST_DIR") or os.path.dirname(node_dir)
    os.environ["ION_NODE_WDNAME"] = node_dir
    os.environ["ION_NODE_LIST_DIR"] = str(node_list_dir)
    return node_dir, str(node_list_dir)


def _shm_keys():
    """SysV shared-memory keys on this host, or None if /proc does not list them."""
    try:
        with open("/proc/sysvipc/shm") as fh:
            rows = fh.read().splitlines()[1:]
    except OSError:
        return None
    return {int(r.split()[0]) for r in rows if r.strip()}


def node_running(node_dir=None, node_list_dir=None):
    """Return ``(ok, detail)``: the node is listed in ``ion_nodes`` and its
    working-memory segment (wmKey) exists.

    Checked before ``bp_attach``: with no node running, ION 4.2.0's attach
    creates the host-global semaphore table (shared memory 0xee08 and
    /dev/shm/sem.ion:GLOBAL:0) before it fails, and leaves both behind;
    ion/start_nodes.sh then refuses to start.
    """
    node_dir = node_dir or os.environ.get("ION_NODE_WDNAME")
    node_list_dir = node_list_dir or os.environ.get("ION_NODE_LIST_DIR")
    if not node_dir or not node_list_dir:
        return False, "ION_NODE_WDNAME and ION_NODE_LIST_DIR must be set"
    path = os.path.join(node_list_dir, "ion_nodes")
    try:
        with open(path) as fh:
            rows = fh.read().splitlines()
    except OSError as ex:
        return False, "cannot read %s (%s)" % (path, ex.strerror)
    for row in rows:
        cols = row.split()
        if len(cols) >= 4 and cols[3] == node_dir:
            try:
                wm_key = int(cols[1])
            except ValueError:
                return False, "unreadable line in %s: %r" % (path, row)
            keys = _shm_keys()
            if keys is not None and wm_key not in keys:
                return False, ("node %s is listed in %s but its working memory (key 0x%x) "
                               "does not exist" % (cols[0], path, wm_key))
            return True, "node %s" % cols[0]
    return False, "%s is not listed in %s" % (node_dir, path)
