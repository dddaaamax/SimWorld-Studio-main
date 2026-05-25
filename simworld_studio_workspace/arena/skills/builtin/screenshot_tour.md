---
id: screenshot_tour
name: Screenshot Guide
version: 3.0.0
author: simworld-team
tags: [screenshot, capture]
dependencies: []
description: >
  Capture screenshots of the current scene using the take_screenshot tool.
---

# Screenshot Guide

## Taking Screenshots
```
Tool: take_screenshot
  filename: "my_shot.png"
```
Screenshots are saved to the `tmp/screens/` directory and displayed in the UI.

## Tips
- Take screenshots AFTER the scene is fully built (not during spawning)
- Name screenshots descriptively: "overview.png", "detail.png"
- The viewport camera is controlled by the user via Pixel Streaming
- Take a screenshot at the end of each scene generation so the user sees results
