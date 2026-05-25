---
id: add_fog_atmosphere
name: Add Atmospheric Fog Volume
version: 1.0.0
author: simworld-team
tags: [fog, atmosphere, mood, volumetric]
dependencies: []
description: >
  Add a large volumetric fog sphere to create atmospheric coastal haze.
  Uses UrbanDistrict fog mesh at high altitude.
---

# Add Atmospheric Fog Volume

## Required Asset (use this exact path — do not substitute)
- `spawn_actor` with `static_mesh="/Game/UrbanDistrict/Effects/Fog_01/sm_Fog_01_01.sm_Fog_01_01"`
- `scale=[10,10,10]` — smaller scales produce no visible effect
- `location=[near scene center, z=2500]` — must be at high altitude to create overhead haze
- `rotation=[0,0,0]`

## Key Rules
- Scale must be ≥ 8; the fog mesh is only visible at large scales
- One fog volume is sufficient; do not place multiple
- Z altitude 2000–3000 is correct; lower placement creates ground fog instead of atmospheric haze
- The fog automatically picks up sun color from `setup_environment` lighting
