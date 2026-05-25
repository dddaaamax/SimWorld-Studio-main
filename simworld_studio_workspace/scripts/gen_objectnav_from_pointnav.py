"""Generate ObjectNav records from existing PointNav JSONL (no UE needed).

For each PointNav record that doesn't have a corresponding ObjectNav record,
create an ObjectNav record: same start/GT path/geodesic, goal replaced with
a random BP object description. The goal coordinates are the same as PN,
just not written to the ON record (agent gets description, not coordinates).

Usage:
  python3 scripts/gen_objectnav_from_pointnav.py
"""
import json, pathlib, random, collections

DATASET = pathlib.Path(__file__).parent.parent / "datasets" / "diverse50"

BP_DESCS = [
    ("fire_hydrant",  "a red fire hydrant"),
    ("trash_bin",     "a trash bin"),
    ("tree",          "a large tree"),
    ("bench",         "a wooden bench"),
    ("traffic_cone",  "an orange traffic cone"),
    ("street_lamp",   "a street lamp"),
]

def load_jsonl(p):
    return [json.loads(l) for l in open(p) if l.strip()] if p.exists() else []

def save_jsonl(records, p, mode="a"):
    with open(p, mode) as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

for split in ("train", "test"):
    pn_path = DATASET / f"{split}_pointnav.jsonl"
    on_path = DATASET / f"{split}_objectnav.jsonl"

    pn_records = load_jsonl(pn_path)
    on_records  = load_jsonl(on_path)

    # Build set of existing ON episode_ids (normalize _pn_ → _on_)
    existing_on = {r["episode_id"] for r in on_records}

    rng = random.Random(42)
    new_on = []
    by_map = collections.defaultdict(list)

    for pn in pn_records:
        # Derive the expected ON episode_id
        on_id = pn["episode_id"].replace("_pn_", "_on_").replace("_obj_pn_", "_on_")
        if on_id in existing_on:
            continue  # already have it

        cat, desc = rng.choice(BP_DESCS)
        new_on.append({
            "episode_id":         on_id,
            "map":                pn["map"],
            "umap_path":          pn["umap_path"],
            "split":              split,
            "task_type":          "objectnav",
            "start_position":     pn["start_position"],
            "start_heading_deg":  pn["start_heading_deg"],
            "target_category":    cat,
            "target_description": desc,
            "gt_path":            pn["gt_path"],
            "geodesic_distance_cm": pn["geodesic_distance_cm"],
            "success_criteria":   pn.get("success_criteria",
                                         {"success_distance_cm": 200.0, "max_steps": 60}),
        })
        by_map[pn["map"]].append(on_id)

    if new_on:
        save_jsonl(new_on, on_path, mode="a")
        print(f"{split}: added {len(new_on)} ON records across {len(by_map)} maps")
    else:
        print(f"{split}: nothing to add")

print("\nFinal counts:")
for split in ("train", "test"):
    for task in ("pointnav", "objectnav"):
        p = DATASET / f"{split}_{task}.jsonl"
        n = sum(1 for _ in open(p)) if p.exists() else 0
        print(f"  {split}_{task}: {n}")
