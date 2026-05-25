---
id: map_asset_discovery
name: Map Asset Discovery & Template Usage
version: 1.0.0
author: simworld
tags: [map, assets, templates, biome, retrieval, allow-ai]
dependencies: []
description: How to discover and use the full allow-AI asset pool for diverse-biome map generation — CityDatabase props, 22 marketplace map templates, and on-demand retrieval from allow-AI pack roots.
---

## When to Use

Use this skill when generating **diverse-biome maps** (village, winter/snow, castle, cave, dungeon, chinese landscape, middle east, container yard, train station, temple, sci-fi, etc.) — anywhere the default CityDatabase-only catalog is insufficient.

**Precondition:** the MCP server must be started with `SIMWORLD_ASSETS_FILE=assets_full.json` in its env. If you call `list_assets` and don't see `map_templates` / `allow_ai_packs` sections, the full catalog is not loaded — stop and flag to the user.

## Three Asset Sources

### 1. CityDatabase (spawn by shorthand)

All 125 buildings, 6 trees, vehicles, and 20 props are available via `spawn_blueprint_actor` with shorthand IDs:

```
spawn_blueprint_actor(actor_name="House_12", blueprint_id="BP_Building_42", location=[1000, 0, 0])
spawn_blueprint_actor(actor_name="Tree_A",   blueprint_id="BP_Tree3",       location=[500, 500, 0])
spawn_blueprint_actor(actor_name="Couch_1",  blueprint_id="BP_Couch",       location=[0, 0, 0])
```

Valid building IDs: 1–127 except 57 and 120. Use `list_assets(category="buildings")` for the exact set.

**Do NOT use roads:** `BP_Road1`, `SM_Road`, `SM_Road_Side*` — they look poor in free-form placement.

### 2. Map Templates (22 allow-AI marketplace maps)

Use `list_assets(category="map_templates")` to see the full list with biome tags. Each template is a showcase `.umap` from a licensed marketplace pack.

**Recommended workflow — clone and modify:**

```python
# Inside execute_python_script:
import unreal

template = "/Game/WinterTown/Maps/RussianWinterTownDemo01"
dest     = "/Game/_Runs/map_042"

# Load the showcase map
unreal.EditorLoadingAndSavingUtils.load_map(template)

# (optional) delete / modify actors, add agents, change lighting
# e.g. remove crowd, add player start at specific spot

# Save-as under the generation output folder
world = unreal.EditorLevelLibrary.get_editor_world()
unreal.EditorLoadingAndSavingUtils.save_map(world, dest)
print(f"Saved derivative map to {dest}")
```

Biome coverage of the 22 templates:

| Biome | Templates |
|---|---|
| village / water town | ChineseWaterTown, Village (x2), MiddleEast |
| snow / winter | WinterTown (x2 Russian) |
| landscape / wilderness | Chinese_Landscape, Lighthouse_Island |
| castle / medieval | CastleRiver, ModularGothicFantasy (x2) |
| cave / dungeon | Cave, Dungeon |
| temple / palace | ModularTemplePlaza, HwaseongHaenggung (Korean) |
| industrial / port | TrainStation, ContainerYard (x2) |
| courtyard / plaza | ModularCourtyard (x2) |
| sci-fi | ModularSciFi (x2: outdoor rocky-swampy + indoor) |

### 3. On-Demand Retrieval from Allow-AI Packs

When you need an asset (building, mesh, prop) from a template's pack that isn't in `list_assets` output, use `execute_python_script` to enumerate it:

```python
import unreal

pack_root = "/Game/Village/"  # must match an entry in allow_ai_packs.items

# List every asset under the pack (blueprints, meshes, materials...)
paths = unreal.EditorAssetLibrary.list_assets(pack_root, recursive=True, include_folder=False)
for p in paths[:50]:
    print(p)
```

Then spawn what you find by full path:

```
spawn_blueprint_actor(
  actor_name="Barn_1",
  blueprint_id="/Game/Village/Blueprints/BP_Barn.BP_Barn_C",
  location=[2000, 0, 0]
)
# or for static meshes:
spawn_actor(
  name="SnowPine_1",
  static_mesh="/Game/WinterTown/Meshes/SM_Pine_Snow_03.SM_Pine_Snow_03",
  location=[1500, 500, 0]
)
```

**Allow-list (17 pack roots):** see `list_assets(category="allow_ai_packs")`. Any asset under these roots is fair game.

## Hard Rules

1. **Never** use anything under `/Game/80_no_ai_maps/` — folder name indicates NOT allow-AI.
2. **Never** spawn road assets (`BP_Road1`, `SM_Road*`) — they look poor.
3. **Always** save derivative maps under `/Game/_Runs/map_XXX` — never overwrite a template.
4. After saving, call `setup_environment` (if needed) then `take_screenshot` to record an overview for the generation log.

## End-to-End Example

Goal: generate a snow village map.

```
1. execute_python_script:
     unreal.EditorLoadingAndSavingUtils.load_map("/Game/WinterTown/Maps/RussianWinterTownDemo01")
2. get_actors_in_level()                               # survey what's there
3. (optional) delete a few actors to open up navigable space
4. execute_python_script:
     world = unreal.EditorLevelLibrary.get_editor_world()
     unreal.EditorLoadingAndSavingUtils.save_map(world, "/Game/_Runs/map_042_snow_village")
5. take_screenshot(filename="map_042_overview.png")
6. verify_scene(original_request="diverse snow village map for nav training")
```
