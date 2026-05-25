"""Fix all Building BPs in the current scene.
For each unique Building class:
1. Add NavModifierComponent to BP if missing
2. Set FailsafeExtent = mesh XY * 0.95
3. Disable mesh can_ever_affect_navigation
4. Save BP
"""
import sys, time, json
sys.path.insert(0, "simworld_studio_workspace/gym_env")
from mcp_client import MCPClient

mcp = MCPClient(port=55558)
print("Waiting for editor...")
mcp._wait_until_ready(timeout=60)

# Step 1: Get all unique Building classes and one actor name per class
resp = mcp.execute_python('''import unreal
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = eas.get_all_level_actors()
seen = {}
for a in actors:
    cls = a.get_class().get_name()
    if "Building" in cls and cls not in seen:
        seen[cls] = a.get_name()
for cls, name in sorted(seen.items()):
    print("B|" + cls + "|" + name)
print("TOTAL|" + str(len(seen)))
''', timeout=15)

logs = resp.get("result", {}).get("python_logs", []) if resp else []
buildings = []
for l in logs:
    l = l.strip()
    if "B|" in l:
        rest = l[l.index("B|"):]
        parts = rest.split("|")
        if len(parts) >= 3:
            buildings.append((parts[1], parts[2]))

print(f"Found {len(buildings)} unique Building classes")

# Step 2: For each class, add NavMod to BP + set properties via the scene actor
BATCH = 5
total_ok = 0
total_err = 0

for i in range(0, len(buildings), BATCH):
    batch = buildings[i:i+BATCH]

    # Build script for this batch
    actor_lines = []
    for cls, actor_name in batch:
        bp_name = cls.replace("_C", "")
        actor_lines.append(f'("{bp_name}", "{actor_name}")')

    actors_str = "[" + ",".join(actor_lines) + "]"

    script = f'''import unreal
sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
all_actors = eas.get_all_level_actors()
actor_map = {{a.get_name(): a for a in all_actors}}

batch = {actors_str}
for bp_name, actor_name in batch:
    actor = actor_map.get(actor_name)
    if not actor:
        print("SKIP " + bp_name + " (actor not found)")
        continue

    # Check if NavMod exists
    nms = actor.get_components_by_class(unreal.NavModifierComponent)

    # If no NavMod, add to Blueprint
    if len(nms) == 0:
        bp_path = "/Game/CityDatabase/blueprints/" + bp_name
        bp = unreal.load_asset(bp_path)
        if not bp:
            print("SKIP " + bp_name + " (BP not found)")
            continue
        handles = sds.k2_gather_subobject_data_for_blueprint(bp)
        if not handles:
            print("SKIP " + bp_name + " (no SCS handles)")
            continue
        params = unreal.AddNewSubobjectParams()
        params.blueprint_context = bp
        params.new_class = unreal.NavModifierComponent
        params.parent_handle = handles[0]
        result, reason = sds.add_new_subobject(params)
        if str(reason):
            print("ERR " + bp_name + " add: " + str(reason))
            continue
        unreal.EditorAssetLibrary.save_asset(bp_path)
        # Refresh actor to get the new component
        nms = actor.get_components_by_class(unreal.NavModifierComponent)

    # Get mesh bounds
    meshes = actor.get_components_by_class(unreal.StaticMeshComponent)
    if not meshes or not meshes[0].static_mesh:
        print("SKIP " + bp_name + " (no mesh)")
        continue

    sm = meshes[0].static_mesh
    bb = sm.get_bounding_box()
    hx = (bb.max.x - bb.min.x) / 2.0
    hy = (bb.max.y - bb.min.y) / 2.0
    hz = (bb.max.z - bb.min.z) / 2.0

    # Set FailsafeExtent on NavMod
    for nm in nms:
        nm.set_editor_property("failsafe_extent", unreal.Vector(hx * 0.95, hy * 0.95, hz))

    # Disable mesh nav
    for mc in meshes:
        mc.set_editor_property("can_ever_affect_navigation", False)

    print("OK " + bp_name + " fe=(" + str(int(hx*0.95)) + "," + str(int(hy*0.95)) + "," + str(int(hz)) + ")" + (" +NavMod" if len(nms) > 0 else ""))
'''

    batch_names = [b[0].replace("_C","") for b in batch]
    print(f"\nBatch {i//BATCH+1}: {', '.join(batch_names)}")
    for attempt in range(3):
        try:
            resp = mcp.execute_python(script, timeout=60)
            logs = resp.get("result", {}).get("python_logs", []) if resp else []
            for l in logs:
                l = l.strip()
                print(f"  {l}")
                if "OK " in l: total_ok += 1
                elif "ERR " in l or "SKIP " in l: total_err += 1
            break
        except Exception:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  BATCH FAILED after retries")
                total_err += len(batch)

print(f"\n=== DONE: {total_ok} OK, {total_err} skipped/errors ===")
