"""Visualize DiverseMaps50 dataset: nested map taxonomy, task difficulty, assets.

Plots produced (under results/diverse50_eval/figs/):
  1. map_taxonomy_donut.png   — inner ring: biome, outer ring: map (task count)
  2. difficulty_violins.png   — violins per metric: geodesic, detour, waypoints, tasks-per-map
  3. asset_types.png          — violin per asset-type-class (only if map_assets.json dumped;
                                run dump_map_assets.py first, else falls back to target categories)

Usage: python3 visualize_dataset.py
"""
import collections
import json
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

WORKSPACE = pathlib.Path(__file__).resolve().parent.parent.parent
DATASET_DIR = WORKSPACE / "datasets" / "diverse50"
SCRIPTS_DIR = WORKSPACE / "scripts"
OUT_DIR = WORKSPACE / "results" / "diverse50_eval" / "figs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(SCRIPTS_DIR))
from prompts_diverse_50 import PROMPTS  # name → template


# ---------- biome taxonomy: template → biome label ----------
BIOME_OF_TEMPLATE = {
    "/Game/WinterTown/Maps/RussianWinterTownDemo01":            "Winter Town",
    "/Game/WinterTown/Maps/RussianWinterTownDemo02":            "Winter Town",
    "/Game/Village/Maps/Village":                               "Slavic Village",
    "/Game/Village/Maps/Village_SummerNightExample":            "Slavic Village",
    "/Game/ChineseWaterTown/Ver1/Map/DemoMap":                  "Chinese Water Town",
    "/Game/ModularCourtyard/Maps/SampleScene_sanny":            "Modular Courtyard",
    "/Game/ModularCourtyard/Maps/SampleScene_overcast":         "Modular Courtyard",
    "/Game/MiddleEast/Maps/MiddleEast":                         "Middle East",
    "/Game/HwaseongHaenggung/Maps/Demo":                        "Korean Palace",
    "/Game/HwaseongFortress/Maps/Demonstration":                "Korean Palace",
    "/Game/CastleRiver/Maps/Demonstration":                     "Medieval Castle",
    "/Game/Cave/Maps/Demonstration":                            "Cave",
    "/Game/Chinese_Landscape/Levels/Chinese_Landscape_Demo":    "Chinese Landscape",
    "/Game/TrainStation/Maps/Demonstration":                    "Train Station",
    "/Game/ContainerYard/Maps/Demonstration":                   "Container Yard",
    "/Game/ContainerYard/Maps/Demonstration_Day":               "Container Yard",
    "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapDay":    "Gothic Fantasy",
    "/Game/ModularGothicFantasyEnvironment/Maps/DemoMapNight":  "Gothic Fantasy",
    "/Game/ModularTemplePlaza/Maps/ConceptMap":                 "Temple Plaza",
    "/Game/Dungeon/Levels/Dungeon_Demo_00":                     "Dungeon",
    "/Game/Lighthouse_Island/Levels/Lighthouse_Demo_00":        "Lighthouse Island",
    "/Game/Maps/EmptyMap":                                      "From-Scratch (Empty)",
}

NAME_TO_TEMPLATE = {p["name"]: p["template"] for p in PROMPTS}


def load_jsonl(path):
    return [json.loads(l) for l in open(path) if l.strip()]


# ---------- load dataset ----------
files = {
    "train_pn": DATASET_DIR / "train_pointnav.jsonl",
    "test_pn":  DATASET_DIR / "test_pointnav.jsonl",
    "train_on": DATASET_DIR / "train_objectnav.jsonl",
    "test_on":  DATASET_DIR / "test_objectnav.jsonl",
}
data = {k: load_jsonl(v) for k, v in files.items() if v.exists()}
all_records = [r for rs in data.values() for r in rs]

# tasks per map (pointnav + objectnav combined)
tasks_per_map = collections.Counter(r["map"] for r in all_records)
maps_present = set(tasks_per_map.keys())

# map → biome
biome_of_map = {}
unknown = []
for m in maps_present:
    tmpl = NAME_TO_TEMPLATE.get(m)
    if tmpl is None:
        unknown.append(m)
        biome_of_map[m] = "Unknown"
    else:
        biome_of_map[m] = BIOME_OF_TEMPLATE.get(tmpl, f"Other ({tmpl})")

if unknown:
    print(f"[warn] no template registered for: {unknown}")


# ========== 1. nested donut: inner=biome, outer=map (task count) ==========
biome_map_counts = collections.defaultdict(list)  # biome → [(map, count), ...]
for m, n in tasks_per_map.items():
    biome_map_counts[biome_of_map[m]].append((m, n))

# sort biomes by total task count (desc), maps within each biome by count (desc)
biomes_sorted = sorted(biome_map_counts.items(),
                       key=lambda kv: -sum(n for _, n in kv[1]))

# colour each biome, then lighten for its maps
cmap = plt.get_cmap("tab20")
biome_colors = {b: cmap(i % 20) for i, (b, _) in enumerate(biomes_sorted)}

inner_sizes, inner_labels, inner_colors = [], [], []
outer_sizes, outer_labels, outer_colors = [], [], []

for biome, entries in biomes_sorted:
    entries_sorted = sorted(entries, key=lambda e: -e[1])
    biome_total = sum(n for _, n in entries_sorted)
    inner_sizes.append(biome_total)
    inner_labels.append(f"{biome}\n({biome_total})")
    inner_colors.append(biome_colors[biome])

    base_rgb = np.array(biome_colors[biome][:3])
    n_maps = len(entries_sorted)
    for i, (mname, cnt) in enumerate(entries_sorted):
        # lighten toward white as i increases so sibling maps are distinguishable
        t = 0.15 + 0.55 * (i / max(1, n_maps - 1)) if n_maps > 1 else 0.2
        rgb = base_rgb * (1 - t) + np.array([1.0, 1.0, 1.0]) * t
        outer_sizes.append(cnt)
        # drop the "map_NN_" prefix in the label for readability
        short = mname.split("_", 2)[-1] if mname.count("_") >= 2 else mname
        outer_labels.append(f"{short}\n({cnt})")
        outer_colors.append((*rgb, 1.0))

fig1, ax1 = plt.subplots(figsize=(14, 14))
ax1.set_title(
    f"DiverseMaps50 map taxonomy — {len(maps_present)} maps · "
    f"{sum(tasks_per_map.values())} tasks (PointNav + ObjectNav)\n"
    f"inner ring: biome · outer ring: specific map · "
    f"numbers in parentheses = task count for that biome / map",
    fontsize=12, pad=22,
)

# inner (biome)
w_inner = 0.30
ax1.pie(
    inner_sizes, labels=inner_labels, colors=inner_colors, radius=1.0 - w_inner,
    wedgeprops=dict(width=w_inner, edgecolor="white", linewidth=1.2),
    labeldistance=0.60, textprops={"fontsize": 9, "fontweight": "bold"},
    startangle=90,
)

# outer (map) — only show labels for slices large enough to read
min_frac = 0.010 * sum(outer_sizes)
outer_label_display = [lab if sz >= min_frac else "" for lab, sz in zip(outer_labels, outer_sizes)]
ax1.pie(
    outer_sizes, labels=outer_label_display, colors=outer_colors, radius=1.0,
    wedgeprops=dict(width=w_inner - 0.02, edgecolor="white", linewidth=0.8),
    labeldistance=1.05, textprops={"fontsize": 7},
    startangle=90,
)

ax1.set(aspect="equal")
fig1.tight_layout()
out1 = OUT_DIR / "map_taxonomy_donut.png"
fig1.savefig(out1, dpi=130, bbox_inches="tight")
print(f"[save] {out1}")
plt.close(fig1)


# ========== 2. difficulty violins ==========
def metric(rec, key):
    if key == "distance_m":
        return rec.get("geodesic_distance_cm", 0) / 100.0
    if key == "detour_ratio":
        d = rec.get("difficulty", {})
        if "detour_ratio" in d:
            return d["detour_ratio"]
        # fallback for objectnav (no pre-computed detour): skip
        return None
    if key == "gt_waypoints":
        return len(rec.get("gt_path", []))
    return None

def series(records, key):
    xs = [metric(r, key) for r in records]
    return np.array([x for x in xs if x is not None], dtype=float)

pn_all = data.get("train_pn", []) + data.get("test_pn", [])
on_all = data.get("train_on", []) + data.get("test_on", [])

# tasks-per-map as a "content density" metric
tasks_per_map_vals = np.array(list(tasks_per_map.values()), dtype=float)

series_specs = [
    ("Geodesic distance (m)",
     [series(pn_all, "distance_m"), series(on_all, "distance_m")],
     ["PointNav", "ObjectNav"],
     None),
    ("GT waypoints per episode",
     [series(pn_all, "gt_waypoints"), series(on_all, "gt_waypoints")],
     ["PointNav", "ObjectNav"],
     "p99"),  # clip view to p99 so the long tail doesn't compress the bulk
]

fig2, axes2 = plt.subplots(1, 2, figsize=(11, 5.5))
fig2.suptitle("DiverseMaps50 — task difficulty distribution", fontsize=14)

violin_palette = ["#4c72b0", "#c44e52", "#55a868", "#8172b2"]

for ax, (title, arr_list, labels, clip) in zip(axes2, series_specs):
    positions = np.arange(1, len(arr_list) + 1)
    # optional p99 clip so a handful of long-tail outliers don't compress the bulk
    plot_arrs = arr_list
    max_val = None
    if clip == "p99":
        all_vals = np.concatenate([a for a in arr_list if len(a)])
        max_val = float(np.percentile(all_vals, 99))
    parts = ax.violinplot(
        plot_arrs, positions=positions, showmeans=False, showmedians=True,
        widths=0.75,
    )
    for i, body in enumerate(parts["bodies"]):
        body.set_facecolor(violin_palette[i % len(violin_palette)])
        body.set_edgecolor("black")
        body.set_alpha(0.75)
    for key in ("cbars", "cmins", "cmaxes", "cmedians"):
        if key in parts:
            parts[key].set_color("black")
            parts[key].set_linewidth(1.0)

    for pos, arr in zip(positions, arr_list):
        if len(arr) == 0:
            continue
        ax.scatter([pos], [arr.mean()], color="white", edgecolor="black",
                   zorder=4, s=30, marker="D")

    if max_val is not None:
        ax.set_ylim(top=max_val * 1.10)
        ax.text(0.98, 0.97, f"view clipped at p99 (={max_val:.1f})",
                transform=ax.transAxes, ha="right", va="top",
                fontsize=8, color="#666")

    ax.set_title(title, fontsize=11, pad=8)
    ax.set_xticks(positions)
    xlabels = []
    for lab, arr in zip(labels, arr_list):
        if len(arr) == 0:
            xlabels.append(lab)
        else:
            xlabels.append(
                f"{lab}\nμ={arr.mean():.2f}  median={np.median(arr):.2f}\n"
                f"max={arr.max():.0f}  n={len(arr)}")
    ax.set_xticklabels(xlabels, fontsize=8)
    ax.grid(axis="y", linestyle=":", alpha=0.4)

fig2.tight_layout(rect=[0, 0, 1, 0.96])
out2 = OUT_DIR / "difficulty_violins.png"
fig2.savefig(out2, dpi=130, bbox_inches="tight")
print(f"[save] {out2}")
plt.close(fig2)


# ========== 3. top-N asset types bar chart ==========
# Needs a dump from dump_map_assets.py; else prints a hint and skips.
ASSET_DUMP = WORKSPACE / "datasets" / "diverse50" / "map_assets.json"
TOP_N = 20

# Actor labels we treat as "invisible" and drop — floor, ground, nav volumes,
# lights/atmosphere, editor-only helpers. Everything else is counted.
SKIP_CLASSES_SUBSTR = (
    "Floor", "Ground", "Plane", "Landscape", "Terrain",
    "SM_Floor", "SM_Ground", "SM_Pavement", "SM_Sidewalk",
    "Arena_Env", "WorldDataLayers", "LevelBounds", "AbstractNavData",
    "NavMesh", "NavMeshBoundsVolume", "PlayerStart", "RecastNavMesh",
    "DirectionalLight", "SkyLight", "SkyAtmosphere", "VolumetricCloud",
    "ExponentialHeightFog", "PostProcessVolume", "ReflectionCapture",
    "AtmosphericFog", "LightmassImportanceVolume", "WorldSettings",
    "CameraActor", "SphereReflectionCapture", "BoxReflectionCapture",
    "LensFlareSource", "LightmassCharacterIndirectDetailVolume",
    "BrushShape", "Brush",
    # meta-containers (not real visible assets)
    "GroupActor", "PlayerState", "AbstractInstance", "ParticleEventManager",
    "HUD_", "GameplayDebuggerCategoryReplicator", "LevelSequenceActor",
    "BP_Sky", "BP_Sun", "BP_Weather",
)


def asset_class(label: str) -> str | None:
    """Collapse an actor label into a 'class'.

    UE auto-numbers spawned instances in two ways: either `BaseName_42` (with
    underscore, e.g. SM_Bench_17) or `BaseName42` (no underscore, e.g.
    SM_LampPost8, SM_Ladder01). Strip either form so all instances collapse.
    """
    if any(s in label for s in SKIP_CLASSES_SUBSTR):
        return None
    import re as _re
    # strip trailing "_C" blueprint suffix first
    cls = _re.sub(r"_C$", "", label)
    # repeatedly peel any trailing (_?\d+) segments
    prev = None
    while prev != cls:
        prev = cls
        cls = _re.sub(r"_?\d+$", "", cls)
    return cls or None


fig3, ax3 = plt.subplots(figsize=(14, 7))

if ASSET_DUMP.exists():
    dump = json.loads(ASSET_DUMP.read_text())

    total_counts: collections.Counter = collections.Counter()
    maps_with_class: collections.Counter = collections.Counter()
    n_maps = 0
    for mname, entry in dump.items():
        if mname not in maps_present:
            continue
        n_maps += 1
        # Re-collapse classes client-side — the UE-side regex only caught the
        # `_<digits>` form, so `SM_LampPost8`-style labels leaked through as
        # distinct keys. Applying asset_class() here merges them.
        raw_pairs = entry.items() if isinstance(entry, dict) else ((l, 1) for l in entry)
        class_counts: collections.Counter = collections.Counter()
        for raw_label, cnt in raw_pairs:
            cls = asset_class(raw_label)
            if cls:
                class_counts[cls] += cnt
        for cls, cnt in class_counts.items():
            total_counts[cls] += cnt
            maps_with_class[cls] += 1

    top = total_counts.most_common(TOP_N)
    classes = [c for c, _ in top]
    counts  = [n for _, n in top]
    covers  = [maps_with_class[c] for c in classes]

    cmap3 = plt.get_cmap("viridis")
    colors = [cmap3(i / max(1, len(classes) - 1)) for i in range(len(classes))]
    x = np.arange(len(classes))
    bars = ax3.bar(x, counts, color=colors, edgecolor="black", linewidth=0.6)

    for rect, n, cov in zip(bars, counts, covers):
        ax3.text(
            rect.get_x() + rect.get_width() / 2, rect.get_height(),
            f"{n}\n({cov}/{n_maps} maps)",
            ha="center", va="bottom", fontsize=8,
        )

    ax3.set_xticks(x)
    ax3.set_xticklabels(classes, rotation=45, ha="right", fontsize=9)
    ax3.set_ylabel("total count across all maps")
    ax3.set_title(
        f"DiverseMaps50 — top {TOP_N} asset classes "
        f"(dumped from {n_maps} maps · floor / ground / landscape / lights excluded)",
        fontsize=12,
    )
    ax3.grid(axis="y", linestyle=":", alpha=0.4)
    ax3.margins(y=0.15)
else:
    ax3.axis("off")
    msg = (
        "No map_assets.json yet.\n\n"
        "Run:  python3 experiments/diverse50_eval/dump_map_assets.py\n\n"
        "That boots UE on each of the 54 maps (≈25s/map, incremental save),\n"
        "then re-run this script to get the top-20 asset-class bar chart."
    )
    ax3.text(0.5, 0.5, msg, transform=ax3.transAxes,
             ha="center", va="center", fontsize=12,
             family="monospace", color="#a0522d")
    ax3.set_title("DiverseMaps50 — asset classes (awaiting dump)",
                  fontsize=12, color="#a0522d")

fig3.tight_layout()
out3 = OUT_DIR / "asset_types.png"
fig3.savefig(out3, dpi=130, bbox_inches="tight")
print(f"[save] {out3}")
plt.close(fig3)

print("\nDone. Outputs in", OUT_DIR)
