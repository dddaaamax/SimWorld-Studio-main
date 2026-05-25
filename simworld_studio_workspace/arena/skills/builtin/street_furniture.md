---
id: street_furniture
name: Street Furniture & Props
version: 2.1.0
author: simworld-team
tags: [props, furniture, trees, vehicles, decoration]
dependencies: []
description: >
  Add trees, benches, scooters, carts, and other props to make
  scenes feel lived-in and realistic.
---

# Street Furniture & Props

## Overview
Props and furniture bring life to city scenes. Place them along streets,
in parks, near buildings, and at intersections.

## Available Props by Category

### Trees (6 varieties)
- `BP_Tree1` through `BP_Tree6`
- Height: ~500-1500 units
- Spacing: 800-2000 units apart for natural look

### Street Furniture (use spawn_blueprint_actor)
Available:
- `BP_Hydrant` — fire hydrant
- `BP_Trash_bin_a`, `BP_Trash_bin_b` — trash bins
- `BP_Trash_can` — trash can
- `BP_Table`, `BP_Table2`, `BP_Table3` — outdoor tables
- `BP_RoadBlocker` — road barrier
- `BP_RoadCone` — traffic cone
- `BP_Couch` — outdoor couch

NOT available (do not use): BP_Box, BP_Box2, BP_Box3, BP_Can, BP_Can2, BP_Rabbish, BP_Soda1, BP_Soda2

### Vehicles
- `BP_Scooter_01` through `BP_Scooter_04` — parked scooters
- `BP_Cart`, `BP_Cart2` — pushcarts

## Spawning Trees
```
Tool: spawn_blueprint_actor
  actor_name: "tree_01"
  blueprint_id: "BP_Tree1"
  location: [500, 200, 0]
```

## Placement Patterns

### Tree-Lined Street
Place trees every 1500-2000 units along a road:
```
BP_Tree1 at (0, 500, 0)
BP_Tree3 at (1500, 500, 0)
BP_Tree2 at (3000, 500, 0)
BP_Tree5 at (4500, 500, 0)
```

### Park / Green Space
Cluster 4-8 trees with varied types and irregular spacing:
```
BP_Tree1 at (0, 0, 0)
BP_Tree3 at (600, 400, 0)
BP_Tree5 at (-300, 800, 0)
BP_Tree2 at (500, 1000, 0)
```

### Parked Vehicles
Place scooters near buildings, slightly offset from walls:
```
BP_Scooter_01 at (building_x + 200, building_y - 100, 0) yaw=45
BP_Scooter_03 at (building_x + 400, building_y - 100, 0) yaw=50
```

## Tips
- Mix tree varieties (BP_Tree1-6) for natural diversity
- Place props at Z=0 (ground level)
- Add slight random yaw rotation for natural feel
- Use 3-5 trees per city block for suburban, 1-2 for downtown
- Scooters look best in groups of 2-4 near building entrances
