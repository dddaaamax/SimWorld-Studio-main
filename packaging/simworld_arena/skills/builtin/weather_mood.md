---
id: weather_mood
name: Environment & Lighting
version: 2.0.0
author: simworld-team
tags: [weather, lighting, atmosphere, environment]
dependencies: []
description: >
  Control scene lighting, sun direction, fog, and atmosphere using
  setup_environment and execute_python_script tools.
---

# Environment & Lighting

## Overview
The `setup_environment` tool creates the base lighting setup: SkyAtmosphere,
DirectionalLight (sun), SkyLight, ExponentialHeightFog, and a ground plane.
Fine-tune with `execute_python_script`.

## Basic Setup
```
Tool: setup_environment
```
This spawns all needed lighting actors with good defaults.

## Adjusting Sun Direction
Use `execute_python_script` to modify the sun after setup:

```python
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for a in subsys.get_all_level_actors():
    if a.get_actor_label() == "Arena_Env_Sun":
        a.set_actor_rotation(unreal.Rotator(pitch=-45.0, yaw=180.0, roll=0.0), False)
        break
```

### Sun Pitch Guide
- `pitch=-80`: Night (sun below horizon)
- `pitch=-10`: Dawn/dusk
- `pitch=5`: Sunrise/sunset (golden hour)
- `pitch=30`: Morning/afternoon
- `pitch=60`: Midday

### Sun Yaw Guide
- `yaw=0`: Sun from north
- `yaw=90`: Sun from east (morning look)
- `yaw=180`: Sun from south (bright, even lighting)
- `yaw=270`: Sun from west (afternoon/sunset look)

## Adjusting Light Intensity
```python
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for a in subsys.get_all_level_actors():
    if a.get_actor_label() == "Arena_Env_Sun":
        comp = a.get_component_by_class(unreal.DirectionalLightComponent)
        comp.set_intensity(5.0)  # Default is 10.0
        break
```

## Fog Control
```python
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for a in subsys.get_all_level_actors():
    if a.get_actor_label() == "Arena_Env_Fog":
        fc = a.get_component_by_class(unreal.ExponentialHeightFogComponent)
        fc.set_editor_property("fog_density", 0.01)  # 0.002=light, 0.05=heavy
        break
```

## Mood Presets

### Bright Midday
- Sun pitch=60, yaw=180, intensity=10
- Fog density=0.001

### Golden Sunset
- Sun pitch=5, yaw=270, intensity=6
- Fog density=0.005

### Moody Overcast
- Sun pitch=30, yaw=200, intensity=3
- Fog density=0.02

### Night Scene
- Sun pitch=-80, yaw=0, intensity=0.5
- Fog density=0.003

## Tips
- Always call `setup_environment` before modifying lighting
- Use keyword args for Rotator: `unreal.Rotator(pitch=X, yaw=Y, roll=0)`
- SkyLight uses `set_editor_property("intensity", val)` not `set_intensity()`
- Lower sun intensity for softer shadows
