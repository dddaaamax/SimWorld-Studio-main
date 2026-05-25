---
id: city_layout
name: City Block Layout
version: 2.1.0
author: simworld-team
tags: [city, layout, planning, buildings, roads]
dependencies: []
description: >
  Plan and build city blocks with buildings, roads, and trees using
  MCP tools. Covers grid layouts and neighborhood areas.
---

# City Block Layout

## Overview
Use `spawn_blueprint_actor` to place buildings and roads in organized city blocks.
Always call `setup_environment` first for lighting, then build layouts systematically.

## Building Assets (ONLY 6 available)
- **Small residential** (01-03): `BP_Building_01` to `BP_Building_03`
- **Medium** (04-06): `BP_Building_04` to `BP_Building_06`

IMPORTANT: Do NOT use any building ID above 06. Only BP_Building_01 through BP_Building_06 are available in this package.

Blueprint path format: `/Game/CityDatabase/blueprints/BP_Building_XX.BP_Building_XX_C`

## Layout Patterns

### Grid Block (4 buildings around a center)
```
spawn_blueprint_actor: BP_Building_01 at (-1500, -1500, 0)
spawn_blueprint_actor: BP_Building_02 at (1500, -1500, 0)
spawn_blueprint_actor: BP_Building_03 at (-1500, 1500, 0)
spawn_blueprint_actor: BP_Building_04 at (1500, 1500, 0)
```

### Street-Facing Row
Place buildings along one axis with consistent spacing:
- Small residential (01-03): spacing ~3000-5000 units apart
- Medium (04-06): spacing ~5000-8000 units apart

### Neighborhood Cluster
1. Place 4-6 buildings (01-06) in a rough grid
2. Add trees between buildings (BP_Tree1 through BP_Tree6)
3. Add a road segment along one edge

## Camera Recommendations
- **Overview**: altitude 8000-15000, pitch -60 to -90
- **Street level**: altitude 300-500, pitch -5 to -15
- **Small buildings** (01-03): camera at 3000-8000 altitude
- **Medium buildings** (04-06): camera at 5000-12000 altitude

## Tips
- Always `setup_environment` before placing anything
- Use `delete_all_spawned` to clear the scene
- Small buildings (01-03) are ~1500-3000 units tall, medium (04-06) are ~3000-6000 units tall
- Rotate buildings with yaw (0, 90, 180, 270) to face streets
- Set `ld_max_draw_distance=0` on actors to prevent culling at distance
- Use all 6 building varieties for visual diversity
