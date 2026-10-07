"""PNG charts for REPORT.md (matplotlib, optional).

Two chart forms:
  timeline   latency (y) against send time (x), one point per envelope, so
             storage across an outage shows as a ramp that ends when the
             contact resumes; outage windows are shaded (scheduled: solid
             band, unscheduled: hatched); envelopes that were sent but never
             received are drawn as crosses on a band above the plot.
  latency    latency per priority, one point per received envelope, per
             direction.
Priority is ordinal, so it is encoded with one blue ramp (CRITICAL darkest)
plus a different marker shape per priority; direction is encoded by the
marker fill (filled: rover to Earth, open: Earth to rover).
"""
import os

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    AVAILABLE = True
except ImportError:          # charts are optional
    AVAILABLE = False

PRIO_COLOR = {0: "#0d366b", 1: "#1c5cab", 2: "#2a78d6", 3: "#5598e7", 4: "#86b6ef"}
PRIO_MARKER = {0: "D", 1: "^", 2: "o", 3: "s", 4: "v"}
PRIO_NAME = {0: "CRITICAL", 1: "HIGH", 2: "NORMAL", 3: "LOW", 4: "BULK"}
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
OUTAGE = "#f0efec"
LOST = "#e34948"


def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def timeline(rows, outages, title, path, duration_s=None):
    if not AVAILABLE:
        return None
    sent = [r for r in rows if r["send_result"] == "SENT"]
    if not sent:
        return None
    fig, ax = plt.subplots(figsize=(9.0, 4.2), dpi=120)
    _style(ax)
    lat = [r["latency_s"] for r in sent if r["delivered"]]
    top = max(lat + [3.0]) * 1.12
    for o in outages:
        if o["kind"] == "scheduled":
            ax.axvspan(o["start_s"], o["end_s"], color=OUTAGE, zorder=0)
        else:
            ax.axvspan(o["start_s"], o["end_s"], facecolor="none", edgecolor="#b8b7b2",
                       hatch="///", linewidth=0, zorder=0)
        ax.text((o["start_s"] + o["end_s"]) / 2, top * 0.99, "%s\n%s" % (
            o["leg"].replace("-", "–"), o["kind"]), ha="center", va="top", fontsize=7, color=INK2)
    for prio in range(5):
        for direction, filled in (("A->B", True), ("B->A", False)):
            pts = [r for r in sent if r["priority"] == prio and r["dir"] == direction and r["delivered"]]
            if not pts:
                continue
            c = PRIO_COLOR[prio]
            ax.scatter([r["send_t_s"] for r in pts], [r["latency_s"] for r in pts],
                       s=26, marker=PRIO_MARKER[prio], facecolors=c if filled else "white",
                       edgecolors=c, linewidths=1.2, zorder=3)
    lost = [r for r in sent if not r["delivered"]]
    ymax = top
    if lost:
        ymax = top * 1.16
        ax.axhline(top * 1.02, color=GRID, linewidth=0.8, zorder=1)
        ax.scatter([r["send_t_s"] for r in lost], [top * 1.08] * len(lost), marker="x", s=34,
                   color=LOST, linewidths=1.4, zorder=3)
        ax.text(0.0, top * 1.12, "sent, never received", fontsize=7, color=LOST, va="bottom")
    rej = [r for r in sent if r["delivered"] and r["recv_result"] == "REJECT"]
    if rej:
        ax.scatter([r["send_t_s"] for r in rej], [r["latency_s"] for r in rej], s=90,
                   marker="o", facecolors="none", edgecolors=LOST, linewidths=1.2, zorder=4)
    ax.set_ylim(min(0, min(lat + [0]) * 1.1), ymax)
    if duration_s:
        ax.set_xlim(-1, duration_s + 1)
    ax.set_xlabel("send time (s after traffic origin)", fontsize=9, color=INK)
    ax.set_ylabel("latency, Timestamp to receipt (s)", fontsize=9, color=INK)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    handles = [Line2D([], [], marker=PRIO_MARKER[p], linestyle="", markerfacecolor=PRIO_COLOR[p],
                      markeredgecolor=PRIO_COLOR[p], label=PRIO_NAME[p]) for p in range(5)
               if any(r["priority"] == p for r in sent)]
    handles += [Line2D([], [], marker="o", linestyle="", markerfacecolor=INK2,
                       markeredgecolor=INK2, label="rover → Earth"),
                Line2D([], [], marker="o", linestyle="", markerfacecolor="white",
                       markeredgecolor=INK2, label="Earth → rover")]
    if rej:
        handles.append(Line2D([], [], marker="o", linestyle="", markersize=9,
                              markerfacecolor="none", markeredgecolor=LOST,
                              label="received, rejected"))
    if lost:
        handles.append(Line2D([], [], marker="x", linestyle="", color=LOST,
                              label="sent, never received"))
    if any(o["kind"] == "scheduled" for o in outages):
        handles.append(Patch(facecolor=OUTAGE, label="scheduled outage"))
    if any(o["kind"] == "unscheduled" for o in outages):
        handles.append(Patch(facecolor="none", edgecolor="#b8b7b2", hatch="///",
                             label="unscheduled outage"))
    ax.legend(handles=handles, fontsize=7, frameon=False, loc="upper left",
              bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def latency_by_priority(rows, title, path):
    if not AVAILABLE:
        return None
    got = [r for r in rows if r["delivered"] and r["latency_s"] is not None]
    if not got:
        return None
    fig, ax = plt.subplots(figsize=(7.0, 3.6), dpi=120)
    _style(ax)
    for prio in range(5):
        for k, (direction, filled) in enumerate((("A->B", True), ("B->A", False))):
            pts = sorted(r["latency_s"] for r in got if r["priority"] == prio and r["dir"] == direction)
            if not pts:
                continue
            n = len(pts)
            xs = [prio + (k - 0.5) * 0.36 + ((i % 7) - 3) * 0.025 for i in range(n)]
            c = PRIO_COLOR[prio]
            ax.scatter(xs, pts, s=18, marker=PRIO_MARKER[prio], facecolors=c if filled else "white",
                       edgecolors=c, linewidths=1.0, zorder=3)
            med = pts[n // 2] if n % 2 else (pts[n // 2 - 1] + pts[n // 2]) / 2
            x0 = prio + (k - 0.5) * 0.36
            ax.plot([x0 - 0.13, x0 + 0.13], [med, med], color=INK, linewidth=1.6, zorder=4)
    ax.set_xticks(range(5))
    ax.set_xticklabels([PRIO_NAME[p] for p in range(5)], fontsize=8)
    ax.set_ylabel("latency (s)", fontsize=9, color=INK)
    ax.set_ylim(bottom=0)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    handles = [Line2D([], [], marker="o", linestyle="", markerfacecolor=INK2, markeredgecolor=INK2,
                      label="rover → Earth"),
               Line2D([], [], marker="o", linestyle="", markerfacecolor="white",
                      markeredgecolor=INK2, label="Earth → rover"),
               Line2D([], [], color=INK, linewidth=1.6, label="median")]
    ax.legend(handles=handles, fontsize=7, frameon=False, loc="upper left",
              bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def arrivals(rows, title, path, duration_s=None):
    """Send and receive times per priority: one row per priority, a line from
    each envelope's send time to its receive time, so overtaking is visible."""
    if not AVAILABLE:
        return None
    got = [r for r in rows if r["delivered"] and r["dir"] == "A->B"]
    if not got:
        return None
    prios = sorted({r["priority"] for r in got})
    fig, ax = plt.subplots(figsize=(9.0, 3.4), dpi=120)
    _style(ax)
    ax.grid(False, axis="y")
    for i, p in enumerate(prios):
        pts = sorted((r for r in got if r["priority"] == p), key=lambda r: r["send_t_s"])
        y0 = len(prios) - 1 - i
        for k, r in enumerate(pts):
            dy = ((k % 5) - 2) * 0.07
            ax.plot([r["send_t_s"], r["recv_t_s"]], [y0 + dy, y0 + dy], color=PRIO_COLOR[p],
                    linewidth=0.8, alpha=0.6, zorder=2)
            ax.scatter([r["recv_t_s"]], [y0 + dy], s=22, marker=PRIO_MARKER[p],
                       color=PRIO_COLOR[p], zorder=3)
            ax.scatter([r["send_t_s"]], [y0 + dy], s=8, marker="|", color=INK2, zorder=3)
    ax.set_yticks(range(len(prios)))
    ax.set_yticklabels([PRIO_NAME[p] for p in reversed(prios)], fontsize=8)
    ax.set_ylim(-0.6, len(prios) - 0.4)
    if duration_s:
        ax.set_xlim(-1, duration_s + 1)
    ax.set_xlabel("time (s after traffic origin)", fontsize=9, color=INK)
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    handles = [Line2D([], [], marker="|", linestyle="", color=INK2, label="sent"),
               Line2D([], [], marker="o", linestyle="", color=INK2, label="received at Earth")]
    ax.legend(handles=handles, fontsize=7, frameon=False, loc="upper left",
              bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def make(run, outdir):
    """Charts for one RunData; returns {name: relative path}."""
    if not AVAILABLE:
        return {}
    out = {}
    os.makedirs(outdir, exist_ok=True)
    rows = run.envelopes
    sid = run.id
    dur = run.scn.get("duration_s")
    p = timeline(rows, run.scn.get("outages", []),
                 "%s: latency of each envelope against its send time" % sid,
                 os.path.join(outdir, "%s-timeline.png" % sid), dur)
    if p:
        out["timeline"] = p
    p = latency_by_priority(rows, "%s: latency by priority" % sid,
                            os.path.join(outdir, "%s-latency.png" % sid))
    if p:
        out["latency"] = p
    if "arrivals" in run.scn.get("charts", []):
        p = arrivals(rows, "%s: rover envelopes from send (|) to receipt at Earth, by priority" % sid,
                     os.path.join(outdir, "%s-arrivals.png" % sid), dur)
        if p:
            out["arrivals"] = p
    return out
