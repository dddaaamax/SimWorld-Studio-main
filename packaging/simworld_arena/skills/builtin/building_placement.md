---
id: building_placement
name: Building Placement & Spacing
version: 2.1.0
author: simworld-team
tags: [buildings, placement, spacing, architecture]
dependencies: []
description: >
  Detailed guide for placing individual buildings with correct spacing,
  rotation, and scale. Covers the 6 available building types and their sizes.
---

# Building Placement & Spacing

## Overview
Each building blueprint has different dimensions. Proper spacing prevents
overlapping and creates realistic urban layouts.

IMPORTANT: Only 6 building types are available in this package (BP_Building_01 through BP_Building_06). Do NOT use any building ID above 06.

## Spawning a Building
```
Tool: spawn_blueprint_actor
  actor_name: "my_building"
  blueprint_name: "/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C"
  location: [x, y, z]
  rotation: [0, yaw, 0]
```

## Building Size Categories

| Range | Type | Approx Height | Spacing |
|-------|------|---------------|---------|
| 01-03 | Small residential | 1500-3000 | 3000-5000 |
| 04-06 | Medium building | 3000-6000 | 5000-8000 |

## Rotation Guide
- `yaw: 0` — faces +X direction
- `yaw: 90` — faces +Y direction
- `yaw: 180` — faces -X direction
- `yaw: 270` — faces -Y direction
- For street-facing rows, align yaw perpendicular to the street

## Common Patterns

### L-Shaped Block
```
Building A at (0, 0, 0) yaw=0
Building B at (3000, 0, 0) yaw=0
Building C at (0, 3000, 0) yaw=90
```

### Mixed-Use Block
Place medium buildings in the center, small residential around edges:
```
Center: BP_Building_04 at (0, 0, 0)
Edges:  BP_Building_01-03 at offsets of 5000-8000
```

### Neighborhood Grid
Use varied buildings with proper spacing:
```
BP_Building_01 at (0, 0, 0)
BP_Building_03 at (5000, 0, 0)
BP_Building_05 at (0, 6000, 0)
BP_Building_02 at (5000, 6000, 0)
```

## Tips
- Always check building bounds with `get_actors_in_level` after spawning
- Z=0 is ground level — all buildings should be placed at z=0
- Use unique `actor_name` for each building to manage them later
- Combine with trees and street furniture for realism
- Vary the building IDs (01-06) to create visual diversity
