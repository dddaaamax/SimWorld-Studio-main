#!/usr/bin/env node
"use strict";

const net = require("net");
const fs = require("fs");
const path = require("path");

const UNREAL_HOST = "127.0.0.1";
const UNREAL_PORT = 55559;
const THUMB_DIR = path.resolve(__dirname, "../../tmp/thumbnails");
const ASSETS = JSON.parse(fs.readFileSync(path.join(__dirname, "assets.json"), "utf-8"));
fs.mkdirSync(THUMB_DIR, { recursive: true });

const args = process.argv.slice(2);
let category = null, startIdx = 0, endIdx = Infinity;
for (let t = 0; t < args.length; t++) {
  if (args[t] === "--category" && args[t + 1]) category = args[++t];
  if (args[t] === "--start" && args[t + 1]) startIdx = parseInt(args[++t]);
  if (args[t] === "--end" && args[t + 1]) endIdx = parseInt(args[++t]);
}

function ueCommandOnce(type, params, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    const timer = setTimeout(() => { sock.destroy(); reject(new Error("Timeout")); }, timeout);
    let buf = "";
    sock.connect(UNREAL_PORT, UNREAL_HOST, () => {
      sock.write(JSON.stringify({ type, params }) + "\n");
    });
    sock.on("data", (d) => {
      buf += d.toString();
      if (buf.includes("\n")) {
        clearTimeout(timer); sock.destroy();
        try { resolve(JSON.parse(buf.trim())); } catch (e) { reject(e); }
      }
    });
    sock.on("error", (e) => { clearTimeout(timer); reject(e); });
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function ueCommand(type, params, timeout = 30000) {
  for (let i = 1; i <= 3; i++) {
    try {
      const res = await ueCommandOnce(type, params, timeout);
      await sleep(200);
      return res;
    } catch (e) {
      if (i < 3 && (e.message === "Timeout" || e.code === "ECONNREFUSED")) {
        console.log(`    (retry ${i}/3 after ${e.message}, waiting...)`);
        await sleep(3000 * i);
      } else throw e;
    }
  }
}

async function captureAsset(id, assetPath, cat) {
  const outFile = path.join(THUMB_DIR, `${id}.png`);
  if (fs.existsSync(outFile)) {
    console.log(`  SKIP ${id} (already exists)`);
    return true;
  }

  try {
    const bpPath = assetPath.replace(/\.[^/]+$/, "");
    const logs = (await ueCommand("execute_python_script", { script: `
import unreal
import math

subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# Delete ONLY the previous preview actor
for a in subsys.get_all_level_actors():
    if a.get_actor_label().startswith("ThumbActor_"):
        subsys.destroy_actor(a)

# Spawn the asset
bp_class = unreal.EditorAssetLibrary.load_blueprint_class("${bpPath}")
if bp_class is None:
    bp_class = unreal.EditorAssetLibrary.load_blueprint_class("${assetPath}")
if bp_class is None:
    print("SPAWN_FAIL")
else:
    actor = subsys.spawn_actor_from_class(bp_class, unreal.Vector(0, 0, 0))
    if actor is None:
        print("SPAWN_FAIL")
    else:
        actor.set_actor_label("ThumbActor_Preview")

        # Fix broken mesh references (CC0 Kenney replacement meshes)
        MESH_FIX = {
            "building_01": "/Game/CityDatabase/meshes/SM_building_01",
            "building_02": "/Game/CityDatabase/meshes/SM_Building_02",
            "SM_building_03": "/Game/CityDatabase/meshes/SM_building_03",
            "building_04": "/Game/CityDatabase/meshes/SM_building_04",
            "Building_05": "/Game/CityDatabase/meshes/SM_Building_05",
            "Building_06": "/Game/CityDatabase/meshes/SM_Building_06",
            "SM_EuropeanHornbeam_Field_01": "/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Field_01",
            "SM_EuropeanHornbeam_Field_03_PP": "/Game/EuropeanHornbeam/Geometry/PivotPainter/SM_EuropeanHornbeam_Field_03_PP",
            "SM_EuropeanHornbeam_Forest_01": "/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Forest_01",
            "SM_EuropeanHornbeam_Forest_07": "/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Forest_07",
            "SM_EuropeanHornbeam_Field_02": "/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Field_02",
            "SM_EuropeanHornbeam_Field_04": "/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Field_04",
            "SM_Scooter_01_Base": "/Game/Scooters/Assets/Scooter_01/Static_Mesh/SM_Scooter_01_Base",
            "SM_Scooter_02_Base": "/Game/Scooters/Assets/Scooter_02/Static_Mesh/SM_Scooter_02_Base",
            "SM_Scooter_03_Base": "/Game/Scooters/Assets/Scooter_03/Static_Mesh/SM_Scooter_03_Base",
            "SM_Scooter_04_Base": "/Game/Scooters/Assets/Scooter_04/Static_Mesh/SM_Scooter_04_Base",
            "SM_Industrial_Carts_Service_Carts_3": "/Game/Industrial_Carts/Meshes/SM_Industrial_Carts_Service_Carts",
            "SM_Industrial_Carts_Static_Carts_2": "/Game/Industrial_Carts/Meshes/SM_Industrial_Carts_Static_Carts",
            "SM_table_a": "/Game/CityDatabase/meshes/SM_table_a",
            "SM_chair_b": "/Game/CityDatabase/meshes/SM_chair_b",
            "SM_chair_b1": "/Game/CityDatabase/meshes/SM_chair_b",
            "SM_SeatTable_01a": "/Game/CityDatabase/meshes/SM_TrafficLight1",
            "SM_CampingTable_01a": "/Game/CityDatabase/meshes/SM_TrafficLight1",
            "SM_hydrant_main": "/Game/CityDatabase/meshes/SM_hydrant_main",
            "SM_trash_bin_a": "/Game/CityDatabase/meshes/SM_trash_bin_a",
            "SM_trash_bin_b": "/Game/CityDatabase/meshes/SM_trash_bin_b",
            "SM_TrashCan_01": "/Game/GasStation/Models/SM_TrashCan_01",
            "SM_road_blocker_b": "/Game/CityDatabase/meshes/SM_road_blocker_b",
            "SM_road_cone": "/Game/CityDatabase/meshes/SM_road_cone",
            "Couch1": "/Game/CityDatabase/meshes/Couch1",
            "roadlines": "/Game/CityDatabase/meshes/roadlines",
            "sidewalks": "/Game/CityDatabase/meshes/sidewalks",
            "street_lights": "/Game/CityDatabase/meshes/street_lights",
        }
        # Scooter sub-parts to clear (replaced with combined car mesh)
        CLEAR_COMPS = {"SM_Scooter_01_WheelB","SM_Scooter_01_Join","SM_Scooter_01_Handlebar","SM_Scooter_01_WheelF","SM_Scooter_01_Leg","SM_Scooter_02_wheel_Base","SM_Scooter_02_Lock","SM_Scooter_02_Wheel_F","SM_Scooter_02_Handlebar","SM_Scooter_02_Leg","SM_Scooter_02_Wheel_B","SM_Scooter_03_Holder_02","SM_Scooter_03_Leg","SM_Scooter_03_wheel_B","SM_Scooter_03_Wheel_Base","SM_Scooter_03_Holder_03","SM_Scooter_03_Holder_01","SM_Scooter_03_Handlebar","SM_Scooter_03_wheel_F","SM_Scooter_04_wheel_B","SM_Scooter_04_Handlebar","SM_Scooter_04_wheel_F"}
        eal = unreal.EditorAssetLibrary
        for comp in actor.get_components_by_class(unreal.StaticMeshComponent):
            cn = comp.get_name()
            if cn in CLEAR_COMPS:
                comp.set_static_mesh(None)
            elif cn in MESH_FIX:
                m = eal.load_asset(MESH_FIX[cn])
                if m:
                    comp.set_static_mesh(m)

        # Get bounds for camera positioning
        (origin, extent) = actor.get_actor_bounds(False)
        cx, cy, cz = origin.x, origin.y, origin.z
        ex, ey, ez = extent.x, extent.y, extent.z
        radius = math.sqrt(ex*ex + ey*ey + ez*ez)
        cat = "${cat}"
        if cat == "buildings":
            mult, min_dist = 2.0, 800
        elif cat == "trees":
            mult, min_dist = 1.5, 400
        elif cat == "vehicles":
            mult, min_dist = 1.0, 200
        else:
            mult, min_dist = 1.0, 150
        dist = max(radius * mult, min_dist)

        # Position camera: front-right, elevated, looking at center
        elev = 0.3 if cat == "buildings" else 0.15
        cam_x = cx - dist * 0.7
        cam_y = cy - dist * 0.7
        cam_z = cz + dist * elev

        dx = cx - cam_x
        dy = cy - cam_y
        dz = (cz + ez * 0.2) - cam_z
        horiz = math.sqrt(dx*dx + dy*dy)
        pitch = math.degrees(math.atan2(dz, horiz))
        yaw = math.degrees(math.atan2(dy, dx))

        ed_subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        ed_subsys.set_level_viewport_camera_info(
            unreal.Vector(cam_x, cam_y, cam_z),
            unreal.Rotator(pitch=pitch, yaw=yaw, roll=0)
        )
        print(f"READY dist={dist}")
` }, 120000))?.result?.python_logs || [];

    if (logs.some(s => s.includes("SPAWN_FAIL"))) {
      console.log(`  FAIL ${id}: spawn failed`);
      return false;
    }
    if (!logs.some(s => s.includes("READY"))) {
      console.log(`  FAIL ${id}: setup failed (${JSON.stringify(logs)})`);
      return false;
    }

    // Wait for shaders to compile and textures to stream in
    await sleep(5000);

    const res = await ueCommand("take_screenshot", { filepath: outFile }, 120000);
    if (res.status !== "success") {
      console.log(`  FAIL ${id}: screenshot failed`);
      return false;
    }
    console.log(`  OK   ${id}`);
    return true;
  } catch (e) {
    console.log(`  ERR  ${id}: ${e.message}`);
    return false;
  }
}

async function waitForUE(timeout = 120) {
  for (let t = 0; t < timeout; t += 5) {
    try {
      await ueCommandOnce("execute_python_script", { script: 'print("PING")' }, 5000);
      return true;
    } catch {
      process.stdout.write(`  Waiting for UE (${t + 5}s)...\r`);
      await sleep(5000);
    }
  }
  return false;
}

async function main() {
  console.log("Asset Thumbnail Generator");
  console.log(`Output: ${THUMB_DIR}`);
  console.log();

  // Step 1: Clear ALL actors from the level for a clean slate
  console.log("Clearing all actors from level...");
  try {
    await ueCommand("execute_python_script", { script: `
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
count = 0
for a in subsys.get_all_level_actors():
    label = a.get_actor_label()
    # Skip essential editor actors
    if "WorldSettings" in label or "GameMode" in label:
        continue
    subsys.destroy_actor(a)
    count += 1
print(f"Cleared {count} actors")
` });
    await sleep(500);
  } catch (e) {
    console.error("Failed to clear level:", e.message);
  }

  // Step 2: Set up minimal clean lighting (no atmosphere, no ground)
  console.log("Setting up clean thumbnail lighting...");
  try {
    // Sky atmosphere for natural lighting (needed for tree/vegetation materials)
    await ueCommand("execute_python_script", { script: `
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
atmo = subsys.spawn_actor_from_class(unreal.SkyAtmosphere.static_class(), unreal.Vector(0,0,0))
atmo.set_actor_label("Thumb_Atmosphere")
print("Atmosphere OK")
` });
    await sleep(300);

    // Sun light for key illumination
    await ueCommand("execute_python_script", { script: `
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sun = subsys.spawn_actor_from_class(unreal.DirectionalLight.static_class(), unreal.Vector(0, 0, 500))
sun.set_actor_label("Thumb_Sun")
sun.set_actor_rotation(unreal.Rotator(pitch=-30.0, yaw=30.0, roll=0.0), False)
comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
comp.set_intensity(8.0)
comp.set_atmosphere_sun_light(True)
print("Sun OK")
` });
    await sleep(300);

    // Sky light for ambient fill
    await ueCommand("execute_python_script", { script: `
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sky = subsys.spawn_actor_from_class(unreal.SkyLight.static_class(), unreal.Vector(0, 0, 500))
sky.set_actor_label("Thumb_SkyLight")
sc = sky.get_component_by_class(unreal.SkyLightComponent)
sc.set_editor_property("intensity", 4.0)
sc.recapture_sky()
print("SkyLight OK")
` });
    await sleep(300);

    // Light gray ground plane for clean background
    await ueCommand("execute_python_script", { script: `
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ground = subsys.spawn_actor_from_class(unreal.StaticMeshActor.static_class(), unreal.Vector(0, 0, -5))
ground.set_actor_label("Thumb_Ground")
ground.set_actor_scale3d(unreal.Vector(5000, 5000, 1))
mc = ground.get_component_by_class(unreal.StaticMeshComponent)
mc.set_static_mesh(unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Plane"))
mat = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/BasicShapeMaterial")
if mat:
    mc.set_material(0, mat)
print("Ground OK")
` });
    await sleep(300);

    // View settings
    await ueCommand("execute_python_script", { script: `
import unreal
cmd = unreal.SystemLibrary.execute_console_command
cmd(None, "r.ViewDistanceScale 100")
cmd(None, "r.ForceLOD 0")
print("ViewSettings OK")
` });
    await sleep(300);

    console.log("Lighting setup complete.");
  } catch (e) {
    console.error("Failed to setup lighting:", e.message);
    process.exit(1);
  }

  // Step 3: Build asset list
  const assets = [];
  const categories = category ? [category] : Object.keys(ASSETS);
  for (const cat of categories) {
    const data = ASSETS[cat];
    if (cat === "buildings" && data.ids) {
      for (const id of data.ids) {
        const name = `BP_Building_${String(id).padStart(2, "0")}`;
        assets.push({ id: name, path: `/Game/CityDatabase/blueprints/${name}.${name}_C`, category: cat });
      }
    } else if (data.items) {
      for (const item of data.items) {
        const parts = item.split("/");
        const name = parts[parts.length - 1].split(".")[0];
        assets.push({ id: name, path: item, category: cat });
      }
    }
  }

  const batch = assets.slice(startIdx, endIdx);
  console.log(`\nCapturing ${batch.length} assets (of ${assets.length} total)...\n`);

  let captured = 0, failed = 0, skipped = 0, consecutive = 0;
  for (let i = 0; i < batch.length; i++) {
    const asset = batch[i];
    process.stdout.write(`[${i + 1}/${batch.length}] `);
    const existed = fs.existsSync(path.join(THUMB_DIR, `${asset.id}.png`));
    const ok = await captureAsset(asset.id, asset.path, asset.category);

    if (existed) { skipped++; consecutive = 0; }
    else if (ok) { captured++; consecutive = 0; }
    else {
      failed++;
      consecutive++;
      if (consecutive >= 2) {
        console.log(`\n  UE appears down. Waiting for recovery...`);
        if (!await waitForUE(120)) {
          console.log("  UE did not recover after 120s. Stopping.");
          break;
        }
        console.log("  UE is back! Continuing...");
        consecutive = 0;
      }
    }
    await sleep(3000);
  }

  console.log(`\nDone: ${captured} captured, ${skipped} skipped, ${failed} failed`);

  // Cleanup
  await ueCommand("execute_python_script", { script: `
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for a in subsys.get_all_level_actors():
    label = a.get_actor_label()
    if label.startswith("Thumb_") or label.startswith("ThumbActor_"):
        subsys.destroy_actor(a)
print("Cleanup done")
` });
}

main().catch(e => { console.error(e); process.exit(1); });
