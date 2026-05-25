#!/usr/bin/env python3
"""Evaluate all 9 scenes using the finalized metrics from METRICS_SPEC.md.

Rule-based metrics (offline, from scene graphs):
  CNT  — Object Count Accuracy
  DIV  — Asset Type Diversity
  COL  — Collision Rate (bounding-box approximation)
  GRAV — Gravity/Support Validity
  OOB  — Out-of-Bounds Rate
  PRES — Preservation Rate (Setting 3 only)
  ECNT — Edit Object Count (Setting 3 only)

VLM-as-Judge metrics (from screenshots via claude-cli):
  PF   — Prompt Fidelity
  SRF  — Spatial Relationship Fidelity (Setting 1)
  LAES — Layout Aesthetics (Setting 1)
  ILC  — Image Layout Correspondence (Setting 2)
  STY  — Style Consistency (Setting 2)
  EC   — Edit Completeness (Setting 3)
  SC   — Scene Coherence (Setting 3)
  LQ   — Layout Quality (Setting 3)
"""

import json, math, os, sys, subprocess, asyncio, re
from pathlib import Path
from collections import Counter

# ---------------------------------------------------------------------------
# Actor classification
# ---------------------------------------------------------------------------
def classify(class_name: str) -> str:
    c = class_name
    if "BP_Building" in c: return "building"
    if "BP_Tree" in c or "Tree" in c: return "tree"
    if "BP_Scooter" in c: return "vehicle"
    if "BP_Cart" in c: return "vehicle"
    if "BP_Table" in c or "BP_Couch" in c: return "furniture"
    if "BP_Trash" in c or "BP_Can" in c or "BP_Rabbish" in c: return "furniture"
    if "BP_Hydrant" in c: return "infrastructure"
    if "BP_RoadBlocker" in c or "BP_RoadCone" in c: return "infrastructure"
    if "SM_Road" in c or "road" in c.lower(): return "road"
    if "BP_Soda" in c or "BP_Box" in c: return "furniture"
    if "Arena_Env" in c: return "environment"
    if "StaticMeshActor" in c: return "road"  # roads are StaticMeshActors
    return "other"

def get_actors(sg: dict) -> list:
    """Extract meaningful actor list from scene graph."""
    raw = sg.get("result", {}).get("actors", []) or sg.get("actors", [])
    actors = []
    for a in raw:
        cls = a.get("class", "")
        label = a.get("label", a.get("name", ""))
        cat = classify(cls)
        if cat in ("environment", "other"):
            continue
        actors.append({
            "label": label,
            "class": cls,
            "category": cat,
            "location": a.get("location", [0, 0, 0]),
            "rotation": a.get("rotation", [0, 0, 0]),
            "scale": a.get("scale", [1, 1, 1]),
        })
    return actors

# ---------------------------------------------------------------------------
# Rule-based metrics
# ---------------------------------------------------------------------------

# Known approximate sizes for bounding-box collision estimation (half-extents in UE units)
APPROX_HALF_EXTENT = {
    "building": (800, 800, 2000),
    "tree": (400, 400, 800),
    "vehicle": (200, 100, 100),
    "furniture": (100, 100, 80),
    "infrastructure": (50, 50, 80),
    "road": (2000, 500, 10),
}

def metric_cnt(actors: list, prompt: str) -> float:
    """R1: Object Count Accuracy."""
    # Count actors by category
    counts = Counter(a["category"] for a in actors)

    # Simple heuristic: extract numbers from prompt
    # Look for patterns like "3 houses", "6 trees", etc.
    categories_mentioned = 0
    categories_correct = 0

    patterns = [
        (r'(\d+)\s*(?:small\s+)?(?:house|building|cottage)', "building"),
        (r'(\d+)\s*(?:tree|oak|pine)', "tree"),
        (r'(\d+)\s*(?:scooter|vehicle|cart)', "vehicle"),
        (r'(\d+)\s*(?:table|couch|bench|seat)', "furniture"),
        (r'(\d+)\s*(?:trash\s*bin|trash\s*can|bin)', "furniture"),
        (r'(\d+)\s*(?:hydrant|cone|blocker)', "infrastructure"),
        (r'(\d+)\s*(?:road)', "road"),
    ]

    for pattern, cat in patterns:
        m = re.search(pattern, prompt, re.IGNORECASE)
        if m:
            expected = int(m.group(1))
            actual = counts.get(cat, 0)
            categories_mentioned += 1
            # Tolerance: exact for small counts, ±1 for >=5
            tol = 1 if expected >= 5 else 0
            if abs(actual - expected) <= tol:
                categories_correct += 1

    if categories_mentioned == 0:
        # Fall back: just check if buildings and trees exist
        has_buildings = counts.get("building", 0) > 0
        has_trees = counts.get("tree", 0) > 0
        return 1.0 if (has_buildings or has_trees) else 0.0

    return categories_correct / categories_mentioned


def metric_div(actors: list) -> float:
    """R2: Asset Type Diversity."""
    if not actors:
        return 0.0
    distinct_classes = len(set(a["class"] for a in actors))
    total = len(actors)
    return min(distinct_classes / total, 1.0) if total > 0 else 0.0


def metric_col(actors: list) -> float:
    """R3: Collision Rate (lower is better, we return 1-collision_rate for consistency)."""
    non_road = [a for a in actors if a["category"] not in ("road",)]
    if len(non_road) < 2:
        return 1.0  # No collisions possible

    collisions = 0
    pairs = 0
    for i in range(len(non_road)):
        for j in range(i + 1, len(non_road)):
            a, b = non_road[i], non_road[j]
            he_a = APPROX_HALF_EXTENT.get(a["category"], (200, 200, 200))
            he_b = APPROX_HALF_EXTENT.get(b["category"], (200, 200, 200))

            la, lb = a["location"], b["location"]
            # AABB overlap check
            overlap = all(
                abs(la[k] - lb[k]) < (he_a[k] + he_b[k])
                for k in range(3)
            )
            if overlap:
                collisions += 1
            pairs += 1

    collision_rate = collisions / pairs if pairs > 0 else 0
    return 1.0 - collision_rate  # Higher is better (no collisions)


def metric_grav(actors: list) -> float:
    """R4: Gravity/Support Validity."""
    if not actors:
        return 1.0
    grounded = 0
    for a in actors:
        z = a["location"][2] if len(a["location"]) > 2 else 0
        # Object is grounded if Z is within [-200, 200] of ground
        if -200 <= z <= 200:
            grounded += 1
    return grounded / len(actors)


def metric_oob(actors: list) -> float:
    """R5: Out-of-Bounds Rate (percentage in-bounds)."""
    if not actors:
        return 1.0
    in_bounds = 0
    for a in actors:
        x, y = a["location"][0], a["location"][1]
        if abs(x) <= 9500 and abs(y) <= 9500:
            in_bounds += 1
    return in_bounds / len(actors)


def metric_pres(before_actors: list, after_actors: list) -> float:
    """R1-S3: Preservation Rate."""
    if not before_actors:
        return 1.0

    after_labels = {a["label"] for a in after_actors}
    after_by_label = {a["label"]: a for a in after_actors}

    preserved = 0
    for ba in before_actors:
        if ba["label"] in after_labels:
            aa = after_by_label[ba["label"]]
            # Check position within tolerance
            dist = math.sqrt(sum((ba["location"][k] - aa["location"][k])**2 for k in range(3)))
            if dist <= 200:  # 200 UE units tolerance
                preserved += 1

    return preserved / len(before_actors)


def metric_ecnt(before_actors: list, after_actors: list, edit_instruction: str) -> float:
    """R2-S3: Edit Object Count — fraction of new objects added."""
    before_labels = {a["label"] for a in before_actors}
    new_actors = [a for a in after_actors if a["label"] not in before_labels]

    # Count expected new objects from edit instruction
    expected_new = 0
    for m in re.finditer(r'(\d+)\s+(?:new\s+)?(?:more\s+)?(\w+)', edit_instruction, re.IGNORECASE):
        expected_new += int(m.group(1))

    if expected_new == 0:
        # Fallback: count action verbs
        add_count = len(re.findall(r'\badd\b', edit_instruction, re.IGNORECASE))
        place_count = len(re.findall(r'\bplace\b', edit_instruction, re.IGNORECASE))
        expected_new = max(add_count + place_count, 1)

    actual_new = len(new_actors)
    return min(actual_new / expected_new, 1.0) if expected_new > 0 else 1.0


# ---------------------------------------------------------------------------
# VLM metrics via claude-cli
# ---------------------------------------------------------------------------

def run_vlm_eval(rubric: str, prompt: str, screenshots: list, scene_graph: dict,
                 reference_image: str = None,
                 before_screenshots: list = None,
                 edit_instruction: str = None) -> dict:
    """Call claude-cli to score a scene. Returns {"score": 0-10, "reasoning": "..."}"""

    system = (
        "You are an expert 3D scene evaluator for SimWorld Studio (Unreal Engine 5).\n"
        "You will be given screenshots of a generated scene, the scene graph, "
        "and evaluation criteria. Score according to the rubric.\n\n"
        f"{rubric}"
    )

    # Build user prompt
    parts = [f"Prompt: {prompt!r}\n"]

    if reference_image:
        parts.append(f"\nReference image: {reference_image}")
    if edit_instruction:
        parts.append(f"\nEdit instruction: {edit_instruction!r}")

    # Scene graph summary
    actors = get_actors(scene_graph)
    cats = Counter(a["category"] for a in actors)
    parts.append(f"\nScene graph: {len(actors)} actors — " + ", ".join(f"{c}:{n}" for c, n in cats.items()))

    img_section = ""
    all_images = []
    if reference_image and Path(reference_image).exists():
        all_images.append(str(Path(reference_image).resolve()))
    if before_screenshots:
        all_images.extend(str(Path(s).resolve()) for s in before_screenshots if Path(s).exists())
    all_images.extend(str(Path(s).resolve()) for s in screenshots if Path(s).exists())

    if all_images:
        img_lines = "\n".join(f"  - {p}" for p in all_images[:8])  # Max 8 images
        img_section = (
            f"\n\nImage files to view:\n{img_lines}\n"
            "IMPORTANT: Use the Read tool to view each image file.\n"
        )

    full_prompt = system + "\n\n" + "\n".join(parts) + img_section

    cmd = ["claude", "-p", full_prompt, "--output-format", "text", "--dangerously-skip-permissions"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        text = proc.stdout.strip()

        # Parse JSON from response
        for start in range(len(text)):
            if text[start] == '{':
                depth = 0
                for end in range(start, len(text)):
                    if text[end] == '{': depth += 1
                    elif text[end] == '}': depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start:end+1])
                            return {
                                "score": float(data.get("score", 0)),
                                "reasoning": str(data.get("reasoning", "")),
                            }
                        except:
                            break

        # Fallback: try to find a number
        numbers = re.findall(r'\b(\d+(?:\.\d+)?)\s*/\s*10', text)
        if numbers:
            return {"score": float(numbers[0]), "reasoning": text[:200]}

        return {"score": 0, "reasoning": f"Could not parse: {text[:200]}"}
    except Exception as e:
        return {"score": 0, "reasoning": f"Error: {e}"}


# VLM Rubrics
RUBRIC_PF = (
    "Score PROMPT FIDELITY on 0-10:\n"
    "10=scene perfectly matches every aspect of the prompt\n"
    "7=most elements present, minor deviations\n"
    "5=partially matches, some spatial relationships wrong\n"
    "3=only vaguely related\n"
    "0=no relationship to prompt\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_SRF = (
    "Score SPATIAL RELATIONSHIP FIDELITY on 0-10:\n"
    "Evaluate whether described spatial relationships hold "
    "(e.g., 'trees line the street', 'buildings face each other', 'park in center').\n"
    "10=all spatial relationships correctly realized\n"
    "5=half the relationships hold\n"
    "0=no spatial relationships match\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_LAES = (
    "Score LAYOUT AESTHETICS on 0-10:\n"
    "Does the scene look like a plausible, well-designed real-world place?\n"
    "10=indistinguishable from hand-designed level\n"
    "5=recognizable but feels artificial\n"
    "0=random object scatter\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_ILC = (
    "Score IMAGE LAYOUT CORRESPONDENCE on 0-10:\n"
    "Compare the generated scene against the reference image.\n"
    "10=closely reproduces the reference layout\n"
    "7=major elements match, some shifts\n"
    "5=roughly half corresponds\n"
    "0=no resemblance\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_STY = (
    "Score STYLE CONSISTENCY on 0-10:\n"
    "Do the building types/scales/density match the reference image's implied style?\n"
    "10=perfect style match\n"
    "5=correct general category but wrong scale/density\n"
    "0=completely wrong style\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_EC = (
    "Score EDIT COMPLETENESS on 0-10:\n"
    "Compare BEFORE and AFTER screenshots. Was the edit instruction fully applied?\n"
    "10=edit completely and correctly applied\n"
    "5=partially applied\n"
    "0=not applied at all\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_SC = (
    "Score SCENE COHERENCE on 0-10:\n"
    "Does the post-edit scene still form a coherent, plausible environment?\n"
    "10=new objects integrate seamlessly\n"
    "5=present but somewhat out of place\n"
    "0=completely break coherence\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)

RUBRIC_LQ = (
    "Score LAYOUT QUALITY on 0-10:\n"
    "Are new objects placed at logical positions relative to existing structures?\n"
    "10=well-organized, logically integrated\n"
    "5=adequate but some issues\n"
    "0=chaotic placement\n\n"
    "Respond with ONLY JSON: {\"score\": <0-10>, \"reasoning\": \"<brief>\"}"
)


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_setting1(folder: str, prompt: str, difficulty: str) -> dict:
    sg = json.loads(Path(folder, "scene_graph.json").read_text())
    actors = get_actors(sg)
    screenshots = sorted(str(p) for p in Path(folder).glob("*.png") if "reference" not in p.name)

    # Rule-based
    cnt = metric_cnt(actors, prompt)
    div = metric_div(actors)
    col = metric_col(actors)
    grav = metric_grav(actors)
    oob = metric_oob(actors)

    # VLM
    pf = run_vlm_eval(RUBRIC_PF, prompt, screenshots, sg)
    srf = run_vlm_eval(RUBRIC_SRF, prompt, screenshots, sg)
    laes = run_vlm_eval(RUBRIC_LAES, prompt, screenshots, sg)

    return {
        "setting": "text_to_scene", "difficulty": difficulty, "folder": folder,
        "rule_based": {"CNT": cnt, "DIV": div, "COL": col, "GRAV": grav, "OOB": oob},
        "vlm": {"PF": pf["score"]/10, "SRF": srf["score"]/10, "LAES": laes["score"]/10},
        "vlm_reasoning": {"PF": pf["reasoning"], "SRF": srf["reasoning"], "LAES": laes["reasoning"]},
        "actor_count": len(actors),
    }


def evaluate_setting2(folder: str, prompt: str, ref_image: str, difficulty: str) -> dict:
    sg = json.loads(Path(folder, "scene_graph.json").read_text()) if Path(folder, "scene_graph.json").exists() else {}
    actors = get_actors(sg)
    screenshots = sorted(str(p) for p in Path(folder).glob("*.png") if "reference" not in p.name)
    ref_in_folder = str(Path(folder) / "reference.png") if Path(folder, "reference.png").exists() else ref_image

    # Rule-based
    cnt = metric_cnt(actors, prompt)
    col = metric_col(actors)
    grav = metric_grav(actors)

    # VLM
    ilc = run_vlm_eval(RUBRIC_ILC, prompt, screenshots, sg, reference_image=ref_in_folder)
    pf = run_vlm_eval(RUBRIC_PF, prompt, screenshots, sg)
    sty = run_vlm_eval(RUBRIC_STY, prompt, screenshots, sg, reference_image=ref_in_folder)

    return {
        "setting": "image_text_to_scene", "difficulty": difficulty, "folder": folder,
        "rule_based": {"CNT": cnt, "COL": col, "GRAV": grav},
        "vlm": {"ILC": ilc["score"]/10, "PF": pf["score"]/10, "STY": sty["score"]/10},
        "vlm_reasoning": {"ILC": ilc["reasoning"], "PF": pf["reasoning"], "STY": sty["reasoning"]},
        "actor_count": len(actors),
    }


def evaluate_setting3(after_folder: str, before_folder: str, edit_instruction: str, difficulty: str) -> dict:
    sg_after = json.loads(Path(after_folder, "scene_graph.json").read_text()) if Path(after_folder, "scene_graph.json").exists() else {}
    sg_before = json.loads(Path(before_folder, "scene_graph.json").read_text()) if Path(before_folder, "scene_graph.json").exists() else {}
    actors_after = get_actors(sg_after)
    actors_before = get_actors(sg_before)
    screenshots_after = sorted(str(p) for p in Path(after_folder).glob("*.png") if "reference" not in p.name)
    screenshots_before = sorted(str(p) for p in Path(before_folder).glob("*.png"))

    # Rule-based
    pres = metric_pres(actors_before, actors_after)
    ecnt = metric_ecnt(actors_before, actors_after, edit_instruction)
    col = metric_col(actors_after)

    # VLM
    ec = run_vlm_eval(RUBRIC_EC, edit_instruction, screenshots_after, sg_after,
                      before_screenshots=screenshots_before, edit_instruction=edit_instruction)
    sc = run_vlm_eval(RUBRIC_SC, "post-edit scene", screenshots_after, sg_after)
    lq = run_vlm_eval(RUBRIC_LQ, "post-edit scene", screenshots_after, sg_after)

    return {
        "setting": "scene_editing", "difficulty": difficulty,
        "after_folder": after_folder, "before_folder": before_folder,
        "rule_based": {"PRES": pres, "ECNT": ecnt, "COL": col},
        "vlm": {"EC": ec["score"]/10, "SC": sc["score"]/10, "LQ": lq["score"]/10},
        "vlm_reasoning": {"EC": ec["reasoning"], "SC": sc["reasoning"], "LQ": lq["reasoning"]},
        "actors_before": len(actors_before), "actors_after": len(actors_after),
    }


def main():
    results_dir = Path("/home/murray/simworld_studio_static_eval/results")

    # Load scene info
    s1_summary = json.loads((results_dir / "text_to_scene/batch_summary.json").read_text())
    all_results = json.loads((results_dir / "all_results.json").read_text())

    prompts_s1 = [
        "Build a quiet street corner with 3 small houses in a row, a tree between each house, and a trash bin on the sidewalk.",
        "Create a town plaza surrounded by 4 buildings on each side of a central open area. Place outdoor seating with tables and a couch in the middle. Add trees around the perimeter, a road along one edge with parked scooters, and trash bins at the corners.",
        "Design a full residential neighborhood. Build two parallel streets with buildings on both sides: place 3 buildings along the north side and 3 buildings along the south side of each street (total 6 buildings in two rows). Connect the streets with a cross road. Line all roads with trees on both sides. Create a central park between the two streets with tables, couches, and trash bins. Add a fire hydrant at every street intersection. Place scooters and carts parked along the roads. Mark one intersection with road cones and road blockers as a construction zone.",
    ]

    prompts_s2 = [
        "Build a scene matching this sketch: a row of houses along a street with some greenery.",
        "Build a scene matching this sketch: a square open area with trees at the four corners and street furniture in the center, like a small park or plaza.",
        "Build a dense urban block matching this aerial photo: multiple buildings arranged in a grid pattern with streets between them, trees lining the streets, and vehicles and street furniture throughout.",
    ]

    edits_s3 = [
        "Add 2 trash bins near the center of the scene and place 1 fire hydrant near one of the buildings.",
        "Add a road along one edge of the plaza connecting two buildings. Place 2 scooters parked near the road. Add 3 more trees to fill gaps in the perimeter. Add 2 tables to extend the seating area.",
        "Expand the plaza into a larger district: add 2 new buildings on the north side. Add roads connecting the new buildings to the existing ones. Plant 6 more trees to line the new roads. Create a marketplace area with 3 tables and 2 carts near the center. Add road cones and road blockers to mark a construction zone near the new buildings. Place fire hydrants at each road intersection and trash bins along the sidewalks.",
    ]

    ref_images_s2 = [
        "/home/murray/SimWorld-Studio-Dev/scene_eval/image_ref/S2-E-01.png",
        "/home/murray/SimWorld-Studio-Dev/scene_eval/image_ref/S2-E-02.png",
        "/home/murray/SimWorld-Studio-Dev/scene_eval/image_ref/city_4.jpg",
    ]

    diffs = ["easy", "mid", "hard"]
    all_eval = []

    # Setting 1
    print("=" * 60)
    print("SETTING 1: TEXT-TO-SCENE")
    print("=" * 60)
    for i, diff in enumerate(diffs):
        folder = s1_summary["results"][i]["folder"]
        print(f"\n[S1-{diff}] {folder}")
        r = evaluate_setting1(folder, prompts_s1[i], diff)
        all_eval.append(r)
        print(f"  Rule: CNT={r['rule_based']['CNT']:.2f} DIV={r['rule_based']['DIV']:.2f} COL={r['rule_based']['COL']:.2f} GRAV={r['rule_based']['GRAV']:.2f} OOB={r['rule_based']['OOB']:.2f}")
        print(f"  VLM:  PF={r['vlm']['PF']:.2f} SRF={r['vlm']['SRF']:.2f} LAES={r['vlm']['LAES']:.2f}")

    # Setting 2
    print("\n" + "=" * 60)
    print("SETTING 2: IMAGE+TEXT-TO-SCENE")
    print("=" * 60)
    for i, diff in enumerate(diffs):
        s2r = all_results["setting2"][i]
        folder = s2r["result"]["folder"] if s2r.get("result") else None
        if not folder:
            print(f"\n[S2-{diff}] SKIPPED (no result)")
            continue
        print(f"\n[S2-{diff}] {folder}")
        r = evaluate_setting2(folder, prompts_s2[i], ref_images_s2[i], diff)
        all_eval.append(r)
        print(f"  Rule: CNT={r['rule_based']['CNT']:.2f} COL={r['rule_based']['COL']:.2f} GRAV={r['rule_based']['GRAV']:.2f}")
        print(f"  VLM:  ILC={r['vlm']['ILC']:.2f} PF={r['vlm']['PF']:.2f} STY={r['vlm']['STY']:.2f}")

    # Setting 3
    print("\n" + "=" * 60)
    print("SETTING 3: SCENE EDITING")
    print("=" * 60)
    for i, diff in enumerate(diffs):
        s3r = all_results["setting3"][i]
        after_folder = s3r["result"]["folder"] if s3r.get("result") else None
        before_folder = s3r.get("before_folder", "")
        if not after_folder:
            print(f"\n[S3-{diff}] SKIPPED")
            continue
        print(f"\n[S3-{diff}] after={after_folder}")
        r = evaluate_setting3(after_folder, before_folder, edits_s3[i], diff)
        all_eval.append(r)
        print(f"  Rule: PRES={r['rule_based']['PRES']:.2f} ECNT={r['rule_based']['ECNT']:.2f} COL={r['rule_based']['COL']:.2f}")
        print(f"  VLM:  EC={r['vlm']['EC']:.2f} SC={r['vlm']['SC']:.2f} LQ={r['vlm']['LQ']:.2f}")

    # Save
    output_path = results_dir / "full_evaluation.json"
    output_path.write_text(json.dumps(all_eval, indent=2))
    print(f"\n\nFull results saved to {output_path}")

    return all_eval


if __name__ == "__main__":
    main()
