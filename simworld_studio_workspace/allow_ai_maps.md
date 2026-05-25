# Allow-AI Map Templates

Maps below are licensed `allow AI` — both the `.umap` and all assets under their root folder can be used by Claude / coding-agent runs (open, duplicate, modify, save as new map under `/Game/_Runs/` etc.).

Paths are relative to `/data/koe/simworld_studio_projects/Content/`, and map as `/Game/<path without .umap>` in Unreal references.

## Verified available (22 maps, 20 packs)

| Pack (price) | UE path | Biome |
|---|---|---|
| ChineseWaterTown Ver.1 ($14.99) | `/Game/ChineseWaterTown/Ver1/Map/DemoMap` | village / water town |
| Lighthouse Island ($79.99) | `/Game/Lighthouse_Island/Levels/Lighthouse_Demo_00` | coastal / island |
| Modular Gothic/Fantasy Environment ($34.99) | `/Game/ModularGothicFantasyEnvironment/Maps/DemoMapDay` | gothic fantasy (day) |
| " | `/Game/ModularGothicFantasyEnvironment/Maps/DemoMapNight` | gothic fantasy (night) |
| Fantasy Medieval Castle Kit ($59.99) | `/Game/CastleRiver/Maps/Demonstration` | medieval castle |
| Fantasy Cave Environment Set ($39.99) | `/Game/Cave/Maps/Demonstration` | cave / underground |
| Modular Temple Plaza 4k PBR ($49.99) | `/Game/ModularTemplePlaza/Maps/ConceptMap` | temple / plaza |
| Victorian Train Station & Railroad ($49.99) | `/Game/TrainStation/Maps/Demonstration` | train station / industrial |
| Container Yard Environment Set ($39.99) | `/Game/ContainerYard/Maps/Demonstration` | container yard / port |
| " | `/Game/ContainerYard/Maps/Demonstration_Day` | container yard (day) |
| Modular Courtyard 1.0 ($19.99) | `/Game/ModularCourtyard/Maps/SampleScene_overcast` | courtyard (overcast) |
| " | `/Game/ModularCourtyard/Maps/SampleScene_sanny` | courtyard (sunny) |
| Middle East ($79.99) | `/Game/MiddleEast/Maps/MiddleEast` | middle-east village/town |
| Chinese Landscape / 63 Assets ($49.99) | `/Game/Chinese_Landscape/Levels/Chinese_Landscape_Demo` | chinese landscape / mountains |
| Slavic Village ($24.99) | `/Game/Village/Maps/Village_SummerNightExample` | slavic village (night) |
| " | `/Game/Village/Maps/Village` | slavic village (day) |
| Russian Winter Town ($1.99) | `/Game/WinterTown/Maps/RussianWinterTownDemo01` | snow / winter town 1 |
| " | `/Game/WinterTown/Maps/RussianWinterTownDemo02` | snow / winter town 2 |
| Modular Sci-Fi (Rocky Swampy Planet) ($99.99) | `/Game/ModularSciFi/Levels/LandscapePreview` | sci-fi rocky planet |
| " | `/Game/ModularSciFi/Levels/PreviewSceneIndoor` | sci-fi indoor |
| Dungeon Environment / 135+ Assets (free) | `/Game/Dungeon/Levels/Dungeon_Demo_00` | dungeon |
| Korean Traditional Palace (free) | `/Game/HwaseongHaenggung/Maps/Demo` | korean palace |

## Missing

| Pack | Expected path | Status |
|---|---|---|
| Industrial Area Hangar ($24.99) | `/Game/Hangar/Maps/Hangar` | not installed — `Hangar/` folder absent |

## Usage notes

- Open in UE via Python: `unreal.EditorLoadingAndSavingUtils.load_map("/Game/<path>")`
- Save-as for derived maps: `unreal.EditorLoadingAndSavingUtils.save_map(unreal.EditorLevelLibrary.get_editor_world(), "/Game/_Runs/map_XXX")`
- All assets under each pack root directory (not just the `.umap`) are in scope for AI use.
- `IndustrialArea.umap` under `/Game/80_no_ai_maps/IndustrialArea/` is NOT allow-AI — do not use.

## Plumbed-through catalog

- Registered in [web/server/assets_full.json](web/server/assets_full.json) under `map_templates` and `allow_ai_packs`.
- Default `assets.json` is untouched — existing pipeline unaffected.
- To enable the full catalog for an MCP-server run, set env `SIMWORLD_ASSETS_FILE=assets_full.json` when launching mcp-server.js.
- Agent-side guidance: [skills/map_asset_discovery_skill.md](skills/map_asset_discovery_skill.md).
