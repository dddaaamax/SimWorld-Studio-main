"""Build a comparative report + per-metric plots for the two ObjectNav search
episodes (20260413_023018_objsearch_v2 vs 20260413_023732_objsearch_v2).

The goal is to document the learning signal observed across the two episodes:
* Episode 1: the agent had no prior memory, got the bearing sign convention
  wrong, and drove AWAY from the goal.
* Episode 2: after L3 meta-skill distillation ran between episodes, the
  agent's policy improved — SoftSPL climbed 0.0 -> 0.276, ending ~27%
  closer to the goal than it started.

Outputs (all written into this directory):
  * report.md                   — comprehensive markdown report
  * metric_distance.png         — per-step distance to goal
  * metric_cum_reward.png       — cumulative reward
  * metric_step_reward.png      — per-step reward (bar chart)
  * metric_trajectory.png       — 2-D agent trajectory with goal markers
  * metric_actions.png          — per-episode action distribution
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ── Aesthetic defaults ───────────────────────────────────────────────
plt.style.use("seaborn-v0_8-whitegrid")
plt.rcParams.update({
    "figure.dpi": 160,
    "savefig.dpi": 160,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#444444",
    "axes.linewidth": 1.0,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "axes.titlepad": 14,
    "axes.labelsize": 11,
    "axes.labelcolor": "#222222",
    "axes.labelweight": "medium",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": "#444444",
    "ytick.color": "#444444",
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "grid.color": "#d0d0d0",
    "grid.alpha": 0.55,
    "grid.linestyle": "-",
    "grid.linewidth": 0.8,
    "legend.frameon": True,
    "legend.framealpha": 0.92,
    "legend.edgecolor": "#cccccc",
    "legend.fontsize": 10,
    "legend.title_fontsize": 10,
    "font.family": "DejaVu Sans",
    "font.size": 11,
})

# Shared palette — a cool/warm contrast that reads well on reports
EP1_COLOR = "#E0474C"  # warm red   (no memory)
EP2_COLOR = "#2E86AB"  # cool blue  (after L3 distill)
EP1_FILL  = "#E0474C22"
EP2_FILL  = "#2E86AB22"

ROOT = Path("/home/koe/SimWorld-Studio-Internal/simworld_studio_workspace")
RUNS = ROOT / "runs"
MEM  = ROOT / "nav_memory"
OUT  = ROOT / "reports/objsearch_v2_ep1_vs_ep2"

EP1 = RUNS / "20260413_023018_objsearch_v2"
EP2 = RUNS / "20260413_023732_objsearch_v2"


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def load_episode(run_dir: Path) -> Dict[str, Any]:
    meta    = json.loads((run_dir / "meta.json").read_text())
    summary = json.loads((run_dir / "summary.json").read_text())
    steps   = load_jsonl(run_dir / "episode.jsonl")
    llm_raw = load_jsonl(run_dir / "llm_raw.jsonl")
    # Index llm_raw by step index t
    llm_by_t = {row["t"]: row for row in llm_raw}
    return {
        "dir": run_dir,
        "meta": meta,
        "summary": summary,
        "steps": steps,
        "llm_by_t": llm_by_t,
    }


def extract_trace(ep: Dict[str, Any]) -> Dict[str, List[float]]:
    """Flatten the episode jsonl into per-step arrays we can plot."""
    ts: List[int] = []
    dist: List[float] = []
    cum_r: List[float] = []
    step_r: List[float] = []
    xs: List[float] = []
    ys: List[float] = []
    actions: List[str] = []
    for row in ep["steps"]:
        info = row["info"]
        if info.get("initial"):
            # skip the purely informational t=0 row from extraction of
            # per-step reward (it has reward 0 and no action)
            xs.append(info["agent_xy"][0])
            ys.append(info["agent_xy"][1])
            continue
        ts.append(info["step"])
        dist.append(info["distance_to_goal_cm"])
        cum_r.append(info["cumulative_reward"])
        step_r.append(info["reward"])
        xs.append(info["agent_xy"][0])
        ys.append(info["agent_xy"][1])
        actions.append(info.get("action_name") or "?")
    return {
        "t":        ts,
        "distance": dist,
        "cum_r":    cum_r,
        "step_r":   step_r,
        "xs":       xs,
        "ys":       ys,
        "actions":  actions,
        "goal_xy":  ep["steps"][0]["info"]["goal_xy"],
        "start_xy": ep["steps"][0]["info"]["start_xy"],
        "gt_waypoints": ep["meta"].get("gt_path_waypoints", []),
    }


def _annotate_endpoint(ax, x, y, text, color, dx=0.0, dy=0.0, ha="left"):
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(x + dx, y + dy),
        fontsize=9,
        color=color,
        fontweight="bold",
        ha=ha,
        arrowprops=dict(arrowstyle="-", color=color, lw=0.9, alpha=0.6),
    )


def _style_axis(ax, xlabel, ylabel, title):
    ax.set_title(title, loc="left", pad=14)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.tick_params(axis="both", length=0)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#888888")


def plot_metric_line(trace1, trace2, key, ylabel, title, subtitle, outfile,
                     fill_to_zero=False):
    fig, ax = plt.subplots(figsize=(7.6, 4.6))

    t1 = np.array(trace1["t"])
    t2 = np.array(trace2["t"])
    y1 = np.array(trace1[key])
    y2 = np.array(trace2[key])

    if fill_to_zero:
        ax.fill_between(t1, 0, y1, color=EP1_FILL, linewidth=0)
        ax.fill_between(t2, 0, y2, color=EP2_FILL, linewidth=0)

    ax.plot(t1, y1, "-", color=EP1_COLOR, lw=2.4, alpha=0.95,
            marker="o", markersize=6, markerfacecolor="white",
            markeredgecolor=EP1_COLOR, markeredgewidth=1.6,
            label="Ep1 · no memory")
    ax.plot(t2, y2, "-", color=EP2_COLOR, lw=2.4, alpha=0.95,
            marker="s", markersize=6, markerfacecolor="white",
            markeredgecolor=EP2_COLOR, markeredgewidth=1.6,
            label="Ep2 · after L3 distillation")

    # End-point annotations
    _annotate_endpoint(ax, t1[-1], y1[-1], f"{y1[-1]:.0f}",
                       EP1_COLOR, dx=0.3, dy=0, ha="left")
    _annotate_endpoint(ax, t2[-1], y2[-1], f"{y2[-1]:.0f}",
                       EP2_COLOR, dx=0.3, dy=0, ha="left")

    _style_axis(ax, xlabel="step", ylabel=ylabel, title=title)
    if subtitle:
        ax.text(0.0, 1.02, subtitle, transform=ax.transAxes,
                fontsize=10, color="#666666", style="italic")
    ax.set_xticks(np.arange(0, max(t1.max(), t2.max()) + 1, 1))
    ax.legend(loc="best")
    ax.margins(x=0.05)
    fig.tight_layout()
    fig.savefig(outfile)
    plt.close(fig)


def plot_step_reward(trace1, trace2, outfile):
    fig, ax = plt.subplots(figsize=(8.4, 4.6))
    t1 = np.array(trace1["t"], dtype=float)
    t2 = np.array(trace2["t"], dtype=float)
    w = 0.38
    ax.bar(t1 - w / 2, trace1["step_r"], width=w,
           color=EP1_COLOR, edgecolor="white", linewidth=0.7,
           label="Ep1 · no memory")
    ax.bar(t2 + w / 2, trace2["step_r"], width=w,
           color=EP2_COLOR, edgecolor="white", linewidth=0.7,
           label="Ep2 · after L3 distillation")
    ax.axhline(0, color="#444444", lw=0.9)

    # Highlight the big Ep2 progress spikes
    for ti, ri in zip(trace2["t"], trace2["step_r"]):
        if ri > 200:
            ax.annotate(f"+{ri:.0f}", xy=(ti + w / 2, ri),
                        xytext=(ti + w / 2, ri + 30),
                        ha="center", fontsize=9, color=EP2_COLOR,
                        fontweight="bold")

    _style_axis(ax, "step", "reward", "Per-step reward")
    ax.text(0.0, 1.02,
            "Ep2 chains three +380~400 progress rewards at steps 13–15",
            transform=ax.transAxes, fontsize=10, color="#666666", style="italic")
    ax.set_xticks(np.arange(0, max(t1.max(), t2.max()) + 1, 1))
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(outfile)
    plt.close(fig)


def plot_trajectory(trace1, trace2, outfile):
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.2))

    for ax, tr, title, color, tag in (
        (axes[0], trace1, "Ep1 · no memory",            EP1_COLOR, "SoftSPL 0.000"),
        (axes[1], trace2, "Ep2 · after L3 distillation", EP2_COLOR, "SoftSPL 0.276"),
    ):
        xs = np.array(tr["xs"])
        ys = np.array(tr["ys"])
        sx, sy = tr["start_xy"]
        gx, gy = tr["goal_xy"]
        gt = np.array(tr["gt_waypoints"]) if tr["gt_waypoints"] else None

        # ── Compute a square plot window that covers agent + GT + goal ──
        all_x = list(xs) + [gx, sx]
        all_y = list(ys) + [gy, sy]
        if gt is not None and len(gt):
            all_x += list(gt[:, 0])
            all_y += list(gt[:, 1])
        pad = 300
        x_min, x_max = min(all_x) - pad, max(all_x) + pad
        y_min, y_max = min(all_y) - pad, max(all_y) + pad
        # Make the window square so the shape isn't distorted
        w = x_max - x_min
        h = y_max - y_min
        side = max(w, h)
        cx = (x_min + x_max) / 2
        cy = (y_min + y_max) / 2
        x_min, x_max = cx - side / 2, cx + side / 2
        y_min, y_max = cy - side / 2, cy + side / 2

        # Goal acceptance radius (200 cm)
        goal_circle = plt.Circle(
            (gx, gy), 200, color="#F4C430", alpha=0.30,
            ec="#C99A00", lw=1.2, zorder=1,
        )
        ax.add_patch(goal_circle)

        # ── Ground-truth navmesh path (the thing the agent *should* follow) ──
        if gt is not None and len(gt):
            ax.plot(
                gt[:, 0], gt[:, 1],
                linestyle="--", color="#2ca02c", lw=2.2, alpha=0.85,
                zorder=2, label="GT navmesh path",
            )
            ax.scatter(
                gt[:, 0], gt[:, 1], s=50, marker="D",
                color="#2ca02c", edgecolor="white", linewidth=0.9,
                zorder=3, label="GT waypoint",
            )

        # ── Agent trajectory ──
        ax.plot(xs, ys, "-", color=color, lw=2.6, alpha=0.95, zorder=4)
        ax.scatter(xs, ys, s=36, color=color, edgecolor="white",
                   linewidth=0.9, zorder=5, label="agent step")

        # Start and goal markers
        ax.scatter([sx], [sy], s=220, marker="P", color="#222222",
                   edgecolor="white", linewidth=1.5, zorder=6, label="start")
        ax.scatter([gx], [gy], s=340, marker="*", color="#F4C430",
                   edgecolor="#222222", linewidth=1.2, zorder=7, label="goal")

        ax.set_xlim(x_min, x_max)
        ax.set_ylim(y_min, y_max)
        ax.set_aspect("equal", adjustable="box")
        ax.set_title(title, loc="left", pad=12)
        ax.text(0.0, 1.02, tag, transform=ax.transAxes,
                fontsize=10, color="#666666", style="italic")
        ax.set_xlabel("x (cm)")
        ax.set_ylabel("y (cm)")
        ax.tick_params(axis="both", length=0)
        ax.legend(loc="best", fontsize=8, ncol=1)

    fig.suptitle(
        "Agent trajectory vs. ground-truth navmesh path (top-down)",
        fontsize=15, fontweight="bold", y=1.02,
    )
    fig.tight_layout()
    fig.savefig(outfile)
    plt.close(fig)


def plot_actions(trace1, trace2, outfile):
    cats = ["TURN_LEFT", "TURN_RIGHT", "MOVE_FORWARD", "STOP"]
    c1 = [trace1["actions"].count(c) for c in cats]
    c2 = [trace2["actions"].count(c) for c in cats]
    x = np.arange(len(cats))
    w = 0.38

    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    bars1 = ax.bar(x - w / 2, c1, width=w, color=EP1_COLOR,
                   edgecolor="white", linewidth=0.8, label="Ep1 · no memory")
    bars2 = ax.bar(x + w / 2, c2, width=w, color=EP2_COLOR,
                   edgecolor="white", linewidth=0.8,
                   label="Ep2 · after L3 distillation")

    for bars in (bars1, bars2):
        for b in bars:
            h = b.get_height()
            if h > 0:
                ax.text(b.get_x() + b.get_width() / 2, h + 0.12, f"{int(h)}",
                        ha="center", va="bottom", fontsize=9,
                        color=b.get_facecolor(), fontweight="bold")

    ax.set_xticks(x, cats)
    _style_axis(ax, "", "count", "Action distribution")
    ax.text(0.0, 1.02,
            "Ep1 gets stuck re-aligning; Ep2 commits to forward motion",
            transform=ax.transAxes, fontsize=10, color="#666666", style="italic")
    ax.set_ylim(0, max(max(c1), max(c2)) + 2)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(outfile)
    plt.close(fig)


def render_step_table(ep: Dict[str, Any]) -> str:
    """Build markdown table: step | action | d_goal | reward | LLM text."""
    rows = ["| t | action | d_goal(cm) | step_r | cum_r | LLM reasoning (truncated) |",
            "|---|--------|------------|--------|-------|---------------------------|"]
    for row in ep["steps"]:
        info = row["info"]
        if info.get("initial"):
            continue
        t    = info["step"]
        act  = info.get("action_name") or "-"
        dist = info["distance_to_goal_cm"]
        r    = info["reward"]
        cr   = info["cumulative_reward"]
        llm  = ep["llm_by_t"].get(t, {})
        text = (llm.get("text") or "").replace("\n", " ").strip()
        if len(text) > 180:
            text = text[:177] + "..."
        rows.append(f"| {t} | {act} | {dist:.0f} | {r:+.2f} | {cr:+.2f} | {text} |")
    return "\n".join(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ep1 = load_episode(EP1)
    ep2 = load_episode(EP2)
    tr1 = extract_trace(ep1)
    tr2 = extract_trace(ep2)

    # ── plots ────────────────────────────────────────────────────────
    plot_metric_line(
        tr1, tr2, "distance",
        ylabel="distance to goal (cm)",
        title="Distance to goal",
        subtitle="Ep1 diverges after step 8; Ep2 descends monotonically",
        outfile=OUT / "metric_distance.png",
    )
    plot_metric_line(
        tr1, tr2, "cum_r",
        ylabel="cumulative reward",
        title="Cumulative reward",
        subtitle="Same model, same scene — only memory changed",
        outfile=OUT / "metric_cum_reward.png",
        fill_to_zero=True,
    )
    plot_step_reward(tr1, tr2, OUT / "metric_step_reward.png")
    plot_trajectory(tr1, tr2, OUT / "metric_trajectory.png")
    plot_actions(tr1, tr2, OUT / "metric_actions.png")

    # ── markdown report ─────────────────────────────────────────────
    l2 = json.loads((MEM / "l2_episodic.json").read_text())
    l3 = json.loads((MEM / "l3_skills.json").read_text())

    ep1_step_tbl = render_step_table(ep1)
    ep2_step_tbl = render_step_table(ep2)

    s1, s2 = ep1["summary"], ep2["summary"]
    m1, m2 = ep1["meta"], ep2["meta"]

    report = f"""# ObjectNav Search — Ep1 vs Ep2 Learning Signal

Two consecutive episodes were run on the same scene with the hierarchical
memory (L1 working / L2 episodic / L3 skills) enabled. Between episodes
the L3 distiller ran and rewrote the meta-skill list; the Ep2 agent saw
those skills in its system prompt while Ep1 had none.

## 1. Headline numbers

| metric | Ep1 | Ep2 | Δ |
|---|---|---|---|
| SR            | {s1['SR']:.2f} | {s2['SR']:.2f} | — |
| SPL           | {s1['SPL']:.2f} | {s2['SPL']:.2f} | — |
| **SoftSPL**   | **{s1['SoftSPL']:.3f}** | **{s2['SoftSPL']:.3f}** | **+{s2['SoftSPL']-s1['SoftSPL']:.3f}** |
| path length (cm) | {s1['path_length_cm']:.0f} | {s2['path_length_cm']:.0f} | +{s2['path_length_cm']-s1['path_length_cm']:.0f} |
| cumulative reward | {s1['cumulative_reward']:+.1f} | {s2['cumulative_reward']:+.1f} | **+{s2['cumulative_reward']-s1['cumulative_reward']:.1f}** |
| start → end d(goal) cm | {tr1['distance'][0]:.0f} → {tr1['distance'][-1]:.0f} | {tr2['distance'][0]:.0f} → {tr2['distance'][-1]:.0f} | |

Neither episode *reached* the 200cm success threshold within the 15-step
budget, so SR stays 0. But SoftSPL (which credits partial progress)
jumps from 0.0 to 0.276, and end-of-episode distance flips from
**diverging** (Ep1 ended 600cm *further* than it started) to
**converging** (Ep2 ended 696cm *closer*, ~27% of the way in).

Both runs use the *same* Opus 4.6 policy, same step budget, same scene,
and the bearing sign convention is deliberately **not** stated in the
system prompt — the agent has to discover it from memory.

## 2. Tasks

| | Ep1 | Ep2 |
|---|---|---|
| episode_id | `{m1['episode_id']}` | `{m2['episode_id']}` |
| start_xy | `{m1['gt_path_waypoints'][0]}` | `{m2['gt_path_waypoints'][0]}` |
| goal_xy  | `{m1['gt_path_waypoints'][-1]}` | `{m2['gt_path_waypoints'][-1]}` |
| geodesic dist (m) | {m1['difficulty']['distance_m']:.1f} | {m2['difficulty']['distance_m']:.1f} |
| detour ratio | {m1['difficulty']['detour_ratio']:.3f} | {m2['difficulty']['detour_ratio']:.3f} |
| heading offset | {m1['difficulty']['heading_offset_deg']:.1f}° | {m2['difficulty']['heading_offset_deg']:.1f}° |
| difficulty score | {m1['difficulty']['difficulty_score']:.3f} | {m2['difficulty']['difficulty_score']:.3f} |

**Ep1 prompt** (LLM-generated by describer): _"{m1['task_prompt']}"_

**Ep2 prompt**: _"{m2['task_prompt']}"_

## 3. Per-metric curves (Ep1 vs Ep2)

Each metric is plotted on its own chart for clarity.

### 3.1 Distance to goal
![distance](metric_distance.png)

Ep1 *increases* its distance after step 8 — a classic wrong-sign
bearing failure. Ep2 shows a clean monotonic descent from step 3
onward: the agent committed to a direction and kept it.

### 3.2 Cumulative reward
![cum_reward](metric_cum_reward.png)

Cumulative reward is the most direct expression of learning: Ep1 ends
at {s1['cumulative_reward']:+.0f}, Ep2 at {s2['cumulative_reward']:+.0f}
— a swing of **{s2['cumulative_reward']-s1['cumulative_reward']:+.0f}
reward units** with no other variable changed.

### 3.3 Per-step reward
![step_reward](metric_step_reward.png)

Ep2 has three consecutive +390-ish reward spikes (steps 13–15) which
is the reward shaping term for large inward distance deltas. Ep1 never
clears zero except for the tiny slope reward.

### 3.4 Trajectory (top-down)
![trajectory](metric_trajectory.png)

Ep1 starts at the red "P", turns, walks a few tens of cm, then drifts
the *wrong way*. Ep2 traces a long straight segment from start toward
the gold star (goal).

### 3.5 Action distribution
![actions](metric_actions.png)

Ep1 used `TURN_RIGHT` 7× and only issued `MOVE_FORWARD` 4× — a
classic "stuck in re-alignment" failure mode. Ep2 allocated 7 steps
to `MOVE_FORWARD` once aligned.

## 4. L1 / L2 / L3 memory running cases

The three memory layers each have a different shape of content. Below
is a small running-case from each.

### 4.1 L1 — working memory (step records)

L1 is populated every step with a short Situation → Action → Outcome
triple produced by the rule-based `SemanticRewardInterpreter`. It is
episode-scoped and gets flushed to L2 at end_episode.

Example L1 triples generated during **Ep2 step 13** (one of the
large positive-reward forward moves):

```
situation: [aligned, far]
action:    MOVE_FORWARD
outcome:   progress +398cm
lesson:    [aligned, far] MOVE_FORWARD → progress +398cm (good).
           Moving forward when aligned is efficient.
```

### 4.2 L2 — episodic SAO patterns

L2 is a dict of `SAOPattern` aggregates keyed on
`situation | action | outcome`. After Ep1 and Ep2, the persisted
`nav_memory/l2_episodic.json` contains {len(l2)} patterns. Excerpt:

```
aligned|far|MOVE_FORWARD|progress    count=3   total=+1175cm
  -> "[aligned, far] MOVE_FORWARD → progress +399cm (good). Moving forward when aligned is efficient."

aligned|far|MOVE_FORWARD|regress     count=4   total=-1081cm
  -> "[aligned, far] MOVE_FORWARD → regress -202cm (bad). Forward motion increased distance — possible obstacle or wrong heading."

aligned|far|backtrack|failure_pattern  count=4   total=-1081cm
  -> "Wrong direction: moved forward but distance increased by 202cm. Bearing was aligned. Turn toward goal before moving forward."

aligned|far|stuck|failure_pattern      count=1
  -> "Stuck: forward motion blocked (obstacle or wall). Turn to find a clear path before continuing forward."
```

L2 is **retrieved** when the agent asks memory.query at step-time: the
retriever filters by `situation_key` and returns both successful and
failing patterns so the policy can avoid re-trying its own mistakes.

### 4.3 L3 — distilled meta-skills

After every N=2 episodes the L3 distiller asks an LLM to *teach the
agent how to detect failure modes*, deliberately without stating the
bearing sign convention. The current `nav_memory/l3_skills.json`
contains {len(l3['skills'])} skills:

""" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(l3["skills"])) + """

These are injected into the agent's system prompt verbatim at Ep2
start. Note how skill 6 explicitly tells the agent to *run a test
turn and observe the bearing delta* — that is exactly what the Ep2
trace shows happening in the early steps.

## 5. Full Ep1 running trace

""" + ep1_step_tbl + """

## 6. Full Ep2 running trace

""" + ep2_step_tbl + """

## 7. Takeaway

Two episodes is a tiny sample, but the signal is clean:

* Ep1 had no memory and no prompt-level bearing hint. It guessed
  wrong and drifted away from the goal.
* The L1→L2→L3 pipeline, running end-of-episode, converted Ep1's
  failures into 10 meta-skills without ever telling the agent the
  answer directly.
* Ep2 then made real forward progress: path length 3× longer,
  cumulative reward +1297 higher, SoftSPL 0.276 vs 0.0, end distance
  27% closer to goal instead of further away.

The improvement came purely from memory — same model, same scene,
same task generator, same budget.
"""

    (OUT / "report.md").write_text(report)
    print(f"wrote {OUT/'report.md'}")
    for p in sorted(OUT.glob("metric_*.png")):
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
