"""
Query Actors 2D — Reusable UE Python Script
============================================
Walks the current level and reports every meaningful actor's:
  * world-space 2D center (x, y) in cm
  * 2D bounding-box size (width, height) in cm — full size, not half-extent,
    derived from the world-axis-aligned bounds
  * yaw orientation in degrees (rotation around Z)

System actors (lights, volumes, fog, world settings, ...) and zero-extent
actors are filtered out so the output only contains real scene geometry.

Usage via execute_python_script MCP tool:
  # print only
  execute_python_script(script=open('scripts/query_actors_2d.py').read())

  # print + save to JSON (set SAVE_PATH below, or override via globals before exec)
  SAVE_PATH = 'D:/tmp/actors_2d.json'
  execute_python_script(script=open('scripts/query_actors_2d.py').read())

Configuration:
  SAVE_PATH — if a non-empty string, results are also written to this JSON file.
              Leave as '' (default) to print only.
"""
import json
import unreal

# ── Configuration ──
# Set to a file path (e.g. 'D:/tmp/actors_2d.json') to also dump JSON to disk.
# If the variable is already defined in the exec globals, that value wins.
try:
    SAVE_PATH  # noqa: F821 — may be injected by caller
except NameError:
    SAVE_PATH = ''

# Actor classes that are part of the level scaffolding, not "scene objects".
SKIP_CLASSES = {
    "WorldSettings", "Brush", "AbstractNavData", "PlayerStart",
    "SkyLight", "DirectionalLight", "PointLight", "SpotLight", "RectLight",
    "SphereReflectionCapture", "BoxReflectionCapture", "PlanarReflection",
    "PostProcessVolume", "ExponentialHeightFog", "SkyAtmosphere",
    "VolumetricCloud", "AtmosphericFog",
    "LightmassImportanceVolume", "PrecomputedVisibilityVolume",
    "HLODSelectionActor", "WorldPartitionMiniMap", "WorldDataLayers",
    "LevelInstanceEditorInstanceActor",
}


def collect_actors_2d():
    subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = subsys.get_all_level_actors()

    out = []
    for actor in all_actors:
        if not isinstance(actor, unreal.Actor):
            continue
        cls_name = actor.get_class().get_name()
        if cls_name in SKIP_CLASSES:
            continue

        try:
            origin, box_extent = actor.get_actor_bounds(False)
        except Exception:
            # Pure logic actors with no renderable component
            continue

        if box_extent.x == 0.0 and box_extent.y == 0.0 and box_extent.z == 0.0:
            continue

        rot = actor.get_actor_rotation()

        out.append({
            "name": actor.get_actor_label(),
            "class": cls_name,
            "center": {
                "x": float(origin.x),
                "y": float(origin.y),
            },
            "size": {
                "width":  float(box_extent.x) * 2.0,
                "height": float(box_extent.y) * 2.0,
            },
            "yaw": float(rot.yaw),
        })
    return out


def main(save_path=''):
    results = collect_actors_2d()
    print("Found {} actors".format(len(results)))
    print(json.dumps(results, indent=2, ensure_ascii=False))

    if save_path:
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print("Saved to {}".format(save_path))

    return results


# Run when executed via execute_python_script (no __main__ guard needed —
# UE's python_execute treats the script body as the entry point).
main(SAVE_PATH)
