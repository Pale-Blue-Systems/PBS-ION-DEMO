"""Endpoint A rover program.

Opens the rover's own EID (ipn:99.1) on the ROVER ION node through ION's
application interface (pbs_endpoint_a.ion, a ctypes binding to libbp),
sends telemetry envelopes at the five PBS priorities according to a plan,
receives command envelopes from Earth, verifies them (PBS-SEC-B-01,
PBS-AUTH-01), records accepted commands (no actuator is driven), rejects
the others with a reason code, and writes every send, receive and
execution as one JSON line (docs/LOG-SCHEMA.md).

    python3 -m pbs_endpoint_a.rover --log results/S1/endpoint-a.jsonl \\
        --scenario S1 --plan builtin:nominal:3:5 --duration 30

Run ``endpoint-python/bin/rover --help`` for all options.
"""

import argparse
import os
import signal
import sys
import threading
import time

from . import IMPL
from . import envelope as E
from . import ion, jsonlog, plan, secb
from .mux import FT_STATUS, build_container
from .pbslink import PBS_LINK

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.dirname(HERE), "config")
DEFAULT_KEYS = os.path.join(CONFIG_DIR, "rover-keys.json")
DEFAULT_POLICY = os.path.join(CONFIG_DIR, "rover-policy.json")

ROVER_SOURCE_ID = "Rover-Alpha"
ROVER_EID = "ipn:99.1"
EARTH_EID = "ipn:23.1"
WAIT_SLICE_S = 0.2


class Rover:
    """Send and receive logic; ``transport`` is an ion.BpEndpoint or a test double
    with ``send(dest, payload, lifespan_s, cos)``, ``receive()``, ``interrupt()``."""

    def __init__(self, transport, log, receiver, source_id=ROVER_SOURCE_ID, dest_eid=EARTH_EID,
                 sequence_start=1, clock=E.now_us, reply_ping=False):
        self.transport = transport
        self.log = log
        self.receiver = receiver
        self.dest_eid = dest_eid
        self.clock = clock
        self.reply_ping = reply_ping
        self.builder = E.EnvelopeBuilder(source_id, sequence_start)
        self._send_lock = threading.Lock()
        self._rx_lock = threading.Lock()
        self._stop = threading.Event()
        self._rx_thread = None
        self.rx_error = None

    # -- sending ------------------------------------------------------------

    def send(self, priority, payload, ttl_s, ack=False, label=None, frame_types=(),
             lifespan_override_s=None, commands=None):
        """Build one envelope with PBS_LINK and hand it to ION as one bundle."""
        with self._send_lock:
            ts = self.clock()
            env = self.builder.build(priority, payload, ttl_s, ack=ack, timestamp_us=ts)
            fields = jsonlog.envelope_fields(env, E.decode_header(env), frame_types)
            cos = E.class_of_service(priority)
            t = self.clock()
            if lifespan_override_s is not None:
                lifespan, outcome, rule = int(lifespan_override_s), "OK", "OVERRIDE"
            else:
                lifespan, outcome = E.ion_lifespan(ts, ttl_s, t)
                rule = "DTN-MAP-01-6.1"
            common = dict(dest_eid=self.dest_eid, lifespan_rule=rule, class_of_service=cos,
                          ordinal=E.ION_ORDINAL, label=label, commands=commands, **fields)
            if lifespan is None:
                return self.log.write("send", t, result="NOT_SENT", reason=outcome, **common)
            called = True
            try:
                self.transport.send(self.dest_eid, env, lifespan, cos)
                result, reason = "SENT", "OK"
            except ion.ZcoError:
                result, reason, called = "NOT_SENT", "ZCO_FAILED", False
            except ion.BpSendError:
                result, reason = "NOT_SENT", "BP_SEND_FAILED"
            t_ret = self.clock()
            return self.log.write("send", t, result=result, reason=reason,
                                  lifespan_s=lifespan if called else None,
                                  bp_send_return_us=t_ret if called else None, **common)

    def send_planned(self, item):
        return self.send(item.priority, item.payload, item.ttl_s, ack=item.ack_req,
                         label=item.label, frame_types=item.frame_types,
                         lifespan_override_s=item.lifespan_override_s)

    # -- receiving ----------------------------------------------------------

    def on_delivery(self, dlv):
        """Process one delivered bundle payload (one envelope)."""
        with self._rx_lock:
            disp = self.receiver.receive(dlv.payload, dlv.t_us)
            hdr = disp.header
            expected = E.SOURCE_EID.get(hdr.source_id) if hdr is not None else None
            sec, auth = disp.sec, disp.auth
            commands = ([c.decode("ascii", "replace") for c in disp.commands]
                        if disp.commands is not None else None)
            self.log.write(
                "recv", dlv.t_us, result=disp.result, reason=disp.reason,
                bundle_source_eid=dlv.source_eid, bundle_creation_ms=dlv.creation_ms,
                bundle_creation_count=dlv.creation_count,
                bundle_creation_us=(E.creation_ms_to_unix_us(dlv.creation_ms)
                                    if dlv.creation_ms is not None else None),
                bundle_ttl_field_ms=dlv.ttl_ms, bundle_admin_record=bool(dlv.admin_record),
                expected_eid=expected,
                eid_match=(dlv.source_eid == expected) if expected is not None else None,
                latency_us=(dlv.t_us - hdr.timestamp_us) if hdr is not None else None,
                commands=commands,
                suite_id=secb.suite_str(sec.suite) if sec else None,
                key_id=sec.key_id.decode("ascii", "replace") if sec else None,
                security_sequence=sec.security_sequence if sec else None,
                authority_id=auth.authority.decode("ascii", "replace") if auth else None,
                role=auth.role.decode("ascii", "replace") if auth else None,
                policy_epoch=auth.epoch if auth else None,
                **jsonlog.envelope_fields(dlv.payload, hdr, disp.frame_types))
            if not (disp.accepted and disp.commands):
                return disp
            for cmd in commands:
                op = cmd.split(" ", 1)[0]
                self.log.write("exec", self.clock(), source_id_hex=hdr.source_id_raw.hex(),
                               sequence=hdr.sequence, timestamp_us=hdr.timestamp_us,
                               command=cmd, operation=op,
                               key_id=sec.key_id.decode("ascii", "replace"),
                               security_sequence=sec.security_sequence,
                               role=auth.role.decode("ascii", "replace"), action="LOGGED")
                if self.reply_ping and op == "PING":
                    text = "PONG SEQ=%d SECSEQ=%d" % (hdr.sequence, sec.security_sequence)
                    self.send(1, build_container([(FT_STATUS, text.encode("ascii"))]), 60,
                              label="PING-REPLY", frame_types=(FT_STATUS,))
            return disp

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                dlv = self.transport.receive()
            except Exception as ex:          # ION failure: stop receiving, report
                self.rx_error = ex
                print("rover: receive failed: %s" % ex, file=sys.stderr, flush=True)
                return
            if dlv.result == ion.BpPayloadPresent:
                try:
                    self.on_delivery(dlv)
                except Exception as ex:      # a bad envelope must not stop the rover
                    print("rover: delivery processing failed: %r" % ex, file=sys.stderr,
                          flush=True)
            elif dlv.result == ion.BpEndpointStopped:
                return

    def start_receiving(self):
        self._rx_thread = threading.Thread(target=self._rx_loop, name="rover-rx", daemon=True)
        self._rx_thread.start()

    def stop_receiving(self, timeout=5.0):
        self._stop.set()
        if self._rx_thread is not None:
            for _ in range(int(timeout / 0.1)):
                self.transport.interrupt()
                self._rx_thread.join(0.1)
                if not self._rx_thread.is_alive():
                    break

    # -- main loop ------------------------------------------------------------

    def run(self, sends, stop_event, duration_s=None):
        """Send the plan on schedule while receiving; return at ``duration_s``
        (seconds after start) or when ``stop_event`` is set."""
        t0 = time.monotonic()

        def wait_until(deadline):
            while not stop_event.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    return True
                stop_event.wait(min(left, WAIT_SLICE_S))
            return False

        for item in sends:
            if duration_s is not None and item.at_s > duration_s:
                break
            if not wait_until(t0 + item.at_s):
                return
            self.send_planned(item)
        if duration_s is None:
            while not stop_event.is_set():
                stop_event.wait(WAIT_SLICE_S)
        else:
            wait_until(t0 + duration_s)


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="rover", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True, help="JSON Lines log file (appended)")
    ap.add_argument("--scenario", default=None, help="scenario label written to every record")
    ap.add_argument("--plan", default="builtin:none",
                    help="telemetry plan: JSON file, builtin:nominal[:COUNT[:INTERVAL_S]] "
                         "or builtin:none (default)")
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds to run; default: until SIGINT or SIGTERM")
    ap.add_argument("--eid", default=ROVER_EID, help="own EID (default %(default)s)")
    ap.add_argument("--dest", default=EARTH_EID, help="telemetry destination (default %(default)s)")
    ap.add_argument("--source-id", default=ROVER_SOURCE_ID, help="Source ID (default %(default)s)")
    ap.add_argument("--sequence-start", type=int, default=1,
                    help="Sequence of the first envelope (default 1)")
    ap.add_argument("--ion-node-dir", default=None,
                    help="ROVER node working directory (default $ION_NODE_WDNAME, else "
                         "$ION_RUN_DIR/rover, else build/run/ion/rover)")
    ap.add_argument("--ion-node-list-dir", default=None,
                    help="ION_NODE_LIST_DIR (default $ION_NODE_LIST_DIR, else the parent of "
                         "--ion-node-dir)")
    ap.add_argument("--keys", default=DEFAULT_KEYS, help="verification keys (TEST KEYS)")
    ap.add_argument("--policy", default=DEFAULT_POLICY, help="authorization policy")
    ap.add_argument("--reply-ping", action="store_true",
                    help="answer each accepted PING with a Status envelope (priority 1, TTL 60)")
    ap.add_argument("--ready-file", default=None,
                    help="file created once the endpoint is open and receiving")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    node_dir = args.ion_node_dir or os.environ.get("ION_NODE_WDNAME") or \
        ion.default_node_dir("rover")
    ion.node_environment(node_dir, args.ion_node_list_dir)

    sends = plan.expand(plan.load(args.plan))
    receiver = secb.rover_receiver(args.keys, args.policy)
    E.source_id_bytes(args.source_id)                  # refuse > 16 bytes before starting

    stop = threading.Event()
    endpoint = ion.BpEndpoint(args.eid)
    log = jsonlog.JsonlLog(args.log, "A", args.eid, args.scenario)
    rover = Rover(endpoint, log, receiver, source_id=args.source_id, dest_eid=args.dest,
                  sequence_start=args.sequence_start, reply_ping=args.reply_ping)

    def on_signal(signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log.write("run_start", E.now_us(), impl=IMPL, pbs_link_version=PBS_LINK.__version__,
              ion_version=endpoint.ion_version, source_id=args.source_id, dest_eid=args.dest,
              pid=os.getpid(), argv=[sys.executable] + sys.argv, keys=receiver.keys.key_ids())
    try:
        rover.start_receiving()
        if args.ready_file:
            with open(args.ready_file, "w") as fh:
                fh.write("%d\n" % os.getpid())
        print("rover: READY %s pid %d node %s" % (args.eid, os.getpid(), node_dir), flush=True)
        rover.run(sends, stop, args.duration)
    finally:
        rover.stop_receiving()
        log.write("run_end", E.now_us(), counts=dict(sorted(log.counts.items())))
        log.close()
        endpoint.close()
    return 1 if rover.rx_error else 0


if __name__ == "__main__":
    sys.exit(main())
