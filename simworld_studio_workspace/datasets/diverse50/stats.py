"""Dataset statistics + distribution plots for DiverseMaps50 tasks."""
import json, pathlib, collections
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATASET_DIR = pathlib.Path(__file__).parent

def load_jsonl(path):
    records = []
    for line in open(path):
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records

# Load all files
files = {
    "train_pn":  DATASET_DIR / "train_pointnav.jsonl",
    "test_pn":   DATASET_DIR / "test_pointnav.jsonl",
    "train_on":  DATASET_DIR / "train_objectnav.jsonl",
    "test_on":   DATASET_DIR / "test_objectnav.jsonl",
}
data = {k: load_jsonl(v) for k, v in files.items() if v.exists()}

print("=== Record counts ===")
for k, v in data.items():
    print(f"  {k}: {len(v)}")
total = sum(len(v) for v in data.values())
print(f"  TOTAL: {total}")

# --- Extract metrics ---
def extract(records):
    import math
    geo, eucl, detour, waypoints, sx, sy, gx, gy = [], [], [], [], [], [], [], []
    for r in records:
        g = r.get("geodesic_distance_cm", 0) / 100
        geo.append(g)
        sp = r.get("start_position", {})
        gp = r.get("goal_position") or (r.get("gt_path") or [{}])[-1]
        sx.append(sp.get("x", 0)); sy.append(sp.get("y", 0))
        if gp:
            gx.append(gp.get("x", 0)); gy.append(gp.get("y", 0))

        diff = r.get("difficulty", {})
        if "distance_m" in diff:
            e = diff["distance_m"]
        elif gp:
            e = math.sqrt((gp.get("x",0)-sp.get("x",0))**2 + (gp.get("y",0)-sp.get("y",0))**2) / 100
        else:
            e = g
        eucl.append(e)

        if "detour_ratio" in diff:
            detour.append(diff["detour_ratio"])
        elif e > 0:
            detour.append(round(g / e, 3))
        else:
            detour.append(1.0)

        waypoints.append(len(r.get("gt_path", [])))
    return dict(geo=np.array(geo), eucl=np.array(eucl), detour=np.array(detour),
                waypoints=np.array(waypoints), sx=np.array(sx), sy=np.array(sy),
                gx=np.array(gx), gy=np.array(gy))

all_pn = data.get("train_pn", []) + data.get("test_pn", [])
all_on = data.get("train_on", []) + data.get("test_on", [])

pn = extract(all_pn)
on_m = extract(all_on) if all_on else None

print("\n=== PointNav stats ===")
print(f"  geodesic_m:  mean={pn['geo'].mean():.1f}  std={pn['geo'].std():.1f}  min={pn['geo'].min():.1f}  max={pn['geo'].max():.1f}")
print(f"  detour_ratio: mean={pn['detour'].mean():.3f}  std={pn['detour'].std():.3f}")
print(f"  gt_waypoints: mean={pn['waypoints'].mean():.1f}  std={pn['waypoints'].std():.1f}  max={pn['waypoints'].max()}")

if on_m:
    print("\n=== ObjectNav stats ===")
    print(f"  geodesic_m:  mean={on_m['geo'].mean():.1f}  std={on_m['geo'].std():.1f}")
    print(f"  detour_ratio: mean={on_m['detour'].mean():.3f}")
    print(f"  gt_waypoints: mean={on_m['waypoints'].mean():.1f}")

    # ObjectNav category distribution
    cats = collections.Counter(r.get("target_category","?") for r in all_on)
    print("\n=== ObjectNav target categories ===")
    for cat, cnt in cats.most_common():
        print(f"  {cat}: {cnt}")

# --- Plots ---
fig, axes = plt.subplots(2, 4, figsize=(18, 9))
fig.suptitle("DiverseMaps50 Dataset Statistics", fontsize=14)

def hist(ax, data, title, xlabel, color="steelblue", bins=30):
    ax.hist(data, bins=bins, color=color, edgecolor="white", alpha=0.85)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("count")
    ax.axvline(data.mean(), color="red", linestyle="--", alpha=0.7, label=f"mean={data.mean():.1f}")
    ax.legend(fontsize=8)

# Row 0: PointNav
hist(axes[0,0], pn["geo"],      "PointNav: Geodesic Distance", "metres", "steelblue")
hist(axes[0,1], pn["detour"],   "PointNav: Detour Ratio",      "ratio (geo/eucl)", "darkorange")
hist(axes[0,2], pn["waypoints"],"PointNav: GT Waypoints",      "# waypoints", "seagreen")
# Scatter: start positions
axes[0,3].scatter(pn["sx"]/100, pn["sy"]/100, alpha=0.2, s=2, c="steelblue")
axes[0,3].set_title("PointNav: Start positions")
axes[0,3].set_xlabel("x (m)"); axes[0,3].set_ylabel("y (m)")

# Row 1: ObjectNav (or empty if no data)
if on_m and len(on_m["geo"]) > 0:
    hist(axes[1,0], on_m["geo"],      "ObjectNav: Geodesic Distance", "metres", "crimson")
    hist(axes[1,1], on_m["detour"],   "ObjectNav: Detour Ratio",      "ratio", "purple")
    hist(axes[1,2], on_m["waypoints"],"ObjectNav: GT Waypoints",      "# waypoints", "teal")
    axes[1,3].scatter(on_m["sx"]/100, on_m["sy"]/100, alpha=0.2, s=2, c="crimson")
    axes[1,3].set_title("ObjectNav: Start positions")
    axes[1,3].set_xlabel("x (m)"); axes[1,3].set_ylabel("y (m)")
    # Add bar chart of categories in one subplot
    cats = collections.Counter(r.get("target_category","?") for r in all_on)
    bars = axes[1,3]
    cats_items = cats.most_common(8)
    bars.clear()
    bars.bar([c[0] for c in cats_items], [c[1] for c in cats_items], color="teal")
    bars.set_title("ObjectNav: Target categories")
    bars.tick_params(axis='x', rotation=30, labelsize=8)
else:
    for ax in axes[1]:
        ax.set_visible(False)

plt.tight_layout()
out = DATASET_DIR / "stats_distribution.png"
plt.savefig(out, dpi=120)
print(f"Plot saved: {out}")

# --- Second figure: dataset overview ---
fig2, axes2 = plt.subplots(1, 3, figsize=(16, 5))
fig2.suptitle("DiverseMaps50 Dataset Overview", fontsize=14)

# 1. Train/Test split bar
splits = {"train_pointnav": len(data.get("train_pn",[])),
          "test_pointnav":  len(data.get("test_pn",[])),
          "train_objectnav":len(data.get("train_on",[])),
          "test_objectnav": len(data.get("test_on",[]))}
colors = ["steelblue","steelblue","crimson","crimson"]
bars = axes2[0].bar(splits.keys(), splits.values(), color=colors, alpha=0.85)
axes2[0].set_title("Record counts by split & task")
axes2[0].tick_params(axis='x', rotation=20, labelsize=9)
for bar, val in zip(bars, splits.values()):
    axes2[0].text(bar.get_x()+bar.get_width()/2, bar.get_height()+5, str(val),
                  ha='center', va='bottom', fontsize=9)

# 2. ObjectNav category pie (train + test combined)
if all_on:
    cats = collections.Counter(r.get("target_category","?") for r in all_on)
    labels = [f"{k}\n({v})" for k,v in cats.most_common()]
    axes2[1].pie(list(cats.values()), labels=labels, autopct='%1.0f%%',
                 startangle=90, textprops={'fontsize': 8})
    axes2[1].set_title("ObjectNav: target categories")

# 3. Maps per difficulty bucket (PN geodesic distance distribution)
if len(pn["geo"]) > 0:
    buckets = [0,10,20,30,40,50,60,70,80]
    counts, _ = __import__("numpy").histogram(pn["geo"], bins=buckets)
    bucket_labels = [f"{buckets[i]}-{buckets[i+1]}m" for i in range(len(buckets)-1)]
    axes2[2].bar(bucket_labels, counts, color="seagreen", edgecolor="white", alpha=0.85)
    axes2[2].set_title("PointNav: distance distribution")
    axes2[2].set_xlabel("Geodesic distance")
    axes2[2].set_ylabel("# tasks")
    axes2[2].tick_params(axis='x', rotation=30, labelsize=8)

plt.tight_layout()
out2 = DATASET_DIR / "stats_overview.png"
plt.savefig(out2, dpi=120)
print(f"Plot saved: {out2}")
