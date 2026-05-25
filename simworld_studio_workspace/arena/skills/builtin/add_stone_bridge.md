---
id: add_stone_bridge
name: Add Stone Bridge Chain
version: 1.0.0
author: simworld-team
tags: [bridge, medieval, stone, coastal]
dependencies: []
description: >
  Add a chain of medieval stone bridge segments extending from the island
  into the sea. Uses CastleRiver SM_Bridge01 at scale 0.5.
---

# Add Stone Bridge Chain

## Overview
Place 5–6 bridge segments in a straight line from the island shore outward.

## Required Assets (use these exact paths — do not substitute)
- **Bridge segment**: `spawn_actor` with `static_mesh="/Game/CastleRiver/Meshes/SM_Bridge01.SM_Bridge01"`, `scale=[0.5,0.5,0.5]`
- **End cap (optional)**: `static_mesh="/Game/CastleRiver/Meshes/SM_Bridge03.SM_Bridge03"`, same scale, for the final segment only

## Placement Pattern
- Scale: always `[0.5, 0.5, 0.5]` — this pack's meshes are large; scale 1 dwarfs the island
- Spacing: ~800 UE units between segment centers along the bridge direction
- Z: ~480–500 (slightly above water to sit at shoreline height)
- Yaw: choose one angle and keep all segments consistent
- Start near the island shore, chain segments outward

## Key Rules
- Place directly at scale [0.5,0.5,0.5] — no other scale testing needed
- Use only CastleRiver SM_Bridge01; do not try other bridge asset packs
- 5–6 segments total; SM_Bridge03 as final end cap adds visual variety
