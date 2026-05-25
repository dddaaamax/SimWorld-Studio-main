---
id: agent_control
name: Agent Control
version: 1.0.0
author: simworld
tags: [agent, movement, navigation, control, pedestrian, humanoid]
dependencies: []
description: Control humanoid and pedestrian agents in SimWorld — movement, rotation, path following, and actions.
---

## Agent Control Skill

This skill enables controlling spawned agents (humanoids and pedestrians) in a SimWorld scene.

### Spawning Agents

Use `spawn_agent` to create controllable agents:

```
spawn_agent(agent_name="Pedestrian_1", agent_type="pedestrian", location=[0, 0, 110])
spawn_agent(agent_name="Robot_1", agent_type="humanoid", location=[500, 0, 110])
```

- `agent_type="pedestrian"` — human NPC character
- `agent_type="humanoid"` — robot/user agent
- Z=110 for ground level

### Basic Movement

```
# Start moving forward (continuous)
agent_move_forward(agent_name="Pedestrian_1")

# Stop
agent_stop(agent_name="Pedestrian_1", agent_type="pedestrian")

# Move forward for specific duration then auto-stop
agent_step_forward(agent_name="Pedestrian_1", duration=3)
```

### Rotation

```
# Turn right 90 degrees
agent_rotate(agent_name="Pedestrian_1", angle=90, direction="right", agent_type="pedestrian")

# Turn left 45 degrees
agent_rotate(agent_name="Pedestrian_1", angle=45, direction="left", agent_type="pedestrian")
```

### Speed Control

```
# Slow walk
agent_set_speed(agent_name="Pedestrian_1", speed=100)

# Normal walk
agent_set_speed(agent_name="Pedestrian_1", speed=200)

# Running
agent_set_speed(agent_name="Pedestrian_1", speed=400)
```

### Path Following

```
# Set waypoints and auto-follow
agent_set_path(agent_name="Pedestrian_1", waypoints=[[300,300],[600,0],[300,-300],[0,0]])
```

### Actions (Humanoid only)

```
agent_action(agent_name="Robot_1", action="sit_down")
agent_action(agent_name="Robot_1", action="stand_up")
agent_action(agent_name="Robot_1", action="wave")
agent_action(agent_name="Robot_1", action="pick_up", target="Package_1")
agent_action(agent_name="Robot_1", action="drop_off")
agent_action(agent_name="Robot_1", action="discuss")
agent_action(agent_name="Robot_1", action="listen")
```

### Get Agent State

```
get_agent_state(agent_name="Pedestrian_1")
# Returns: { location: [x, y, z], rotation: [pitch, yaw, roll] }
```

### Example: Walk to a building

```
1. get_agent_state(agent_name="Pedestrian_1")    # Check current position
2. agent_set_speed(agent_name="Pedestrian_1", speed=200)
3. agent_rotate(agent_name="Pedestrian_1", angle=45, direction="right", agent_type="pedestrian")
4. agent_step_forward(agent_name="Pedestrian_1", duration=5)
5. get_agent_state(agent_name="Pedestrian_1")    # Verify new position
6. take_screenshot()                              # Visual confirmation
```
