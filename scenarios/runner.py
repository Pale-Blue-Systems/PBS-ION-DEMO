#!/usr/bin/env python3
"""Run one scenario of the PBS ION demo.

  scenarios/runner.py scenarios/S1.json --out results/S1

Sequence (scenario time 0 = traffic origin O; T0 = O - lead_s):
  1. writes the contact plan, emulator configuration and both endpoint plans
     into the output directory;
  2. starts the link emulator (emulator/linkemu.py) on the four link ports;
  3. starts the three ION nodes (ion/start_nodes.sh) with the contact plan
     referenced to T0;
  4. starts Endpoint A (endpoint-python/bin/rover) on the ROVER node so that
     its plan starts at about O, then Endpoint B (pbs_earth run) on the EARTH
     node with its plan origin --t0-us exactly O;
  5. runs the probes (bplist / bpstats on a node) at their scenario times;
  6. at O + duration_s (end of the traffic phase) starts the drain: it reads
     both endpoint logs every 0.5 s and waits until every envelope handed to
     ION has been received by the other endpoint or its bundle lifetime has
     ended, for at most drain_max_s (scenario key, default 30 s: six LTP
     checkpoint timeouts of 5 s); then stops both endpoints (SIGTERM), runs
     bpstats and ltpstats -a on every node, copies each node's ion.log, stops
     the nodes (ion/stop_nodes.sh) and then the emulator;
  7. runs the scenario's post-run commands (offline cross-checks);
  8. writes run.json (times, versions, exit statuses).

A scenario with "live": false skips steps 2 to 6.

Environment:
  PBS_EARTH_BIN   Endpoint B program (default build/endpoint-c/pbs_earth)
  PBS_EARTH_TOOL  Endpoint B offline tool (default build/endpoint-c/pbs_tool)
  PYTHON          Python for Endpoint A and the tools (default: this interpreter)
  PBS_DEMO_WAIT_S seconds to wait for another ION instance on this host to
                  finish before giving up (default 0)
"""
import argparse
import datetime
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import scenario as S  # noqa: E402

ION_PREFIX = os.environ.get("ION_PREFIX", os.path.join(ROOT, "build", "ion"))
ION_RUN_DIR = os.environ.get("ION_RUN_DIR", os.path.join(ROOT, "build", "run", "ion"))
PYTHON = os.environ.get("PYTHON", sys.executable)
EARTH_BIN = os.environ.get("PBS_EARTH_BIN", os.path.join(ROOT, "build", "endpoint-c", "pbs_earth"))
EARTH_TOOL = os.environ.get("PBS_EARTH_TOOL", os.path.join(ROOT, "build", "endpoint-c", "pbs_tool"))
ROVER_BIN = os.path.join(ROOT, "endpoint-python", "bin", "rover")
ROVER_TOOL = os.path.join(ROOT, "endpoint-python", "bin", "tool")
LINKEMU = os.path.join(ROOT, "emulator", "linkemu.py")

ROVER_STARTUP_EST_S = 0.1      # launch-to-ready time of bin/rover (0.098 s measured)
STOP_GRACE_S = 15
DRAIN_MAX_S = 30               # default bound of the drain after the traffic phase
DRAIN_POLL_S = 0.5
LIFETIME_MARGIN_S = 2.0        # a bundle counts as expired this long after send + lifetime


def now():
    return time.time()


def log(msg):
    print("[%s] %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S"), msg),
          flush=True)


def sleep_until(t):
    while True:
        d = t - now()
        if d <= 0:
            return
        time.sleep(min(d, 0.2))


def ion_env(node=None):
    env = dict(os.environ)
    env["ION_NODE_LIST_DIR"] = ION_RUN_DIR
    env["ION_RUN_DIR"] = ION_RUN_DIR
    env["PATH"] = os.path.join(ION_PREFIX, "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHON"] = PYTHON
    if node:
        env["ION_NODE_WDNAME"] = os.path.join(ION_RUN_DIR, node)
    return env


def ion_state_clean():
    cmd = 'source "%s/ion/env.sh"; source "%s/ion/lib.sh"; ion_state_clean' % (ROOT, ROOT)
    return subprocess.run(["bash", "-c", cmd], env=ion_env()).returncode == 0


def ion_state_report():
    cmd = 'source "%s/ion/env.sh"; source "%s/ion/lib.sh"; ion_state_report' % (ROOT, ROOT)
    return subprocess.run(["bash", "-c", cmd], env=ion_env(), capture_output=True,
                          text=True).stdout


def read_jsonl(path):
    """Complete lines of a JSON-lines log that is still being written."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    break
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def undelivered(log_a, log_b, now_s):
    """Envelopes handed to ION (send result SENT) that the other endpoint has
    not logged as received and whose bundle lifetime has not ended
    (send time + lifespan_s + LIFETIME_MARGIN_S >= now_s)."""
    logs = {"A": read_jsonl(log_a), "B": read_jsonl(log_b)}
    received = {}
    for ep, recs in logs.items():
        received[ep] = {(r.get("source_id_hex"), r.get("sequence"), r.get("timestamp_us"))
                        for r in recs if r.get("event") == "recv"}
    out = []
    for ep, peer in (("A", "B"), ("B", "A")):
        for r in logs[ep]:
            if r.get("event") != "send" or r.get("result") != "SENT":
                continue
            ident = (r.get("source_id_hex"), r.get("sequence"), r.get("timestamp_us"))
            if ident in received[peer]:
                continue
            life = r.get("lifespan_s")
            if life is not None and r["t_us"] / 1e6 + life + LIFETIME_MARGIN_S < now_s:
                continue
            out.append({"from": ep, "label": r.get("label"), "sequence": r.get("sequence"),
                        "send_unix_s": r["t_us"] / 1e6})
    return out


def utc_ion(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y/%m/%d-%H:%M:%S")


def git_head():
    try:
        r = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
                           text=True, check=True)
        # Tracked files only, outside the outputs this demo writes.
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain",
                                "--untracked-files=no", "--", ".", ":!results", ":!REPORT.md"],
                               capture_output=True, text=True).stdout.strip() != ""
        return {"commit": r.stdout.strip(), "worktree_modified": dirty}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"commit": None, "worktree_modified": None}


def host_info():
    info = {"platform": platform.platform(), "python": platform.python_version(),
            "nproc": os.cpu_count()}
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        info["clock_source"] = open(
            "/sys/devices/system/clocksource/clocksource0/current_clocksource").read().strip()
    except OSError:
        info["clock_source"] = None
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
        info["ntp_synchronized"] = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        info["ntp_synchronized"] = None
    return info


def ion_version():
    stamp = os.path.join(ION_PREFIX, ".ion-commit")
    commit = open(stamp).read().strip() if os.path.exists(stamp) else None
    tag = None
    build = os.path.join(ROOT, "ion", "build_ion.sh")
    if os.path.exists(build):
        for line in open(build):
            if line.startswith("ION_TAG="):
                tag = line.split("=", 1)[1].strip().strip('"').split(":-")[-1].rstrip("}")
    return {"tag": tag, "commit": commit}


class Run:
    def __init__(self, scn_path, out):
        self.scn_path = os.path.abspath(scn_path)
        self.scn = S.load(scn_path)
        self.topo = S.load_topology()
        self.out = os.path.abspath(out)
        self.procs = {}
        self.files = {}
        self.nodes_started = False
        self.info = {"scenario": self.scn["id"], "title": self.scn["title"],
                     "scenario_file": os.path.relpath(self.scn_path, ROOT),
                     "lead_s": self.scn["lead_s"], "duration_s": self.scn["duration_s"],
                     "git": git_head(), "ion": ion_version(), "host": host_info(),
                     "steps": [], "exit": {}, "probes": [], "post": []}

    def step(self, name, **kw):
        rec = {"step": name, "t_us": int(now() * 1e6)}
        rec.update(kw)
        self.info["steps"].append(rec)
        log("%s: %s %s" % (self.scn["id"], name, " ".join("%s=%s" % kv for kv in kw.items())))

    def path(self, *parts):
        return os.path.join(self.out, *parts)

    # -- preparation --------------------------------------------------------

    def prepare(self):
        if os.path.isdir(self.out):
            shutil.rmtree(self.out)
        os.makedirs(self.path("ion"))
        os.makedirs(self.path("probes"))
        shutil.copy(self.scn_path, self.path("scenario.json"))
        with open(self.path("contacts.ionrc"), "w") as fh:
            fh.write(S.contact_plan(self.scn, self.topo))
        with open(self.path("emulator.json"), "w") as fh:
            json.dump(S.emulator_config(self.scn, self.topo), fh, indent=1)
        with open(self.path("rover-plan.json"), "w") as fh:
            json.dump(S.rover_plan(self.scn), fh, indent=1)
        with open(self.path("earth-plan.jsonl"), "w") as fh:
            fh.write(S.earth_plan_lines(self.scn))

    def wait_for_clean_ion(self):
        wait = float(os.environ.get("PBS_DEMO_WAIT_S", "0"))
        deadline = now() + wait
        while not ion_state_clean():
            if now() >= deadline:
                raise RuntimeError("ION state already present on this host (another demo "
                                   "instance, or leftovers; run ion/stop_nodes.sh):\n" +
                                   ion_state_report())
            log("%s: ION in use on this host; waiting" % self.scn["id"])
            time.sleep(10)

    # -- processes ----------------------------------------------------------

    def spawn(self, name, argv, env=None, cwd=None):
        fh = open(self.path(name + ".out"), "w")
        self.files[name] = fh
        p = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=env or ion_env(), cwd=cwd or ROOT, start_new_session=True)
        self.procs[name] = p
        return p

    def stop_proc(self, name, sig=signal.SIGTERM, grace=STOP_GRACE_S):
        p = self.procs.get(name)
        if p is None:
            return None
        if p.poll() is None:
            try:
                os.kill(p.pid, sig)
            except ProcessLookupError:
                pass
            try:
                p.wait(grace)
            except subprocess.TimeoutExpired:
                log("%s: %s did not stop within %d s; SIGKILL" % (self.scn["id"], name, grace))
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(5)
        self.info["exit"][name] = p.returncode
        fh = self.files.pop(name, None)
        if fh:
            fh.close()
        return p.returncode

    def wait_file(self, path, timeout, proc=None):
        deadline = now() + timeout
        while now() < deadline:
            if os.path.exists(path):
                return True
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(0.02)
        return False

    def ion_tool(self, node, argv, out_name, timeout=30):
        """Run an ION utility attached to a node; stdout to probes/<out_name>."""
        t = now()
        with open(self.path("probes", out_name), "w") as fh:
            try:
                r = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=ion_env(node),
                                   cwd=os.path.join(ION_RUN_DIR, node), timeout=timeout)
                rc = r.returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
        return {"node": node, "argv": argv, "file": "probes/" + out_name,
                "t_us": int(t * 1e6), "exit": rc}

    # -- live phase -----------------------------------------------------------

    def live(self):
        scn = self.scn
        lead = scn["lead_s"]
        self.wait_for_clean_ion()
        t0 = float(int(now()) + 2)                    # contact-plan reference (whole second)
        origin = t0 + lead
        self.info.update({"t0_unix_s": t0, "t0_utc": utc_ion(t0), "origin_unix_s": origin,
                          "origin_us": int(origin * 1e6)})

        emu_ready = self.path("emulator.ready")
        self.spawn("emulator", [PYTHON, LINKEMU, "--config", self.path("emulator.json"),
                                "--t0", "%.6f" % origin, "--log", self.path("emulator.jsonl"),
                                "--stats", self.path("emulator-stats.json"),
                                "--ready-file", emu_ready])
        if not self.wait_file(emu_ready, 10, self.procs["emulator"]):
            raise RuntimeError("link emulator did not start; see emulator.out")
        self.step("emulator_ready")

        env = ion_env()
        env["ION_T0"] = utc_ion(t0)
        sleep_until(t0)
        with open(self.path("start_nodes.log"), "w") as fh:
            r = subprocess.run([os.path.join(ROOT, "ion", "start_nodes.sh"),
                                self.path("contacts.ionrc")], stdout=fh,
                               stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
        self.nodes_started = True
        if r.returncode != 0:
            raise RuntimeError("ion/start_nodes.sh failed; see start_nodes.log")
        self.step("nodes_started", t0_utc=utc_ion(t0))
        if now() > origin - ROVER_STARTUP_EST_S - 0.5:
            raise RuntimeError("nodes took longer than lead_s to start")

        # Endpoint A, timed so that its plan origin falls near O.
        rover_ready = self.path("rover.ready")
        run_s = scn["duration_s"] + scn["drain_max_s"] + 20     # endpoints outlive the drain
        argv = [ROVER_BIN, "--log", self.path("endpoint-a.jsonl"), "--scenario", scn["id"],
                "--plan", self.path("rover-plan.json"),
                "--duration", str(run_s),
                "--ion-node-dir", os.path.join(ION_RUN_DIR, "rover"),
                "--ion-node-list-dir", ION_RUN_DIR, "--ready-file", rover_ready]
        argv += scn["rover"].get("args", [])
        sleep_until(origin - ROVER_STARTUP_EST_S)
        launch = now()
        p = self.spawn("endpoint-a", argv, env=ion_env("rover"))
        if not self.wait_file(rover_ready, 20, p):
            raise RuntimeError("Endpoint A did not become ready; see endpoint-a.out")
        a_origin = os.stat(rover_ready).st_mtime_ns / 1e9
        self.info["endpoint_a"] = {"argv": argv, "launch_unix_s": launch,
                                   "plan_origin_unix_s": a_origin,
                                   "plan_origin_offset_s": round(a_origin - origin, 6),
                                   "startup_s": round(a_origin - launch, 6)}
        self.step("endpoint_a_ready", plan_origin_offset_s=round(a_origin - origin, 3))

        argv = [EARTH_BIN, "run", "--plan", self.path("earth-plan.jsonl"),
                "--log", self.path("endpoint-b.jsonl"), "--scenario", scn["id"],
                "--t0-us", str(int(origin * 1e6)), "--duration", str(run_s),
                "--node-dir", os.path.join(ION_RUN_DIR, "earth")]
        argv += scn["earth"].get("args", [])
        self.spawn("endpoint-b", argv, env=ion_env("earth"))
        self.info["endpoint_b"] = {"argv": argv, "launch_unix_s": now(),
                                   "plan_origin_unix_s": origin}
        self.step("endpoint_b_started")

        probes = sorted(scn["probes"], key=lambda p: p["at_s"])
        stop_at = origin + scn["duration_s"]
        for i, pr in enumerate(probes):
            sleep_until(origin + pr["at_s"])
            for name, p in (("endpoint-a", self.procs["endpoint-a"]),
                            ("endpoint-b", self.procs["endpoint-b"])):
                if p.poll() is not None:
                    raise RuntimeError("%s exited early (status %s); see %s.out"
                                       % (name, p.returncode, name))
            tool = pr["tool"]
            argv = {"bplist": ["bplist", "count"], "bplist_detail": ["bplist"],
                    "bpstats": ["bpstats"]}[tool]
            rec = self.ion_tool(pr["node"], argv, "%02d-%s-%s.txt" % (i, pr["node"], tool))
            rec["at_s"] = pr["at_s"]
            rec["scenario_t_s"] = round(rec["t_us"] / 1e6 - origin, 3)
            if "label" in pr:
                rec["label"] = pr["label"]
            self.info["probes"].append(rec)
            self.step("probe", node=pr["node"], tool=tool, at_s=pr["at_s"])
        sleep_until(stop_at)
        for name in ("endpoint-a", "endpoint-b"):
            if self.procs[name].poll() is not None:
                self.step("early_exit", endpoint=name, status=self.procs[name].returncode)
        self.step("traffic_phase_end")
        self.drain(origin, stop_at)
        self.stop_proc("endpoint-a")
        self.stop_proc("endpoint-b")
        self.step("endpoints_stopped", a=self.info["exit"].get("endpoint-a"),
                  b=self.info["exit"].get("endpoint-b"))
        for node in ("rover", "gateway", "earth"):
            for tool in (["bpstats"], ["ltpstats", "-a"]):
                rec = self.ion_tool(node, tool, "final-%s-%s.txt" % (node, tool[0]))
                rec["final"] = True
                self.info["probes"].append(rec)
        time.sleep(1.0)
        self.collect_ion_logs()

    def drain(self, origin, stop_at):
        """Wait until nothing handed to ION is still on its way (or drain_max_s)."""
        bound = stop_at + self.scn["drain_max_s"]
        while True:
            t = now()
            pending = undelivered(self.path("endpoint-a.jsonl"), self.path("endpoint-b.jsonl"), t)
            alive = all(self.procs[n].poll() is None for n in ("endpoint-a", "endpoint-b"))
            if not pending or t >= bound or not alive:
                break
            time.sleep(DRAIN_POLL_S)
        self.info["drain"] = {
            "max_s": self.scn["drain_max_s"], "end_unix_s": t,
            "end_scenario_s": round(t - origin, 3), "duration_s": round(t - stop_at, 3),
            "complete": not pending,
            "pending": [{"from": p["from"], "label": p["label"], "sequence": p["sequence"],
                         "send_scenario_s": round(p["send_unix_s"] - origin, 3)}
                        for p in pending]}
        self.step("drain_end", duration_s=round(t - stop_at, 1), pending=len(pending))

    def collect_ion_logs(self):
        for node in ("rover", "gateway", "earth"):
            d = os.path.join(ION_RUN_DIR, node)
            for f in ("ion.log", "start.log", "contacts.ionrc"):
                src = os.path.join(d, f)
                if os.path.exists(src):
                    shutil.copy(src, self.path("ion", "%s.%s" % (node, f)))

    def shutdown(self):
        for name in ("endpoint-a", "endpoint-b"):
            if name in self.procs and self.procs[name].poll() is None:
                self.stop_proc(name)
        if self.nodes_started:
            t = now()
            with open(self.path("stop_nodes.log"), "w") as fh:
                r = subprocess.run([os.path.join(ROOT, "ion", "stop_nodes.sh")], stdout=fh,
                                   stderr=subprocess.STDOUT, env=ion_env(),
                                   stdin=subprocess.DEVNULL)
            self.info["exit"]["stop_nodes"] = r.returncode
            text = open(self.path("stop_nodes.log")).read()
            self.info["stop_clean_without_fallback"] = (
                r.returncode == 0 and "falling back" not in text)
            self.info["stop_nodes_s"] = round(now() - t, 1)
            # ion.log gains the shutdown lines; copy again.
            self.collect_ion_logs()
            self.step("nodes_stopped", status=r.returncode)
        if "emulator" in self.procs:
            self.stop_proc("emulator", signal.SIGTERM, 10)
            self.step("emulator_stopped")

    # -- post-run commands ----------------------------------------------------

    def post(self):
        subst = {"out": self.out, "root": ROOT, "python": PYTHON, "earth_tool": EARTH_TOOL,
                 "rover_tool": ROVER_TOOL}
        for item in self.scn.get("post", []):
            argv = [a.format(**subst) for a in item["argv"]]
            outfile = self.path(item["name"] + ".out")
            t = now()
            with open(outfile, "w") as fh:
                r = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT,
                                   env=ion_env(), stdin=subprocess.DEVNULL)
            self.info["post"].append({"name": item["name"], "argv": argv,
                                      "exit": r.returncode, "file": item["name"] + ".out",
                                      "duration_s": round(now() - t, 3)})
            self.step("post", command=item["name"], status=r.returncode)

    def write_info(self):
        with open(self.path("run.json"), "w") as fh:
            json.dump(self.info, fh, indent=1, sort_keys=True)

    def run(self):
        self.prepare()
        self.info["started_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        err = None
        try:
            if self.scn["live"]:
                self.live()
        except BaseException as ex:            # includes KeyboardInterrupt
            err = ex
            self.info["error"] = "%s: %s" % (type(ex).__name__, ex)
            log("%s: ERROR %s" % (self.scn["id"], self.info["error"]))
        finally:
            try:
                self.shutdown()
            finally:
                self.write_info()
        if err is None:
            self.post()
        self.info["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.write_info()
        if isinstance(err, KeyboardInterrupt):
            raise err
        return 0 if err is None else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("scenario", help="scenario file (scenarios/S*.json)")
    ap.add_argument("--out", required=True, help="output directory (replaced)")
    args = ap.parse_args(argv)
    return Run(args.scenario, args.out).run()


if __name__ == "__main__":
    sys.exit(main())
