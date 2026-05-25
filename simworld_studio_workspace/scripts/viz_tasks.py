"""Task visualization — pure navmesh, zero scene graph dependency.

All data comes directly from UE via UnrealCV:
  - Navigable points: vget /nav/random_points
  - Paths: vget /nav/path
  - Start/Goal: sampled from navmesh random points
  - Obstacle shapes: vget /object/{name}/bounds (real AABB from UE)
"""
import os, sys, math, time, random

_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_root, "simworld_studio_workspace"))

from gym_env.ucv_client import UCVClient
from nav_task.episode import Position

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.lines import Line2D
import numpy as np

UCV_PORT = 9002
OUT_DIR = os.path.dirname(__file__)


# ---------------------------------------------------------------------------
# UE data fetching (all from UnrealCV, zero scene graph)
# ---------------------------------------------------------------------------

def navmesh_random_points(ucv, count):
    resp = ucv.send(f"vget /nav/random_points {count}").strip()
    parts = resp.split("|")
    n = int(parts[0])
    pts = []
    for p in parts[1:n+1]:
        c = p.split(",")
        if len(c) >= 3:
            pts.append(Position(x=float(c[0]), y=float(c[1]), node_type="navmesh"))
    return pts


def navmesh_path(ucv, start, goal):
    """Returns (length_cm, [(x,y), ...]) or (None, [])."""
    resp = ucv.send(f"vget /nav/path {start.x} {start.y} 0 {goal.x} {goal.y} 0").strip()
    if resp == "-1":
        return None, []
    parts = resp.split("|")
    length = float(parts[0])
    wps = []
    for p in parts[1:]:
        c = p.split(",")
        if len(c) >= 3:
            wps.append((float(c[0]), float(c[1])))
    return length, wps


def get_real_obstacles(ucv):
    """Fetch real AABB bounds for every House/Tree from UE.

    House bounds are shrunk by 15% because the AABB includes roof
    overhang that the navmesh collision hull does not.
    """
    objs = ucv.vget_objects()
    obstacles = []
    for name in objs:
        is_house = "House" in name
        is_tree = "Tree" in name
        if not (is_house or is_tree):
            continue
        try:
            b = ucv.send(f"vget /object/{name}/bounds").strip().split()
            if len(b) < 6:
                continue
            minx, miny, minz = float(b[0]), float(b[1]), float(b[2])
            maxx, maxy, maxz = float(b[3]), float(b[4]), float(b[5])
            cx, cy = (minx+maxx)/2, (miny+maxy)/2
            w, h = maxx - minx, maxy - miny
            # Shrink house bounds — AABB includes roof overhang
            if is_house:
                w *= 0.85
                h *= 0.85
            obstacles.append({
                "name": name,
                "type": "house" if is_house else "tree",
                "minx": cx - w/2, "miny": cy - h/2,
                "maxx": cx + w/2, "maxy": cy + h/2,
                "cx": cx, "cy": cy,
                "w": w, "h": h,
                "height": maxz - minz,
            })
        except Exception:
            continue
    return obstacles


# ---------------------------------------------------------------------------
# Task sampling (pure navmesh)
# ---------------------------------------------------------------------------

def compute_difficulty(eucl, geo, heading, target_angle):
    detour = geo / max(eucl, 1)
    heading_off = abs(((heading - target_angle + 180) % 360) - 180)
    d_norm = min(geo / 10000, 1.0)
    det_norm = min(max(detour - 1, 0) / 1.0, 1.0)
    h_norm = heading_off / 180.0
    score = 0.4 * d_norm + 0.35 * det_norm + 0.25 * h_norm
    return {
        "distance_m": geo / 100,
        "detour_ratio": round(detour, 2),
        "heading_offset_deg": round(heading_off, 1),
        "difficulty_score": round(score, 2),
    }


def sample_pointnav(ucv, positions, rng, min_geo, max_geo, max_attempts=500):
    for _ in range(max_attempts):
        s = rng.choice(positions)
        g = rng.choice(positions)
        eucl = math.hypot(s.x - g.x, s.y - g.y)
        if eucl < min_geo * 0.3 or eucl > max_geo * 2.5:
            continue
        geo, wps = navmesh_path(ucv, s, g)
        if geo is None or not (min_geo <= geo <= max_geo):
            continue
        if len(wps) < 2:
            continue
        heading = rng.uniform(0, 360)
        ta = math.degrees(math.atan2(g.y - s.y, g.x - s.x))
        diff = compute_difficulty(eucl, geo, heading, ta)
        return {"start": s, "goal": g, "waypoints": wps,
                "heading": heading, "geo": geo, "eucl": eucl, "difficulty": diff}
    return None


def sample_objectnav(ucv, positions, rng, target_name, target_pos, max_attempts=200):
    for _ in range(max_attempts):
        s = rng.choice(positions)
        eucl = math.hypot(s.x - target_pos.x, s.y - target_pos.y)
        if eucl < 500:
            continue
        geo, wps = navmesh_path(ucv, s, target_pos)
        if geo is None or geo < 500 or len(wps) < 2:
            continue
        heading = rng.uniform(0, 360)
        ta = math.degrees(math.atan2(target_pos.y - s.y, target_pos.x - s.x))
        diff = compute_difficulty(eucl, geo, heading, ta)
        dirs = ["north","northeast","east","southeast","south","southwest","west","northwest"]
        di = int(((ta + 22.5) % 360) / 45)
        prompt = (f"Find and navigate to {target_name}. "
                  f"It is roughly to the {dirs[di]} of your starting position, "
                  f"approximately {geo/100:.0f}m away. You must get within 2m of it.")
        return {"start": s, "goal": target_pos, "waypoints": wps,
                "heading": heading, "geo": geo, "eucl": eucl, "difficulty": diff,
                "target_actor": target_name, "prompt": prompt, "direction": dirs[di]}
    return None


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def draw_obstacles(ax, obstacles):
    """Draw Houses as rectangles, Trees as circles — real bounds from UE."""
    for ob in obstacles:
        if ob["type"] == "house":
            rect = patches.FancyBboxPatch(
                (ob["minx"], ob["miny"]), ob["w"], ob["h"],
                boxstyle="round,pad=0", linewidth=0.8,
                edgecolor="#994444", facecolor="#cc4444", alpha=0.3,
            )
            ax.add_patch(rect)
            ax.text(ob["cx"], ob["cy"], ob["name"], fontsize=10,
                    ha='center', va='center', color='#994444', alpha=0.5)
        elif ob["type"] == "tree":
            # Trees bounds are canopy projection, not trunk — shrink 50%
            r = (ob["w"] + ob["h"]) / 4 * 0.5
            circle = plt.Circle((ob["cx"], ob["cy"]), r,
                                linewidth=0.6, edgecolor="#337733",
                                facecolor="#44aa44", alpha=0.3)
            ax.add_patch(circle)


def draw_episode(ax, ep, color, label, show_prompt=False):
    wps = ep["waypoints"]
    sx, sy = ep["start"].x, ep["start"].y
    gx, gy = ep["goal"].x, ep["goal"].y

    # Navmesh path (thick line with waypoint dots)
    if wps:
        xs = [p[0] for p in wps]
        ys = [p[1] for p in wps]
        ax.plot(xs, ys, '-', color=color, linewidth=2.5, alpha=0.85, zorder=3)
        ax.plot(xs, ys, '.', color=color, markersize=4, alpha=0.5, zorder=3)

    # Straight-line dashed
    ax.plot([sx, gx], [sy, gy], '--', color=color, linewidth=0.8, alpha=0.2)

    # Start (circle) + heading arrow — big and bold
    ax.plot(sx, sy, 'o', color=color, markersize=24, zorder=5,
            markeredgecolor='white', markeredgewidth=3)
    dx = 1000 * math.cos(math.radians(ep["heading"]))
    dy = 1000 * math.sin(math.radians(ep["heading"]))
    ax.annotate("", xy=(sx+dx, sy+dy), xytext=(sx, sy),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=5,
                                mutation_scale=40), zorder=5)

    # Goal (star)
    ax.plot(gx, gy, '*', color=color, markersize=36, zorder=5,
            markeredgecolor='white', markeredgewidth=2)


MAP_LIM = 8000  # fixed axis range

def setup_ax(ax, title, obstacles):
    draw_obstacles(ax, obstacles)
    ax.set_xlim(-MAP_LIM, MAP_LIM)
    ax.set_ylim(-MAP_LIM, MAP_LIM)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.12, linestyle='--')
    ax.set_title(title, fontsize=28, fontweight='bold', pad=14)
    ax.set_xlabel("X (cm)", fontsize=20)
    ax.set_ylabel("Y (cm)", fontsize=20)
    ax.tick_params(labelsize=16)


def make_legend():
    return [
        Line2D([0],[0], marker='o', color='gray', markerfacecolor='gray',
               markersize=8, linestyle='None', label='Start'),
        Line2D([0],[0], marker='*', color='gray', markerfacecolor='gray',
               markersize=12, linestyle='None', label='Goal'),
        Line2D([0],[0], color='gray', linewidth=2.5, label='NavMesh Path'),
        Line2D([0],[0], color='gray', linewidth=0.8, linestyle='--',
               alpha=0.4, label='Euclidean'),
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ucv = UCVClient(port=UCV_PORT, name="viz")
    ucv.connect()
    print("Connected")

    print("Building navmesh...")
    ucv.send("vset /nav/build -12000 -12000 -1000 12000 12000 3000")
    time.sleep(20)
    print(f"NavMesh: {ucv.send('vget /nav/status')}")

    # Sample many navmesh points, then filter to the city core
    # (where buildings are) to force paths through dense areas.
    CITY_CORE = 8000  # houses are within ±6000, trees within ±7000
    print(f"Sampling navmesh points (city core ±{CITY_CORE}cm)...")
    raw_positions = navmesh_random_points(ucv, 2000)
    positions = [p for p in raw_positions
                 if -CITY_CORE < p.x < CITY_CORE and -CITY_CORE < p.y < CITY_CORE]
    print(f"  {len(positions)}/{len(raw_positions)} points in city core")

    print("Fetching obstacle bounds from UE...")
    obstacles = get_real_obstacles(ucv)
    houses = [o for o in obstacles if o["type"] == "house"]
    trees = [o for o in obstacles if o["type"] == "tree"]
    print(f"  {len(houses)} houses, {len(trees)} trees")

    # PointNav
    print("\n--- PointNav ---")
    diffs = [
        ("Easy",   500,  2000,  "#2196F3"),
        ("Medium", 2000, 6000,  "#FF9800"),
        ("Hard",   6000, 25000, "#F44336"),
    ]
    N_PER_DIFF = 5

    pointnav = {}
    for name, lo, hi, color in diffs:
        pointnav[name] = []
        for j in range(N_PER_DIFF):
            rng = random.Random(42 + j*17 + hash(name) % 97)
            ep = sample_pointnav(ucv, positions, rng, lo, hi)
            if ep:
                pointnav[name].append(ep)
                d = ep["difficulty"]
                print(f"  {name} #{j+1}: {d['distance_m']:.0f}m  detour={d['detour_ratio']}  wps={len(ep['waypoints'])}")
            else:
                print(f"  {name} #{j+1}: FAILED")

    # ObjectNav
    print("\n--- ObjectNav ---")
    tree_actors = [n for n in ucv.vget_objects() if n.startswith("Tree")]
    objectnav = []
    for j, tn in enumerate(tree_actors[:5]):
        loc = ucv.vget_location(tn)
        tp = Position(x=loc[0], y=loc[1], node_type="navmesh")
        rng = random.Random(200 + j * 11)
        ep = sample_objectnav(ucv, positions, rng, tn, tp)
        if ep:
            objectnav.append(ep)
            d = ep["difficulty"]
            print(f"  {tn}: {d['distance_m']:.0f}m  wps={len(ep['waypoints'])}  dir={ep['direction']}")
            print(f"    Prompt: {ep['prompt']}")
        else:
            print(f"  {tn}: FAILED")

    # ==========================================================
    # PLOT 1: PointNav by difficulty (no legend — clean)
    # ==========================================================
    print("\nPlot 1: PointNav...")
    fig, axes = plt.subplots(1, 3, figsize=(36, 12))
    for idx, (name, lo, hi, color) in enumerate(diffs):
        ax = axes[idx]
        setup_ax(ax, f"PointNav — {name} ({lo/100:.0f}–{hi/100:.0f}m)", obstacles)
        for j, ep in enumerate(pointnav.get(name, [])):
            draw_episode(ax, ep, color, f"Ep{j+1}")
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "viz_pointnav_by_difficulty.png")
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  {out}")

    # ==========================================================
    # PLOT 2: ObjectNav — single episode, map left + prompt right
    # ==========================================================
    print("Plot 2: ObjectNav...")
    if objectnav:
        ep = objectnav[0]  # pick best one
        c = "#9C27B0"
        fig = plt.figure(figsize=(28, 14))
        # Left: map
        ax = fig.add_axes([0.02, 0.05, 0.55, 0.88])
        setup_ax(ax, f"ObjectNav — {ep['target_actor']}", obstacles)
        draw_episode(ax, ep, c, ep['target_actor'])
        gx, gy = ep["goal"].x, ep["goal"].y
        ax.add_patch(plt.Circle((gx, gy), 200, color=c, fill=False,
                                linewidth=3, linestyle='--', alpha=0.7, zorder=4))
        ax.annotate("TARGET", xy=(gx, gy), fontsize=20, color=c, fontweight='bold',
                    ha='center', xytext=(0, 35), textcoords='offset points', zorder=6)
        # Right: prompt text box
        import textwrap
        prompt_lines = textwrap.fill(ep["prompt"], width=45)
        d = ep["difficulty"]
        info_text = (
            f"Target: {ep['target_actor']}\n"
            f"Direction: {ep['direction']}\n"
            f"Distance: {d['distance_m']:.0f}m (geodesic)\n"
            f"Detour ratio: {d['detour_ratio']:.2f}\n"
            f"Difficulty score: {d['difficulty_score']:.2f}\n"
            f"Waypoints: {len(ep['waypoints'])}\n"
            f"\n--- Agent Prompt ---\n\n"
            f"{prompt_lines}"
        )
        ax_text = fig.add_axes([0.60, 0.05, 0.38, 0.88])
        ax_text.axis('off')
        ax_text.text(0.05, 0.95, info_text, transform=ax_text.transAxes,
                     fontsize=20, va='top', fontfamily='monospace',
                     bbox=dict(boxstyle='round,pad=0.8', facecolor='lightyellow',
                               edgecolor=c, alpha=0.9, linewidth=2))
        out = os.path.join(OUT_DIR, "viz_objectnav_with_prompts.png")
        plt.savefig(out, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"  {out}")

    # ==========================================================
    # PLOT 3: All PointNav tasks — legend in right margin
    # ==========================================================
    print("Plot 3: All PointNav tasks...")
    fig, ax = plt.subplots(1, 1, figsize=(22, 20))
    setup_ax(ax, "PointNav Task Generation: NavMesh GT Paths", obstacles)
    # Widen right margin for legend
    ax.set_xlim(-MAP_LIM, MAP_LIM + 2500)
    diff_colors = {"Easy": "#2196F3", "Medium": "#FF9800", "Hard": "#F44336"}
    for dn in ["Easy", "Medium", "Hard"]:
        for j, ep in enumerate(pointnav.get(dn, [])[:3]):
            draw_episode(ax, ep, diff_colors[dn], "")
    # Arrow meaning
    legend_handles = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor='gray',
               markersize=16, label='Start position'),
        Line2D([0], [0], marker='*', color='w', markerfacecolor='gray',
               markersize=20, label='Goal position'),
        Line2D([0], [0], color='gray', linewidth=3, label='NavMesh path'),
        Line2D([0], [0], color='gray', linewidth=1, linestyle='--',
               alpha=0.4, label='Euclidean (straight)'),
        Line2D([0], [0], marker='>', color='gray', markersize=12,
               linestyle='None', label='Start heading (arrow)'),
        Line2D([0], [0], color='w', linewidth=0, label=''),  # spacer
        Line2D([0], [0], color="#2196F3", linewidth=4, label='Easy (5–20m)'),
        Line2D([0], [0], color="#FF9800", linewidth=4, label='Medium (20–60m)'),
        Line2D([0], [0], color="#F44336", linewidth=4, label='Hard (60–250m)'),
    ]
    ax.legend(handles=legend_handles, loc='lower right', fontsize=16,
              framealpha=0.95, edgecolor='#666666',
              bbox_to_anchor=(1.0, 0.0))
    plt.tight_layout()
    out = os.path.join(OUT_DIR, "viz_all_tasks.png")
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  {out}")

    ucv.disconnect()
    print("\nDONE")


if __name__ == "__main__":
    main()
