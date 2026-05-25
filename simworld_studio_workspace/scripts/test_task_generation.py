"""Full task generation test: PointNav + ObjectNav with NavMesh.

Generates episodes, prints detailed info, and creates a matplotlib
visualization of GT paths overlaid on the scene graph.
"""
import os, sys, math, json, time

# Setup paths
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_root, "simworld_studio_workspace"))
sys.path.insert(0, os.path.join(_root, "simworld_studio_workspace"))

from gym_env.ucv_client import UCVClient
from gym_env.episode_builder import (
    sample_pointnav_episode_navmesh,
    sample_objectnav_episode_navmesh,
    snapshot_scene,
)

UCV_PORT = 9002
SCENE_GRAPH = "test_map_scene_graph.json"


def print_episode(label, result):
    ep = result["episode"]
    diff = result["difficulty"]
    wps = result["gt_path_waypoints"]

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Episode ID:    {ep.episode_id}")
    print(f"  Task type:     {ep.task_type}")
    print(f"  Start:         ({ep.start_position.x:.0f}, {ep.start_position.y:.0f})")
    print(f"  Goal:          ({ep.goal_position.x:.0f}, {ep.goal_position.y:.0f})")
    print(f"  Start heading: {result['start_heading_deg']}°")
    print(f"  Geodesic:      {ep.reference_path.shortest_path_length_cm:.0f} cm")
    eucl = math.sqrt((ep.start_position.x - ep.goal_position.x)**2 +
                     (ep.start_position.y - ep.goal_position.y)**2)
    print(f"  Euclidean:     {eucl:.0f} cm")
    print(f"  Detour ratio:  {diff['detour_ratio']}")
    print(f"  Heading offset:{diff['heading_offset_deg']}°")
    print(f"  Difficulty:    {diff['difficulty_score']}")
    print(f"  GT waypoints:  {len(wps)}")

    if "prompt" in result:
        print(f"  Target:        {result.get('target_actor_name', '?')}")
        print(f"  Description:   {result.get('object_description', '?')}")
        print(f"  Prompt:        {result['prompt']}")

    # Show first/last few waypoints
    if wps:
        print(f"  Path start:    ({wps[0][0]:.0f}, {wps[0][1]:.0f})")
        if len(wps) > 2:
            mid = len(wps) // 2
            print(f"  Path mid:      ({wps[mid][0]:.0f}, {wps[mid][1]:.0f})")
        print(f"  Path end:      ({wps[-1][0]:.0f}, {wps[-1][1]:.0f})")


def visualize_episodes(episodes, scene_graph_file, output_file="task_gen_viz.png"):
    """Plot episodes on scene graph."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
    except ImportError:
        print("matplotlib not available, skipping visualization")
        return

    # Load scene graph
    with open(scene_graph_file) as f:
        sg = json.load(f)
    items = sg if isinstance(sg, list) else sg.get("elements", sg.get("objects", []))

    fig, ax = plt.subplots(1, 1, figsize=(14, 14))

    # Draw obstacles
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
        if cls in ("StaticMeshActor", "Floor_C"):
            continue

        color = "#888888"
        alpha = 0.3
        if "building" in name.lower() or "house" in name.lower():
            color = "#cc4444"
            alpha = 0.5
        elif "tree" in name.lower():
            color = "#44aa44"
            alpha = 0.4

        rect = patches.Rectangle(
            (cx - w/2, cy - h/2), w, h,
            linewidth=0.5, edgecolor=color, facecolor=color, alpha=alpha
        )
        ax.add_patch(rect)

    # Draw each episode
    colors = ["#0066ff", "#ff6600", "#00cc66", "#cc00ff", "#ffcc00"]
    for i, (label, result) in enumerate(episodes):
        ep = result["episode"]
        wps = result["gt_path_waypoints"]
        color = colors[i % len(colors)]

        # GT path
        if wps:
            xs = [p[0] for p in wps]
            ys = [p[1] for p in wps]
            ax.plot(xs, ys, '-', color=color, linewidth=2, alpha=0.8, label=label)

        # Start point + heading arrow
        sx, sy = ep.start_position.x, ep.start_position.y
        heading = result["start_heading_deg"]
        ax.plot(sx, sy, 'o', color=color, markersize=10, zorder=5)
        dx = 300 * math.cos(math.radians(heading))
        dy = 300 * math.sin(math.radians(heading))
        ax.annotate("", xy=(sx+dx, sy+dy), xytext=(sx, sy),
                    arrowprops=dict(arrowstyle="->", color=color, lw=2))

        # Goal point
        gx, gy = ep.goal_position.x, ep.goal_position.y
        ax.plot(gx, gy, '*', color=color, markersize=15, zorder=5)

        # Straight line (dashed)
        ax.plot([sx, gx], [sy, gy], '--', color=color, linewidth=1, alpha=0.3)

        # Label
        diff = result["difficulty"]
        ax.annotate(
            f'{label}\nd={diff["distance_m"]:.0f}m r={diff["detour_ratio"]:.2f}',
            xy=(sx, sy), fontsize=7, color=color,
            xytext=(10, 10), textcoords='offset points'
        )

    ax.set_xlim(-12000, 12000)
    ax.set_ylim(-12000, 12000)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("NavMesh Task Generation: GT Paths + Scene Graph")
    ax.set_xlabel("X (cm)")
    ax.set_ylabel("Y (cm)")

    plt.tight_layout()
    plt.savefig(output_file, dpi=150)
    print(f"\nVisualization saved to {output_file}")


def main():
    # UnrealCV needs a live game world (PIE) for /nav/* and /objects
    # commands, so make sure PIE is running before any UCV queries.
    from gym_env.mcp_client import MCPClient
    mcp = MCPClient(port=55558, name="taskgen-mcp")
    try:
        mcp.start_pie(wait_seconds=8.0)
        print("PIE started")
    except Exception as exc:
        print(f"WARN: PIE start failed ({exc}); proceeding anyway")

    ucv = UCVClient(port=UCV_PORT, name="taskgen")
    ucv.connect()
    print("Connected to UnrealCV")

    # Build navmesh once
    print("Building navmesh...")
    ucv.send("vset /nav/build -11000 -11000 -1000 11000 11000 3000")
    time.sleep(20)
    print("NavMesh status:", ucv.send("vget /nav/status"))

    # Generate scene graph from current UE scene if missing/empty
    import json as _json
    try:
        with open(SCENE_GRAPH) as f:
            sg_data = _json.load(f)
        if not sg_data:
            raise ValueError("empty")
    except:
        print("Scene graph missing/empty, generating from UE...")
        actors = snapshot_scene(ucv)
        sg_items = []
        for name, (ax, ay, az) in actors:
            # Skip system actors
            if any(s in name for s in ["Camera", "HUD", "Debug", "Manager",
                   "Recast", "NavMesh", "WorldSettings", "GameMode",
                   "PlayerController", "DefaultPawn", "Spectator",
                   "GameState", "GameSession", "NetworkManager",
                   "SmartObject", "Chaos", "Replicator", "Brush",
                   "Buoyancy", "MassVisualizer", "Fog", "Light",
                   "Sky", "Cloud", "Atmosphere", "Unreal"]):
                continue
            sg_items.append({
                "name": name,
                "class": name.split("_")[0] if "_" in name else "Actor",
                "center": {"x": ax, "y": ay},
                "size": {"width": 200, "height": 200},  # default
            })
        with open(SCENE_GRAPH, "w") as f:
            _json.dump(sg_items, f, indent=2)
        print(f"  Generated {len(sg_items)} objects in {SCENE_GRAPH}")

    from nav_task.navmesh_interface import NavmeshNavigationInterface
    nav = NavmeshNavigationInterface(ucv)

    all_episodes = []

    # --- PointNav episodes at different difficulties ---
    print("\n" + "="*60)
    print("  POINTNAV EPISODES (NavMesh validated)")
    print("="*60)

    for i, (label, min_d, max_d) in enumerate([
        ("PointNav EASY (10-20m)", 1000, 2000),
        ("PointNav MEDIUM (20-40m)", 2000, 4000),
        ("PointNav HARD (40-80m)", 4000, 8000),
    ]):
        try:
            result = sample_pointnav_episode_navmesh(
                ucv, seed=42, idx=i,
                min_geodesic_cm=min_d, max_geodesic_cm=max_d,
                build_navmesh=False, nav_interface=nav,
            )
            print_episode(label, result)
            all_episodes.append((label, result))
        except Exception as e:
            print(f"\n  {label}: FAILED - {e}")

    # --- ObjectNav episodes ---
    print("\n" + "="*60)
    print("  OBJECTNAV EPISODES (NavMesh validated)")
    print("="*60)

    object_targets = [
        ("Tree", lambda n: n.startswith("Tree_"), "tree",
         "a large green tree with spreading branches"),
        ("Trash", lambda n: "Trash" in n, "trash can",
         "a metal trash can on the sidewalk"),
    ]

    for i, (name, filt, cat, desc) in enumerate(object_targets):
        try:
            result = sample_objectnav_episode_navmesh(
                ucv, seed=100, idx=i,
                target_filter=filt, object_category=cat,
                object_description=desc,
                build_navmesh=False, nav_interface=nav,
            )
            print_episode(f"ObjectNav: {name}", result)
            all_episodes.append((f"ObjectNav: {name}", result))
        except Exception as e:
            print(f"\n  ObjectNav {name}: FAILED - {e}")

    # --- Validation checks ---
    print("\n" + "="*60)
    print("  VALIDATION CHECKS")
    print("="*60)

    for label, result in all_episodes:
        ep = result["episode"]
        wps = result["gt_path_waypoints"]
        sx, sy = ep.start_position.x, ep.start_position.y
        gx, gy = ep.goal_position.x, ep.goal_position.y

        # Check: start not inside building
        proj_start = ucv.send(f"vget /nav/project {sx} {sy} 10").strip()
        start_on_nav = proj_start != "-1"

        # Check: goal not inside building
        proj_goal = ucv.send(f"vget /nav/project {gx} {gy} 10").strip()
        goal_on_nav = proj_goal != "-1"

        # Check: path has more than 2 waypoints (indicates obstacle avoidance)
        has_detour = len(wps) > 2

        print(f"  {label}:")
        print(f"    Start on navmesh: {start_on_nav}")
        print(f"    Goal on navmesh:  {goal_on_nav}")
        print(f"    GT path detours:  {has_detour} ({len(wps)} waypoints)")
        print(f"    Detour ratio:     {result['difficulty']['detour_ratio']}")

    # --- Visualize ---
    if all_episodes:
        visualize_episodes(
            all_episodes, SCENE_GRAPH,
            "simworld_studio_workspace/scripts/task_gen_viz.png"
        )

    ucv.disconnect()
    print("\nDONE")


if __name__ == "__main__":
    main()
