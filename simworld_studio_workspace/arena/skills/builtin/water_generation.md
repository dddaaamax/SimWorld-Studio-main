---
id: water_generation
name: Water Generation
version: 1.0.0
author: simworld-team
tags: [water, environment, nature, medieval]
dependencies: []
description: >
  Generate water surfaces using the Medieval_Env water plane mesh.
  Useful for creating lakes, rivers, ponds, and other water features.
---

# Water Generation

## Overview
Add water surfaces to your scene using the Medieval Environment water plane mesh.
The water plane can be scaled and positioned to create various water features.

## Water Mesh Path
The water mesh is located at:
```
/Game/Medieval_Env/Nature/Water/SM_WaterPlane.SM_WaterPlane
```

## Spawning Water

### Basic Water Plane
```
Tool: spawn_actor
  name: "Water_01"
  static_mesh: "/Game/Medieval_Env/Nature/Water/SM_WaterPlane.SM_WaterPlane"
  location: [0, -500, 20.66]
  rotation: [0, 0, 0]
  scale: [1, 1, 1]
```

### Parameters
- **name**: Unique identifier for the water actor (e.g., "Water_01", "Lake_Center", "River_Section_1")
- **static_mesh**: Always use `/Game/Medieval_Env/Nature/Water/SM_WaterPlane.SM_WaterPlane`
- **location**: [x, y, z] in UE units (cm)
  - Z coordinate: Typically 200-500 units above ground level for water surface
  - X, Y: Position of the water feature
- **rotation**: [pitch, yaw, roll] in degrees (usually [0, 0, 0] for flat water)
- **scale**: [x, y, z] scale multipliers
  - Use larger scales (e.g., [5, 5, 1]) for lakes
  - Use elongated scales (e.g., [10, 2, 1]) for rivers

## Common Patterns

### Small Pond
```
Water_01 at (0, 0, 250) scale=[3, 3, 1]
```

### Lake
```
Water_01 at (0, 0, 200) scale=[10, 10, 1]
```

### River
```
Water_01 at (0, -500, 206.6) scale=[20, 2, 1]
Water_02 at (0, -1000, 206。6) scale=[20, 2, 1]
```

### Multiple Water Features
```
Lake at (0, 0, 20) scale=[8, 8, 1]
Pond_1 at (5000, 3000, 25) scale=[2, 2, 1]
Pond_2 at (-5000, 3000, 25) scale=[2, 2, 1]
```

## Tips
- Water Z coordinate should be slightly above ground (20-50 units) to avoid clipping
- Use larger X/Y scales for bigger water bodies
- Keep Z scale at 1.0 for flat water surfaces
- Place water features before adding surrounding terrain/vegetation
- Multiple water planes can be placed adjacent to each other for larger areas
- Consider placing bridges or paths over water features for realism
