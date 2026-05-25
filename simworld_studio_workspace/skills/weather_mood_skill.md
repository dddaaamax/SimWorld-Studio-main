---
id: "weather_mood_skill"
name: "Weather Mood Skill"
version: "1.0.0"
author: "evolution-engine"
tags: ["learned", "linked-tool"]
dependencies: []
description: "Linked usage guidance for learned tool learned__weather_mood"
createdByBatchId: "b30525dc-4a67-4658-98d3-ace17d063b37"
updatedByBatchIds: []
sourceSessionIds: ["prompt_mn829kr9_dxmht7"]
reusedByBatchIds: []
provenance_session: "prompt_mn829kr9_dxmht7"
---

## Weather Mood Setter

### When To Use
This skill was linked from subpart: "Set the scene lighting and atmosphere to a sunset mood".
Use `learned__weather_mood` for requests that match this reusable operation.

### Execution Policy
Call `learned__weather_mood` first when its parameters can represent the request.
Fallback to primitive calls only for behavior that cannot be represented by this tool.

### Parameterization
Keep requests parameterized so the tool can generalize across scenes.
