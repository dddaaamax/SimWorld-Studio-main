"""
Bagua (Eight Trigrams) Formation — Reusable UE Python Script
=============================================================
Spawns a complete classical Chinese Bagua formation using tree assets.
Later Heaven (King Wen) arrangement with 3 layers:
  1. Trigram Lines — 24 rows of trees encoding yin/yang lines
  2. Maze Walls    — 8 radial barriers with passage gaps
  3. Harmony Pass  — gradient-scaled boundary trees

Usage via execute_python_script MCP tool:
  execute_python_script(script=open('scripts/spawn_bagua_formation.py').read())

Configuration:
  Adjust CENTER, FORMATION_RADIUS, STEP_MAG below to reposition/resize.
"""
import unreal
import math

# ── Configuration ──
CENTER = (0, 0, 0)
FORMATION_RADIUS = 1.0  # scale multiplier (1.0 = default ~13000 unit diameter)

# ── Helper ──
def spawn_tree(name, tree_id, x, y, z=0, yaw=0, scale=1.0):
    bp_path = '/Game/CityDatabase/blueprints/{}.{}_C'.format(tree_id, tree_id)
    bp_class = unreal.load_class(None, bp_path)
    if not bp_class:
        unreal.log_warning('Failed to load ' + bp_path)
        return None
    loc = unreal.Vector(
        CENTER[0] + x * FORMATION_RADIUS,
        CENTER[1] + y * FORMATION_RADIUS,
        CENTER[2] + z
    )
    rot = unreal.Rotator(0, yaw, 0)
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = subsystem.spawn_actor_from_class(bp_class, loc, rot)
    if actor:
        actor.set_actor_label(name)
        actor.set_actor_scale3d(unreal.Vector(scale, scale, scale))
    return actor

count = 0

# ═══════════════════════════════════════════════════
# BAGUA DEFINITION — Later Heaven (King Wen) Order
# ═══════════════════════════════════════════════════
# (name, math_angle_deg, [L1_solid, L2_solid, L3_solid])
# L1=inner, L3=outer; True=yang(solid), False=yin(broken)
TRIGRAMS = [
    ('kan',  90,   [False, True,  False]),   # 0 N  — Water  ☵
    ('gen',  45,   [False, False, True]),     # 1 NE — Mountain ☶
    ('zhen', 0,    [True,  False, False]),    # 2 E  — Thunder ☳
    ('xun',  315,  [False, True,  True]),     # 3 SE — Wind ☴
    ('li',   270,  [True,  False, True]),     # 4 S  — Fire ☲
    ('kun',  225,  [False, False, False]),    # 5 SW — Earth ☷
    ('dui',  180,  [True,  True,  False]),    # 6 W  — Lake ☱
    ('qian', 135,  [True,  True,  True]),     # 7 NW — Heaven ☰
]

LINE_RADII = [3500, 5000, 6500]
STEP_MAG = 350
TREE_ASSETS = {
    (0, 0): 'BP_Tree1', (0, 1): 'BP_Tree2',  # inner
    (1, 0): 'BP_Tree3', (1, 1): 'BP_Tree4',  # middle
    (2, 0): 'BP_Tree5', (2, 1): 'BP_Tree6',  # outer
}
TREE_SCALES = [1.0, 1.2, 0.9]

# ═══════════════════════════════════
# PHASE 1: Trigram Lines (24 rows)
# ═══════════════════════════════════
for s_idx, (tname, angle_deg, lines) in enumerate(TRIGRAMS):
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)
    tan_x, tan_y = -sin_a, cos_a

    for l_idx, is_solid in enumerate(lines):
        r = LINE_RADII[l_idx]
        cx, cy = r * cos_a, r * sin_a
        tree_id = TREE_ASSETS[(l_idx, s_idx % 2)]
        sc = TREE_SCALES[l_idx]

        for i, offset in enumerate([-2, -1, 0, 1, 2]):
            if not is_solid and offset == 0:
                continue
            tx = cx + offset * STEP_MAG * tan_x
            ty = cy + offset * STEP_MAG * tan_y
            name = 'bagua_{}_{}_L{}_{}'.format(tname, s_idx, l_idx + 1, i)
            spawn_tree(name, tree_id, tx, ty, 0, angle_deg, sc)
            count += 1

# ═══════════════════════════════════
# PHASE 2: Maze Walls (8 radial)
# ═══════════════════════════════════
BOUNDARY_ANGLES = [67.5, 22.5, 337.5, 292.5, 247.5, 202.5, 157.5, 112.5]
WALL_GAPS = [3, 2, 4, 1, 5, 3, 2, 4]
WALL_TREES = ['BP_Tree3', 'BP_Tree4', 'BP_Tree3', 'BP_Tree4',
              'BP_Tree3', 'BP_Tree4', 'BP_Tree3', 'BP_Tree4']
WALL_NAMES = ['N_NE', 'NE_E', 'E_SE', 'SE_S', 'S_SW', 'SW_W', 'W_NW', 'NW_N']

for w_idx in range(8):
    a = math.radians(BOUNDARY_ANGLES[w_idx])
    cos_a, sin_a = math.cos(a), math.sin(a)
    origin_x, origin_y = 3000 * cos_a, 3000 * sin_a
    step_x, step_y = 500 * cos_a, 500 * sin_a

    for t in range(7):
        if t == WALL_GAPS[w_idx]:
            continue
        tx = origin_x + t * step_x
        ty = origin_y + t * step_y
        name = 'maze_{}_{}'.format(WALL_NAMES[w_idx], t)
        spawn_tree(name, WALL_TREES[w_idx], tx, ty, 0, BOUNDARY_ANGLES[w_idx], 0.8)
        count += 1

# ═══════════════════════════════════
# PHASE 3: Landscape Harmony (8 sectors)
# ═══════════════════════════════════
HARMONY_TREES = ['BP_Tree1', 'BP_Tree2', 'BP_Tree3', 'BP_Tree4', 'BP_Tree5', 'BP_Tree6']

for s_idx in range(8):
    angle_deg = 90 - s_idx * 45
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)

    inner_r, outer_r = 7000, 9000
    num_trees = 8

    for t in range(num_trees):
        frac = t / max(num_trees - 1, 1)
        r = inner_r + frac * (outer_r - inner_r)
        angle_offset = math.radians((t - num_trees / 2.0) * 2.5)
        aa = a + angle_offset
        tx = r * math.cos(aa)
        ty = r * math.sin(aa)
        sc = 0.7 + frac * 0.6
        tree_id = HARMONY_TREES[t % len(HARMONY_TREES)]
        name = 'harmony_s{}_{}'.format(s_idx, t)
        spawn_tree(name, tree_id, tx, ty, 0, 0, sc)
        count += 1

unreal.log('Bagua formation complete: {} trees spawned'.format(count))
