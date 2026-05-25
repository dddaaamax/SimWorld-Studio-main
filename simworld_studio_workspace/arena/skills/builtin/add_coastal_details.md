---
id: add_coastal_details
name: Add Coastal Atmosphere Details
version: 1.0.0
author: simworld-team
tags: [coastal, details, boat, driftwood, atmosphere]
dependencies: []
description: >
  Add a fishing boat and driftwood pieces to bring the coastal scene to life.
  Uses Asian_town boat and Lighthouse_Island driftwood assets.
---

# Add Coastal Atmosphere Details

## Required Assets (use these exact paths — do not substitute)

### Fishing Boat
- `spawn_actor` with `static_mesh="/Game/Asian_town/Assets/Boat/SM_boat_02.SM_boat_02"`
- `scale=[1,1,1]`, z=90 (water surface level), position near the bridge

### Driftwood
- Large log: `static_mesh="/Game/Lighthouse_Island/Meshes/SM_Sticks_Large_01.SM_Sticks_Large_01"`, `scale=[10,10,10]`, z=-40 (half-submerged at waterline)
- Smaller cluster: same mesh, `scale=[5,5,5]`, z=60
- Angled piece: `static_mesh="/Game/Lighthouse_Island/Meshes/SM_Sticks_Large_03.SM_Sticks_Large_03"`, `scale=[5,5,5]`, `rotation=[0,0,-70]`, z=90

## Key Rules
- Boat z=90 exactly (water surface)
- Large driftwood z=-40 simulates half-submerged floating log — this is intentional
- Place 2–3 driftwood pieces in a cluster near the island shore, not spread far apart
- Use SM_boat_02 only; do not try other boat meshes
