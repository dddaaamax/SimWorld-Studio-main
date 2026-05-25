"""Add NavModifierComponent to all CityDatabase Blueprint actors.

This makes navmesh treat the entire bounding box of each BP as non-walkable.
NavModifierComponent with default AreaClass=NavArea_Null means "no navmesh here".
"""
import sys, time, json
sys.path.insert(0, "simworld_studio_workspace/gym_env")
from mcp_client import MCPClient

mcp = MCPClient(port=55558)
print("Waiting for editor...")
mcp._wait_until_ready(timeout=60)

# Get all BP paths
resp = mcp.execute_python('''import unreal
ar = unreal.AssetRegistryHelpers.get_asset_registry()
assets = ar.get_assets_by_path("/Game/CityDatabase", recursive=True)
SKIP = {"BP_AssetBase", "BPI_Objects", "BP_BuildingBase", "BP_DetailBase",
        "BP_Waypoint_Mark", "BP_Road1", "Floor"}
for a in assets:
    if str(a.asset_class_path.asset_name) == "Blueprint":
        name = str(a.asset_name)
        if name not in SKIP:
            print("BP|" + str(a.package_name) + "|" + name)
''', timeout=15)

logs = resp.get("result", {}).get("python_logs", []) if resp else []
bp_list = []
for l in logs:
    l = l.strip()
    if "BP|" in l:
        idx = l.index("BP|")
        parts = l[idx:].split("|")
        if len(parts) >= 3:
            bp_list.append((parts[1], parts[2]))

print(f"Found {len(bp_list)} Blueprints")

BATCH = 8
total_ok = 0
total_skip = 0
total_err = 0

for i in range(0, len(bp_list), BATCH):
    batch = bp_list[i:i+BATCH]
    paths_str = ";;".join(p for p, n in batch)
    names_str = ";;".join(n for p, n in batch)

    script = f'''import unreal
sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
paths = "{paths_str}".split(";;")
names = "{names_str}".split(";;")
for bp_path, bp_name in zip(paths, names):
    try:
        bp = unreal.load_asset(bp_path)
        if not bp:
            print("SKIP " + bp_name + " (load fail)")
            continue
        gc = bp.generated_class()
        if not gc:
            print("SKIP " + bp_name + " (no gc)")
            continue

        # Check if already has NavModifierComponent
        temp = eas.spawn_actor_from_class(gc, unreal.Vector(0, 0, -99000))
        if not temp:
            print("SKIP " + bp_name + " (spawn fail)")
            continue
        nav_mods = temp.get_components_by_class(unreal.NavModifierComponent)
        origin, extent = temp.get_actor_bounds(False)
        temp.destroy_actor()

        if len(nav_mods) > 0:
            print("SKIP " + bp_name + " (already has NavMod)")
            continue

        # Skip very small objects
        if extent.x < 30 or extent.y < 30 or extent.z < 30:
            print("SKIP " + bp_name + " (too small)")
            continue

        # Add NavModifierComponent to BP
        handles = sds.k2_gather_subobject_data_for_blueprint(bp)
        if len(handles) == 0:
            print("SKIP " + bp_name + " (no handles)")
            continue

        params = unreal.AddNewSubobjectParams()
        params.blueprint_context = bp
        params.new_class = unreal.NavModifierComponent
        params.parent_handle = handles[0]
        result_handle, reason = sds.add_new_subobject(params)
        if str(reason):
            print("ERR " + bp_name + ": " + str(reason))
            continue

        unreal.EditorAssetLibrary.save_asset(bp_path)
        print("OK " + bp_name + " (" + str(int(extent.x)) + "x" + str(int(extent.y)) + "x" + str(int(extent.z)) + ")")
    except Exception as e:
        print("ERR " + bp_name + ": " + str(e))
'''

    batch_names = [n for _, n in batch]
    print(f"\nBatch {i//BATCH+1}: {', '.join(batch_names)}")
    try:
        resp = mcp.execute_python(script, timeout=60)
        logs = resp.get("result", {}).get("python_logs", []) if resp else []
        for l in logs:
            l = l.strip()
            print(f"  {l}")
            if "OK " in l: total_ok += 1
            elif "SKIP " in l: total_skip += 1
            elif "ERR " in l: total_err += 1
    except Exception as e:
        print(f"  BATCH FAILED: {e}")
        total_err += len(batch)

print(f"\n=== DONE: {total_ok} updated, {total_skip} skipped, {total_err} errors ===")
