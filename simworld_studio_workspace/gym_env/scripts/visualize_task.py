"""Generate PointNav task and visualize on 2D top-down map.

Usage:
    cd simworld_studio_workspace
    python -m gym_env.scripts.visualize_task --ucv-port 9002
"""
import os, sys, math, json, argparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_GYM_DIR = os.path.dirname(_SCRIPT_DIR)
_WORKSPACE_DIR = os.path.dirname(_GYM_DIR)
_PROJECT_ROOT = os.path.dirname(_WORKSPACE_DIR)

# Ensure imports work
sys.path.insert(0, _WORKSPACE_DIR)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np

from gym_env.ucv_client import UCVClient
from gym_env.episode_builder import (
    sample_pointnav_episode_navmesh,
    sample_objectnav_episode_navmesh,
)


def draw_scene(ax, scene_graph_file):
    """Draw buildings and trees from scene graph."""
    with open(scene_graph_file) as f:
        sg = json.load(f)
    items = sg if isinstance(sg, list) else sg.get("elements", [])

    for item in items:
        center = item.get("center", {})
        size = item.get("size", {})
        name = item.get("name", "")
        cls = item.get("class", "")
        cx = float(center.get("x", 0))
        cy = float(center.get("y", 0))
        w = float(size.get("width", 0))
        h = float(size.get("height", 0))
        if w <= 0 or h <= 0:
            continue
        # Skip huge floor / sky actors
        if cls in ("StaticMeshActor", "Floor_C", "NavMeshBoundsVolume",
                    "RecastNavMesh"):
            continue

        if "building" in name.lower() or "Building" in cls:
            color = "#cc4444"
            alpha = 0.45
            ax.add_patch(patches.Rectangle(
                (cx - w/2, cy - h/2), w, h,
                linewidth=0.5, edgecolor="#993333",
                facecolor=color, alpha=alpha,
            ))
            ax.text(cx, cy, name.split("_")[-1], ha="center", va="center",
                    fontsize=6, color="#993333", alpha=0.7)
        elif "tree" in name.lower() or "Tree" in cls:
            circle = patches.Circle(
                (cx, cy), w/2,
                linewidth=0.5, edgecolor="#338833",
                facecolor="#44aa44", alpha=0.35,
            )
            ax.add_patch(circle)


def draw_episode(ax, label, result, color, show_waypoints=True):
    """Draw one episode: GT path, start, goal, heading arrow."""
    ep = result["episode"]
    wps = result["gt_path_waypoints"]
    diff = result["difficulty"]

    sx, sy = ep.start_position.x, ep.start_position.y
    gx, gy = ep.goal_position.x, ep.goal_position.y
    heading = result["start_heading_deg"]

    # GT path
    if wps:
        xs = [p[0] for p in wps]
        ys = [p[1] for p in wps]
        ax.plot(xs, ys, '-', color=color, linewidth=2.5, alpha=0.85,
                label=f"{label} d={diff['distance_m']:.0f}m r={diff['detour_ratio']:.2f} diff={diff['difficulty_score']:.2f}")
        if show_waypoints:
            ax.plot(xs, ys, '.', color=color, markersize=3, alpha=0.5)

    # Start → heading arrow
    ax.plot(sx, sy, 'o', color=color, markersize=12, zorder=5,
            markeredgecolor="white", markeredgewidth=1.5)
    dx = 400 * math.cos(math.radians(heading))
    dy = 400 * math.sin(math.radians(heading))
    ax.annotate("", xy=(sx+dx, sy+dy), xytext=(sx, sy),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=2.5),
                zorder=6)

    # Goal star
    ax.plot(gx, gy, '*', color=color, markersize=18, zorder=5,
            markeredgecolor="white", markeredgewidth=0.5)

    # Dashed straight line
    ax.plot([sx, gx], [sy, gy], '--', color=color, linewidth=1, alpha=0.25)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ucv-port", type=int, default=9002)
    p.add_argument("--scene-graph", default=os.path.join(_PROJECT_ROOT, "test_map_scene_graph.json"))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default=os.path.join(_SCRIPT_DIR, "demo_pointnav_task.png"))
    p.add_argument("--n-episodes", type=int, default=3,
                   help="Number of PointNav episodes to generate at different difficulties")
    args = p.parse_args()

    ucv = UCVClient(host="127.0.0.1", port=args.ucv_port, name="viz")
    ucv.connect()
    print("Connected to UCV")

    # Build navmesh
    from nav_task.navmesh_interface import NavmeshNavigationInterface
    nav = NavmeshNavigationInterface(ucv)
    print("Building navmesh...")
    resp = nav.build_navmesh(padding_cm=500.0)
    print(f"NavMesh built: {resp}")

    # Generate episodes at different difficulty levels
    difficulty_levels = [
        ("Easy (10-20m)", 1000, 2000),
        ("Medium (20-30m)", 2000, 3000),
        ("Hard (30-50m)", 3000, 5000),
    ]
    colors = ["#0066ff", "#ff6600", "#00cc66", "#cc00ff", "#ffcc00",
              "#ff3399", "#6633cc", "#33cccc"]

    episodes = []
    for i, (label, min_d, max_d) in enumerate(difficulty_levels[:args.n_episodes]):
        try:
            result = sample_pointnav_episode_navmesh(
                ucv,
                seed=args.seed, idx=i,
                min_geodesic_cm=min_d, max_geodesic_cm=max_d,
                build_navmesh=False, nav_interface=nav,
            )
            ep = result["episode"]
            diff = result["difficulty"]
            print(f"  {label}: start=({ep.start_position.x:.0f},{ep.start_position.y:.0f}) "
                  f"goal=({ep.goal_position.x:.0f},{ep.goal_position.y:.0f}) "
                  f"geo={diff['distance_m']:.0f}m detour={diff['detour_ratio']:.2f}")
            episodes.append((label, result))
        except Exception as e:
            print(f"  {label}: FAILED - {e}")

    ucv.disconnect()

    if not episodes:
        print("No episodes generated!")
        return

    # Plot
    fig, ax = plt.subplots(1, 1, figsize=(14, 14))
    draw_scene(ax, args.scene_graph)
    for i, (label, result) in enumerate(episodes):
        draw_episode(ax, label, result, colors[i % len(colors)])

    ax.set_xlim(-12000, 12000)
    ax.set_ylim(-12000, 12000)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.15, linestyle='--')
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.set_title("Task Generation: GT Paths with Obstacle Avoidance", fontsize=14, fontweight="bold")
    ax.set_xlabel("X (cm)", fontsize=11)
    ax.set_ylabel("Y (cm)", fontsize=11)

    plt.tight_layout()
    plt.savefig(args.output, dpi=150, bbox_inches="tight")
    print(f"\nSaved: {args.output}")


if __name__ == "__main__":
    main()
