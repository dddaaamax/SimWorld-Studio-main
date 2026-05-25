---
id: coastal_island_scene
name: Coastal Island Scene with Lighthouse
version: 1.0.0
author: simworld-team
tags: [coastal, island, lighthouse, water, medieval]
dependencies: []
description: >
  Generate a medieval coastal island scene with ocean water, a rocky island,
  lighthouse tower, and trees. Uses proven asset paths and positions.
---

# Coastal Island Scene with Lighthouse

## Overview
Create a complete coastal scene: ocean surface → rocky island → lighthouse tower → trees.
Call `setup_environment` first, then follow this order exactly.

## Required Assets (use these exact paths — do not substitute)
- **Water**: `spawn_actor` with `static_mesh="/Game/Medieval_Env/Nature/Water/SM_WaterPlane.SM_WaterPlane"`, `location=[0,0,90]`, `scale=[1,1,1]`
- **Island base**: `spawn_actor` with `static_mesh="/Game/simworld_100maps_3/Content/Medieval_Env/Nature/Cliffs_Rocks/SM_Island_01.SM_Island_01"`, `location=[0,0,-590]`, `scale=[3,3,3]` — z=-590 at scale 3 makes it emerge naturally from water at z=90
- **Lighthouse tower**: `spawn_actor` with `static_mesh="/Game/Lighthouse_Island/Meshes/SM_Tower.SM_Tower"`, place on island plateau at z=80, `scale=[1,1,1]`
- **Trees (oak)**: `spawn_actor` with `static_mesh="/Game/simworld_100maps_3/Content/Medieval_Environment/Real_Landscape/Default/Meshes/Trees/SM_White_Oak_01.SM_White_Oak_01"`, `scale=[0.5,0.5,0.5]`, z≈250–350 (island surface height)

## Key Rules
- Place directly at final scale — no trial placements needed
- Water sits at z=90; island at z=-590, scale [3,3,3] creates correct emergence height
- Trees on island plateau: z≈250 near center, z≈300–400 on outer edges
- After placing all objects, call `verify_scene` to check placement quality before finishing
