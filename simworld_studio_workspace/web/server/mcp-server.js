"use strict";const net=require("net"),fs=require("fs"),path=require("path"),readline=require("readline"),UE_HOST=process.env.UNREAL_HOST||"127.0.0.1",UE_PORT=parseInt(process.env.UNREAL_PORT||"55559",10),SCREENSHOT_DIR=path.resolve(__dirname,"../../tmp/screens"),ASSETS=JSON.parse(fs.readFileSync(path.resolve(__dirname,process.env.SIMWORLD_ASSETS_FILE||"assets_full.json"),"utf-8"));fs.mkdirSync(SCREENSHOT_DIR,{recursive:!0});const spawnedActors=new Set,cmdQueue=[];let cmdRunning=!1;

// ---------------------------------------------------------------------------
// Command queue with adaptive cooldown + retry
// ---------------------------------------------------------------------------
// UE's MCP server: one TCP connection per command, accepts ΓåÆ reads ΓåÆ responds.
// After responding it needs time to re-enter its accept loop, especially after
// heavy commands (spawn, python_script) that block the game thread.
//
// Queue is serial: wait for UE response ΓåÆ cooldown ΓåÆ next command.
// Cooldown is adaptive: heavy commands get more breathing room.
// Retry with backoff on transient connection failures.
// ---------------------------------------------------------------------------
const COOLDOWN={
  spawn_blueprint_actor:200, // UE loads BP + instantiates ΓÇö needs time
  execute_python_script:200, // Python runs on game thread
  spawn_actor:200,
  delete_all_spawned:300,    // bulk delete is heavy
  setup_environment:300,
  delete_actor:100,
  _default:50,               // reads / queries are light
};
let _lastCmdEnd=0,_lastCooldown=50;

function ueCommand(e,t,s=3e4){return new Promise((n,o)=>{cmdQueue.push({type:e,params:t,timeoutMs:s,resolve:n,reject:o}),processQueue()})}

function processQueue(){
  if(cmdRunning||cmdQueue.length===0)return;
  // Wait for cooldown from previous command
  const elapsed=Date.now()-_lastCmdEnd;
  if(elapsed<_lastCooldown){
    setTimeout(processQueue,_lastCooldown-elapsed);
    return;
  }
  cmdRunning=!0;
  const cmd=cmdQueue.shift();
  _lastCooldown=COOLDOWN[cmd.type]||COOLDOWN._default;
  _execWithRetry(cmd.type,cmd.params,cmd.timeoutMs,3)
    .then(r=>{cmd.resolve(r)})
    .catch(e=>{cmd.reject(e)})
    .finally(()=>{_lastCmdEnd=Date.now();cmdRunning=!1;processQueue()});
}

function _execOnce(type,params,timeoutMs){
  return new Promise((resolve,reject)=>{
    const sock=new net.Socket();
    const timer=setTimeout(()=>{sock.destroy();reject(new Error(`UE command '${type}' timed out after ${timeoutMs}ms`))},timeoutMs);
    sock.connect(UE_PORT,UE_HOST,()=>{sock.write(JSON.stringify({type,params})+"\n")});
    let buf="";
    sock.on("data",d=>{buf+=d.toString();try{const p=JSON.parse(buf);clearTimeout(timer);sock.destroy();resolve(p)}catch{}});
    sock.on("error",e=>{clearTimeout(timer);reject(new Error(`UE connection error: ${e.message}`))});
    sock.on("close",()=>{clearTimeout(timer);if(buf.trim())try{resolve(JSON.parse(buf))}catch{reject(new Error("Incomplete response from UE"))}});
  });
}

async function _execWithRetry(type,params,timeoutMs,retries){
  let lastErr;
  for(let i=0;i<retries;i++){
    try{return await _execOnce(type,params,timeoutMs)}
    catch(e){
      lastErr=e;
      if(i<retries-1){
        const delay=300*(i+1); // 300ms, 600ms backoff
        process.stderr.write(`[mcp-server] Retry ${i+1}/${retries} for '${type}' after ${delay}ms: ${e.message}\n`);
        await new Promise(r=>setTimeout(r,delay));
      }
    }
  }
  throw lastErr;
}

// Ensure actor names are unique by appending a session-unique suffix.
// _SID = millisecond timestamp (base36) + 3 random chars ΓåÆ e.g. "lhq3k2_a7f"
// This survives: server restarts, map reloads, PIE restarts, pre-existing map actors.
const _usedNames=new Set();
const _SID=(Date.now().toString(36).slice(-5)+Math.random().toString(36).slice(2,5));
function _uniqueName(name){
  // Strip any previous suffix (in case Claude reuses names across turns)
  const base=name.replace(/_[a-z0-9]{6,}$/i,'');
  const tagged=`${base}_${_SID}`;
  if(!_usedNames.has(tagged)){_usedNames.add(tagged);return tagged}
  // Within-session duplicate: append incrementing counter
  let i=2;
  while(_usedNames.has(`${tagged}${i}`))i++;
  const unique=`${tagged}${i}`;
  _usedNames.add(unique);
  return unique;
}

async function toolSpawnBlueprintActor({actor_name:e,blueprint_id:t,location:s,rotation:n,scale:o}){e=_uniqueName(e);let r=t;if(!r.startsWith("/Game/")){let a=!1;for(const i of["trees","vehicles","street_furniture","roads"]){const l=(ASSETS[i]?.items||[]).find(_=>{const u=_.split("/").pop().split(".")[0];return u===r||u.toLowerCase()===r.toLowerCase()});if(l){r=l,a=!0;break}}if(!a){const i=parseInt(r.replace(/\D/g,""),10);if((/^(BP_Building_)?\d+$/.test(r)||/^Building_\d+$/.test(r))&&!isNaN(i)&&ASSETS.buildings.ids.includes(i)){const l=String(i).padStart(2,"0");r=`/Game/CityDatabase/blueprints/BP_Building_${l}.BP_Building_${l}_C`}else if((!isNaN(i)&&(/^(BP_Building_)?\d+$/.test(r)||/^Building_\d+$/.test(r)))){return{status:"error",message:`Building ${i} is not available. Only buildings 01-06 are included in this package. Use BP_Building_01 through BP_Building_06.`}}else{for(const l of["trees","vehicles","street_furniture","roads"]){const u=(ASSETS[l]?.items||[]).find(m=>m.toLowerCase().includes(r.toLowerCase()));if(u){r=u,a=!0;break}}a||(r=`/Game/CityDatabase/blueprints/${r}.${r}_C`)}}}if(r.startsWith("/Game/")&&!r.endsWith("_C")){const a=r.split(".");if(a.length===2)r=`${a[0]}.${a[1]}_C`;else{const i=r.split("/").pop();r=`${r}.${i}_C`}}const loc=s||[0,0,0],GROUND_HALF=9500;loc[0]=Math.max(-GROUND_HALF,Math.min(GROUND_HALF,loc[0]));loc[1]=Math.max(-GROUND_HALF,Math.min(GROUND_HALF,loc[1]));if(loc[2]<0)loc[2]=0;const c=await ueCommand("spawn_blueprint_actor",{actor_name:e,blueprint_name:r,location:loc,rotation:n||[0,0,0]});if(c.status==="success"){spawnedActors.add(e);const ueActorName=c.result?.name||e;try{await ueCommand("execute_python_script",{script:`
import unreal
eal = unreal.EditorAssetLibrary
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
MESH_FIX = {"building_01":"/Game/CityDatabase/meshes/SM_building_01","building_02":"/Game/CityDatabase/meshes/SM_Building_02","SM_building_03":"/Game/CityDatabase/meshes/SM_building_03","building_04":"/Game/CityDatabase/meshes/SM_building_04","Building_05":"/Game/CityDatabase/meshes/SM_Building_05","Building_06":"/Game/CityDatabase/meshes/SM_Building_06","SM_EuropeanHornbeam_Field_01":"/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Field_01","SM_EuropeanHornbeam_Field_03_PP":"/Game/EuropeanHornbeam/Geometry/PivotPainter/SM_EuropeanHornbeam_Field_03_PP","SM_EuropeanHornbeam_Forest_01":"/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Forest_01","SM_EuropeanHornbeam_Forest_07":"/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Forest_07","SM_EuropeanHornbeam_Field_02":"/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Field_02","SM_EuropeanHornbeam_Field_04":"/Game/EuropeanHornbeam/Geometry/SimpleWind/SM_EuropeanHornbeam_Field_04","SM_Scooter_01_Base":"/Game/Scooters/Assets/Scooter_01/Static_Mesh/SM_Scooter_01_Base","SM_Scooter_02_Base":"/Game/Scooters/Assets/Scooter_02/Static_Mesh/SM_Scooter_02_Base","SM_Scooter_03_Base":"/Game/Scooters/Assets/Scooter_03/Static_Mesh/SM_Scooter_03_Base","SM_Scooter_04_Base":"/Game/Scooters/Assets/Scooter_04/Static_Mesh/SM_Scooter_04_Base","SM_Industrial_Carts_Service_Carts_3":"/Game/Industrial_Carts/Meshes/SM_Industrial_Carts_Service_Carts","SM_Industrial_Carts_Static_Carts_2":"/Game/Industrial_Carts/Meshes/SM_Industrial_Carts_Static_Carts","SM_table_a":"/Game/CityDatabase/meshes/SM_table_a","SM_chair_b":"/Game/CityDatabase/meshes/SM_chair_b","SM_chair_b1":"/Game/CityDatabase/meshes/SM_chair_b","SM_SeatTable_01a":"/Game/CityDatabase/meshes/SM_TrafficLight1","SM_CampingTable_01a":"/Game/CityDatabase/meshes/SM_TrafficLight1","SM_hydrant_main":"/Game/CityDatabase/meshes/SM_hydrant_main","SM_trash_bin_a":"/Game/CityDatabase/meshes/SM_trash_bin_a","SM_trash_bin_b":"/Game/CityDatabase/meshes/SM_trash_bin_b","SM_TrashCan_01":"/Game/GasStation/Models/SM_TrashCan_01","SM_road_blocker_b":"/Game/CityDatabase/meshes/SM_road_blocker_b","SM_road_cone":"/Game/CityDatabase/meshes/SM_road_cone","Couch1":"/Game/CityDatabase/meshes/Couch1","roadlines":"/Game/CityDatabase/meshes/roadlines","sidewalks":"/Game/CityDatabase/meshes/sidewalks","street_lights":"/Game/CityDatabase/meshes/street_lights"}
CLEAR = {"SM_Scooter_01_WheelB","SM_Scooter_01_Join","SM_Scooter_01_Handlebar","SM_Scooter_01_WheelF","SM_Scooter_01_Leg","SM_Scooter_02_wheel_Base","SM_Scooter_02_Lock","SM_Scooter_02_Wheel_F","SM_Scooter_02_Handlebar","SM_Scooter_02_Leg","SM_Scooter_02_Wheel_B","SM_Scooter_03_Holder_02","SM_Scooter_03_Leg","SM_Scooter_03_wheel_B","SM_Scooter_03_Wheel_Base","SM_Scooter_03_Holder_03","SM_Scooter_03_Holder_01","SM_Scooter_03_Handlebar","SM_Scooter_03_wheel_F","SM_Scooter_04_wheel_B","SM_Scooter_04_Handlebar","SM_Scooter_04_wheel_F"}
actor = None
ue_name = "${ueActorName}"
for a in subsys.get_all_level_actors():
    if a.get_name() == ue_name:
        actor = a
        break
if actor:
    for comp in actor.get_components_by_class(unreal.StaticMeshComponent):
        cn = comp.get_name()
        if cn in CLEAR:
            comp.set_static_mesh(None)
        elif cn in MESH_FIX:
            m = eal.load_asset(MESH_FIX[cn])
            if m:
                comp.set_static_mesh(m)
    print("MESH_FIX_OK")
`},15000)}catch(fixErr){}if(o&&(o[0]!==1||o[1]!==1||o[2]!==1))await ueCommand("set_actor_transform",{name:e,scale:o})}return c}async function toolSpawnActor({name:e,static_mesh:t,location:s,rotation:n,scale:o}){e=_uniqueName(e);const loc2=s||[0,0,0],GH2=9500;loc2[0]=Math.max(-GH2,Math.min(GH2,loc2[0]));loc2[1]=Math.max(-GH2,Math.min(GH2,loc2[1]));try{await ueCommand("execute_python_script",{script:`import unreal\nsubsys=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\nfor a in subsys.get_all_level_actors():\n    if a.get_name()=="${e}" or a.get_actor_label()=="${e}":\n        subsys.destroy_actor(a)\n        break\nunreal.SystemLibrary.collect_garbage()\nprint("pre_clear_done")`},5000)}catch(_){}const r=await ueCommand("spawn_actor",{name:e,type:"StaticMeshActor",location:loc2,rotation:n||[0,0,0],scale:o||[1,1,1],static_mesh:t});if(r.status==="success"&&t){const ue_name=r.result?.name||e;const sc=o||[1,1,1];const mesh_path=t;try{await ueCommand("execute_python_script",{script:`import unreal\nsubsys=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\nmesh=unreal.load_asset("${mesh_path}")\nactor=None\nfor a in subsys.get_all_level_actors():\n    if a.get_name()=="${ue_name}" or a.get_actor_label()=="${e}":\n        actor=a\n        break\nif actor and mesh:\n    comp=actor.get_component_by_class(unreal.StaticMeshComponent)\n    if comp: comp.set_static_mesh(mesh)\n    actor.set_actor_location(unreal.Vector(${loc2[0]},${loc2[1]},${loc2[2]}),False,False)\n    actor.set_actor_rotation(unreal.Rotator(${(n||[0,0,0])[0]},${(n||[0,0,0])[1]},${(n||[0,0,0])[2]}),False)\n    actor.set_actor_scale3d(unreal.Vector(${sc[0]},${sc[1]},${sc[2]}))\n    print("mesh_set_ok")\nelse:\n    print("mesh_set_failed: actor="+str(actor)+" mesh="+str(mesh))`},15000)}catch(fixErr){}}return r.status==="success"&&spawnedActors.add(e),r}async function toolDeleteActor({name:e}){const t=await ueCommand("delete_actor",{name:e});if(t.status==="success")return spawnedActors.delete(e),t;const s=await ueCommand("execute_python_script",{script:`
import unreal
deleted = False
for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors():
    if a.get_actor_label() == "${e.replace(/"/g,'\\"')}":
        unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actor(a)
        deleted = True
        break
print("DELETED" if deleted else "NOT_FOUND")
`});spawnedActors.delete(e);const o=(s?.result?.python_logs||[]).some(r=>r.includes("DELETED"));return{status:o?"success":"error",message:o?`Deleted ${e}`:`Actor not found: ${e}`}}async function toolDeleteAllSpawned(){const e=[...spawnedActors],t=await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# Default level actors we should NOT delete
keep_prefixes = ("Floor", "SM_", "SkySphere", "Light", "Atmo", "Fog", "Sky",
                 "Camera", "Player", "Default", "GameMode", "WorldSettings",
                 "Brush", "Volume", "Note", "AbstractNav", "NavMesh")
keep_classes = ("WorldSettings", "GameModeBase", "PlayerStart", "Brush",
                "AbstractNavData", "NavigationData", "NavMeshBoundsVolume",
                "LevelScriptActor", "WorldDataLayers", "ExternalDataLayerAsset")

deleted = []
kept = []
for a in subsys.get_all_level_actors():
    label = a.get_actor_label()
    cls = a.get_class().get_name()
    # Always delete Arena_Env_ prefixed (our environment actors)
    if label.startswith("Arena_Env_"):
        subsys.destroy_actor(a)
        deleted.append(label)
        continue
    # Skip engine/level infrastructure
    if cls in keep_classes:
        kept.append(label)
        continue
    if any(label.startswith(p) for p in keep_prefixes):
        kept.append(label)
        continue
    # Delete everything else (user-spawned actors)
    subsys.destroy_actor(a)
    deleted.append(label)

print(f"Deleted {len(deleted)} actors: {deleted}")
print(f"Kept {len(kept)} infrastructure actors")
`});spawnedActors.clear();_usedNames.clear();const s=t?.result?.python_logs||[];return{result:t?.result,logs:s}}async function toolGetActors(){return ueCommand("get_actors_in_level",{})}async function toolFindActors({pattern:e}){return ueCommand("find_actors_by_name",{pattern:e})}async function toolSetActorTransform({name:e,location:t,rotation:s,scale:n}){const o={name:e};return t&&(o.location=t),s&&(o.rotation=s),n&&(o.scale=n),ueCommand("set_actor_transform",o)}async function toolTakeScreenshot({filename:e}){const t=e||`screenshot_${Date.now()}.png`,s=path.join(SCREENSHOT_DIR,t);return ueCommand("take_screenshot",{filepath:s})}async function toolSetCamera({location:e,rotation:t}){if(e&&t){const o=`
import unreal
subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
loc = unreal.Vector(${e[0]}, ${e[1]}, ${e[2]})
rot = unreal.Rotator(pitch=${t[0]}, yaw=${t[1]}, roll=0.0)
subsys.set_level_viewport_camera_info(loc, rot)
`;return ueCommand("execute_python_script",{script:o})}const n=`
import unreal, math

subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
world = unreal.EditorLevelLibrary.get_editor_world()
all_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)

min_x, min_y, min_z = float('inf'), float('inf'), float('inf')
max_x, max_y, max_z = float('-inf'), float('-inf'), float('-inf')
count = 0

for actor in all_actors:
    name = actor.get_name()
    if any(skip in name.lower() for skip in ['sky', 'light', 'fog', 'atmosphere', 'volume', 'brush', 'worldsettings', 'ground']):
        continue
    origin, extent = actor.get_actor_bounds(False)
    if extent.x < 1 and extent.y < 1 and extent.z < 1:
        continue
    loc = actor.get_actor_location()
    min_x = min(min_x, loc.x - extent.x)
    max_x = max(max_x, loc.x + extent.x)
    min_y = min(min_y, loc.y - extent.y)
    max_y = max(max_y, loc.y + extent.y)
    min_z = min(min_z, loc.z - extent.z)
    max_z = max(max_z, loc.z + extent.z)
    count += 1

loc_override = ${e?`unreal.Vector(${e[0]}, ${e[1]}, ${e[2]})`:"None"}

if count == 0:
    cam_loc = loc_override if loc_override else unreal.Vector(0, 0, 5000)
    cam_rot = unreal.Rotator(pitch=-90, yaw=0, roll=0)
else:
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0
    sx = max_x - min_x
    sy = max_y - min_y
    sz = max_z - min_z
    span = max(sx, sy, sz)

    if loc_override:
        cam_loc = loc_override
    else:
        dist = span * 1.2
        height = cz + dist * 0.7
        offset = dist * 0.7
        cam_loc = unreal.Vector(cx - offset, cy - offset, height)

    dx = cx - cam_loc.x
    dy = cy - cam_loc.y
    dz = cz - cam_loc.z
    horiz = math.sqrt(dx*dx + dy*dy)
    pitch = math.degrees(math.atan2(dz, horiz)) if horiz > 0.01 else -90.0
    yaw = math.degrees(math.atan2(dy, dx))
    cam_rot = unreal.Rotator(pitch=pitch, yaw=yaw, roll=0.0)

subsys.set_level_viewport_camera_info(cam_loc, cam_rot)
print(f"Camera: loc=({cam_loc.x:.0f},{cam_loc.y:.0f},{cam_loc.z:.0f}) rot=({cam_rot.pitch:.1f},{cam_rot.yaw:.1f},0.0) scene_actors={count}")
`;return ueCommand("execute_python_script",{script:n})}async function toolExecutePython({script:e}){return ueCommand("execute_python_script",{script:e})}function toolListAssets({category:e}){if(e&&ASSETS[e])return{category:e,assets:ASSETS[e]};const t={};for(const[s,n]of Object.entries(ASSETS))n.items?t[s]={count:n.items.length,description:n.description,items:n.items}:n.ids?t[s]={count:n.ids.length,description:n.description,example:n.example,notes:n.notes}:t[s]={description:n.description};return t}async function toolSetupEnvironment({ground_size:e,time_of_day:t}){const s=e||200,n=t||"afternoon",o={morning:{pitch:-25,yaw:-120},noon:{pitch:-75,yaw:-30},afternoon:{pitch:-45,yaw:30},sunset:{pitch:-10,yaw:60},night:{pitch:10,yaw:0}},r=o[n]||o.afternoon,c=await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# Remove old environment actors
for a in subsys.get_all_level_actors():
    if a.get_actor_label().startswith("Arena_Env_"):
        subsys.destroy_actor(a)

# Sky Atmosphere (MUST exist for sky to render)
atmo = subsys.spawn_actor_from_class(unreal.SkyAtmosphere.static_class(), unreal.Vector(0, 0, 0))
atmo.set_actor_label("Arena_Env_Atmosphere")
print("SkyAtmosphere OK")
`}),a=await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sun = subsys.spawn_actor_from_class(unreal.DirectionalLight.static_class(), unreal.Vector(0, 0, 500))
sun.set_actor_label("Arena_Env_Sun")
sun.set_actor_rotation(unreal.Rotator(pitch=${r.pitch}.0, yaw=${r.yaw}.0, roll=0.0), False)
comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
comp.set_intensity(10.0)
comp.set_atmosphere_sun_light(True)
rot = sun.get_actor_rotation()
print(f"Sun OK: pitch={rot.pitch:.1f} yaw={rot.yaw:.1f} atmo_sun={comp.atmosphere_sun_light}")
`}),i=await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
sky = subsys.spawn_actor_from_class(unreal.SkyLight.static_class(), unreal.Vector(0, 0, 500))
sky.set_actor_label("Arena_Env_SkyLight")
sc = sky.get_component_by_class(unreal.SkyLightComponent)
sc.set_editor_property("intensity", 3.0)
print(f"SkyLight OK: intensity={sc.intensity}")
`}),p=await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

fog = subsys.spawn_actor_from_class(unreal.ExponentialHeightFog.static_class(), unreal.Vector(0, 0, 0))
fog.set_actor_label("Arena_Env_Fog")
fc = fog.get_component_by_class(unreal.ExponentialHeightFogComponent)
fc.set_editor_property("fog_density", 0.002)
fc.set_editor_property("fog_max_opacity", 0.6)
print("Fog OK")

# Disable aggressive view distance culling
cmds = [
    "r.ViewDistanceScale 100",
    "r.StaticMeshLODDistanceScale 0.01",
    "r.ForceLOD 0",
    "foliage.LODDistanceScale 100",
]
for cmd in cmds:
    unreal.SystemLibrary.execute_console_command(None, cmd)
print("View distance culling disabled")
`}),l=await ueCommand("execute_python_script",{script:`
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

# Spawn ground plane
ground = subsys.spawn_actor_from_class(unreal.StaticMeshActor.static_class(), unreal.Vector(0, 0, -10))
ground.set_actor_label("Arena_Env_Ground")
ground.set_actor_scale3d(unreal.Vector(${s}, ${s}, 1))
mc = ground.get_component_by_class(unreal.StaticMeshComponent)
mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Plane.Plane")
if mc and mesh:
    mc.set_static_mesh(mesh)
print(f"Ground plane spawned at scale ${s}")

# Disable distance culling on all spawned actors
for a in subsys.get_all_level_actors():
    cls = a.get_class().get_name()
    label = a.get_actor_label()
    if "BP_" in cls or label.startswith("Arena_Env_"):
        comps = a.get_components_by_class(unreal.PrimitiveComponent)
        for c in comps:
            try:
                c.set_editor_property("ld_max_draw_distance", 0)
                c.set_editor_property("cached_max_draw_distance", 0)
            except:
                pass
print("Culling disabled on all spawned actors")
`});return spawnedActors.add("Arena_Env_Ground"),{status:"success",message:`Environment set up: sun (${n}), sky atmosphere, sky light, fog, ground (${s*100}m x ${s*100}m), view distance culling disabled`,steps:{atmosphere:c?.result?.python_logs,sun:a?.result?.python_logs,skylight:i?.result?.python_logs,fog:p?.result?.python_logs},ground:l?.result?.python_logs}}function _notifyBackend(body){try{const http=require('http');const data=JSON.stringify(body);const req=http.request({host:'127.0.0.1',port:parseInt(process.env.PORT||'3002'),path:'/api/verifier-update',method:'POST',headers:{'Content-Type':'application/json','Content-Length':Buffer.byteLength(data)}});req.on('error',()=>{});req.write(data);req.end();}catch(e){}}async function toolVerifyScene({original_request:R,focus_areas:F}){const ts='verify_'+Date.now()+'.png',sp=path.join(SCREENSHOT_DIR,ts);try{await ueCommand('take_screenshot',{filepath:sp})}catch(e){return{status:'error',message:'Screenshot failed: '+e.message}}let ar;try{ar=await toolGetActors()}catch(e){ar={status:'error'}}// Notify backend: screenshot ready (show panel immediately)
_notifyBackend({type:'screenshot',data:sp});const actorsList=JSON.stringify(ar,null,2);const uc=[];try{if(fs.existsSync(sp)){const imgData=fs.readFileSync(sp);const isJpeg=imgData[0]===255&&imgData[1]===216;const mediaType=isJpeg?'image/jpeg':'image/png';uc.push({type:'image',source:{type:'base64',media_type:mediaType,data:imgData.toString('base64')}})}}catch(e){}const promptText='Please verify this 3D scene in SimWorld Studio (Unreal Engine 5).\n\n'+(R?'Original scene request: "'+R+'"\n\n':'')+'Current actors in the scene:\n'+actorsList+(F?'\n\nFocus on: '+F:'');uc.push({type:'text',text:promptText});const sysPrompt='You are a 3D scene verification expert for SimWorld Studio (Unreal Engine 5).\nAnalyze the scene screenshot and actor list, then provide concise actionable feedback.\n\nEvaluate:\n1. Completeness: Are all requested objects present?\n2. Placement: Are objects in good positions? (X/Y within -9500 to 9500, not overlapping, not outside ground)\n3. Scale: Do objects look appropriately sized relative to each other?\n4. Realism: Does the scene match the original request?\n5. Issues: Any obvious problems (floating objects above ground, buried below ground, misaligned)?\n6. Navigation/walkability (IMPORTANT): Large buildings (BP_Building_*) have a large footprint and BLOCK agent navigation if placed in the walkable area near PlayerStart. They should be placed as background scenery far from center (>2500 UU from PlayerStart). Small props (hydrants, bins, cones, benches, trash) are fine anywhere. Trees belong at the scene edge. If you see large buildings close to the center/PlayerStart, flag NEEDS_IMPROVEMENT and suggest moving them to a background ring 2500-5000 UU away.\n\nFormat your response as:\n- **Status**: PASS / NEEDS_IMPROVEMENT / FAIL\n- **Issues**: (bullet list of specific problems, or "None" if PASS)\n- **Suggestions**: (bullet list of specific actionable improvements the agent should make)';const CLAUDE=process.env.CLAUDE_BIN||'claude';const args=['--input-format','stream-json','--output-format','stream-json','--verbose','--dangerously-skip-permissions','--append-system-prompt',sysPrompt];return new Promise((resolve)=>{const p=require('child_process').spawn(CLAUDE,args,{stdio:['pipe','pipe','pipe'],env:process.env});p.stdin.write(JSON.stringify({type:'user',message:{role:'user',content:uc}})+'\n');p.stdin.end();let buf='',feedback='';p.stdout.on('data',d=>{buf+=d.toString();const lines=buf.split('\n');buf=lines.pop()||'';for(const line of lines){if(!line.trim())continue;try{const ev=JSON.parse(line);if(ev.type==='result'&&typeof ev.result==='string'&&ev.result){feedback=ev.result}else if(ev.type==='assistant'){for(const b of(ev.message&&ev.message.content||[])){if(b.type==='text'&&b.text){feedback+=b.text;_notifyBackend({type:'delta',data:b.text})}}}else if(ev.type==='stream_event'){const evt=ev.event||{};if(evt.type==='content_block_delta'&&evt.delta&&evt.delta.type==='text_delta'&&evt.delta.text){feedback+=evt.delta.text;_notifyBackend({type:'delta',data:evt.delta.text})}}}catch{}}});p.stderr.on('data',d=>process.stderr.write('[verifier] '+d));p.on('close',()=>{resolve({status:'success',screenshot:sp,actors_count:(ar&&ar.result&&ar.result.actors&&ar.result.actors.length)||0,feedback:feedback||'No feedback generated'})})});}// ---------------------------------------------------------------------------
// UnrealCV access (Phase 2) ΓÇö UCV traffic now goes through the main server's
// singleton UcvBroker via HTTP RPC, instead of each mcp-server subprocess
// opening its own TCP socket to UE port 9000. This eliminates the cross-process
// race on UCV that was silently dropping commands when one agent's spawn reset
// the connection mid-flight for everyone else. The broker (in unreal-bridge.js)
// handles the persistent socket, FIFO queue, retries, and reconnect.
// ---------------------------------------------------------------------------
const BROKER_HOST=process.env.SIMWORLD_BROKER_HOST||"127.0.0.1";
const BROKER_PORT=process.env.PORT||"3002";
const BROKER_URL=`http://${BROKER_HOST}:${BROKER_PORT}/api/internal/ucv`;

const AGENT_REGISTRY=JSON.parse(fs.readFileSync(path.resolve(__dirname,"agent-registry.json"),"utf-8"));

function getAgentType(agentType){return AGENT_REGISTRY.agentTypes[agentType]||null}
function resolveAgentType(nameOrType){
  if(AGENT_REGISTRY.agentTypes[nameOrType])return nameOrType;
  for(const[t,def]of Object.entries(AGENT_REGISTRY.agentTypes)){
    if(def.namePatterns.some(p=>nameOrType.toLowerCase().includes(p)))return t;
  }
  return"pedestrian";
}

async function ucvCommand(cmd,timeout=10000,opts={}){
  const retries=typeof opts.retries==="number"?opts.retries:1;
  const queueDeadlineMs=typeof opts.queueDeadlineMs==="number"
    ?opts.queueDeadlineMs
    :Math.max(timeout*2,15000);
  let res;
  try{
    res=await fetch(BROKER_URL,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({cmd,timeoutMs:timeout,retries,queueDeadlineMs}),
    });
  }catch(err){
    throw new Error(`UCV broker unreachable at ${BROKER_URL}: ${err.message}`);
  }
  let json;
  try{json=await res.json()}catch{json={ok:false,error:`broker returned non-JSON (HTTP ${res.status})`}}
  if(!json.ok){
    throw new Error(`UCV ${cmd.slice(0,60)}: ${json.error||"unknown error"}`);
  }
  return json.result;
}


let pieStarted=false;

async function checkPIE(){
  // Check if PIE is running by trying a simple UnrealCV command
  try{const r=await ucvCommand("vget /objects",5000);return r&&r.length>0}
  catch(e){return false}
}

async function ensurePIE(){
  if(pieStarted)return;
  // Check if PIE is already running
  const running=await checkPIE();
  if(running){pieStarted=true;return}
  // Not running ΓÇö return error telling user to start PIE manually
  throw new Error("PIE mode is not active. Please start Play-In-Editor (PIE) mode in Unreal Engine first, then try again. You can start PIE by clicking the Play button in the UE toolbar.")
}

async function ucvCommandRetry(cmd,retries=3,delay=2000,timeout=10000){
  mcpLog('debug','ucv: '+cmd.slice(0,80));
  // The broker handles retries+reconnect internally now (Phase 2). We just
  // pass `retries` through. `delay` is unused ΓÇö broker uses its own backoff.
  return ucvCommand(cmd,timeout,{retries});
}

function mcpLog(level,msg,data){process.stderr.write(`[${new Date().toISOString()}] [${level}] [mcp] ${msg}${data?' '+JSON.stringify(data):''}\n`)}

async function toolSpawnAgent({agent_name,agent_type,location,rotation}){
  mcpLog('info','spawn_agent',{agent_name,agent_type,location});
  await ensurePIE();
  const type=resolveAgentType(agent_type||"pedestrian");
  const typeDef=getAgentType(type);
  if(!typeDef)return{status:"error",message:`Unknown agent type: ${agent_type}. Available: ${Object.keys(AGENT_REGISTRY.agentTypes).join(", ")}`};
  const bp=typeDef.blueprintPath;
  const loc=location||[0,0,typeDef.spawnZ||110];
  const rot=rotation||[0,0,0];
  try{
    // Spawn may cause UE to reset the socket ΓÇö retry after delay
    try{await ucvCommand(`vset /objects/spawn_bp_asset ${bp} ${agent_name}`,15000)}
    catch(e){/* spawn often resets connection, that's OK */}
    // Wait for UE to finish loading the character
    await new Promise(r=>setTimeout(r,3000));
    // Set position/rotation with retries (connection may need to re-establish)
    await ucvCommandRetry(`vset /object/${agent_name}/location ${loc[0]} ${loc[1]} ${loc[2]}`);
    await ucvCommandRetry(`vset /object/${agent_name}/rotation ${rot[0]} ${rot[1]} ${rot[2]}`);
    await ucvCommandRetry(`vset /object/${agent_name}/collision true`);
    await ucvCommandRetry(`vset /object/${agent_name}/object_mobility true`);
    spawnedActors.add(agent_name);
    return{status:"success",agent_name,agent_type:type,location:loc,rotation:rot,blueprint:bp,available_actions:Object.keys(typeDef.actions)}
  }catch(e){return{status:"error",message:e.message}}
}

async function toolAgentStop({agent_name,agent_type}){
  const type=resolveAgentType(agent_type||"humanoid");
  const typeDef=getAgentType(type);
  const cmd=typeDef?.stopCmd||"StopAgent";
  try{await ucvCommandRetry(`vbp ${agent_name} ${cmd}`);return{status:"success",action:"stop",agent:agent_name}}
  catch(e){return{status:"error",message:e.message}}
}

async function toolAgentRotate({agent_name,angle,direction,agent_type}){
  const type=resolveAgentType(agent_type||"humanoid");
  const typeDef=getAgentType(type);
  const rotCmd=typeDef?.rotateCmd||"TurnAround";
  const dir=direction==="right"?1:-1;
  const a=direction==="right"?angle:-angle;
  try{await ucvCommandRetry(`vbp ${agent_name} ${rotCmd} 1 ${a} ${dir}`);return{status:"success",action:"rotate",agent:agent_name,angle,direction}}
  catch(e){return{status:"error",message:e.message}}
}

async function toolAgentAction({agent_name,action,agent_type,params}){mcpLog('info','agent_action',{agent_name,action,agent_type});
  const type=resolveAgentType(agent_type||"humanoid");
  const typeDef=getAgentType(type);
  if(!typeDef)return{status:"error",message:`Unknown agent type: ${agent_type}`};
  const actionDef=typeDef.actions[action];
  if(!actionDef){
    const available=Object.keys(typeDef.actions).join(", ");
    return{status:"error",message:`Unknown action '${action}' for ${type}. Available: ${available}`}
  }
  // Build command: "vbp {name} {cmd} {param1} {param2} ..."
  let cmdStr=`vbp ${agent_name} ${actionDef.cmd}`;
  if(actionDef.params){
    const p=params||{};
    const defaults=actionDef.defaults||[];
    for(let i=0;i<actionDef.params.length;i++){
      const val=p[actionDef.params[i]]??defaults[i]??"";
      cmdStr+=` ${val}`;
    }
  }
  try{const resp=await ucvCommandRetry(cmdStr.trim());return{status:"success",action,agent:agent_name,response:resp}}
  catch(e){return{status:"error",message:e.message}}
}

async function toolGetAgentState({agent_name}){
  try{
    const loc=await ucvCommandRetry(`vget /object/${agent_name}/location`);
    const rot=await ucvCommandRetry(`vget /object/${agent_name}/rotation`);
    const locParts=loc.trim().split(/\s+/).map(Number);
    const rotParts=rot.trim().split(/\s+/).map(Number);
    return{status:"success",agent:agent_name,location:locParts,rotation:rotParts}
  }catch(e){return{status:"error",message:e.message}}
}

// Shared helper: build UE Python script for scene checks.
// Works in editor mode (no PIE). Uses actor labels for lookup.
// When names list is empty, falls back to scanning all non-infra actors.
// Floating: per-actor downward column search; fallback = Z=0 (no global ground_z).
// Collisions: AABB overlap with 5cm touch tolerance.
function _buildSceneCheckScript(names, floatThreshold, checkType) {
  return `
import unreal, json
world = unreal.EditorLevelLibrary.get_editor_world()
names_list     = ${JSON.stringify(names)}
FLOAT_THRESHOLD = ${floatThreshold}
TOUCH_TOL       = 5.0

all_actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)

INFRA_CLS = frozenset(['skyatmosphere','directionallight','skylightcomponent',
    'exponentialheightfog','worldsettings','defaultphysicsvolume',
    'navmeshboundingvolume','postprocessvolume','triggervolume','brushactor',
    'recastnavmesh','atmosphericfog','smartobjectsubsystem'])
INFRA_NM  = frozenset(['navmesh','recast','worldsettings','smartobject','subsystem',
    'gameplaydebugger','atmosphericfog','postprocess'])

INFRA_LABEL_PREFIX = frozenset(['sm_bg', 'bp_bg'])
def is_infra(actor):
    cls = actor.get_class().get_name().lower()
    if any(s in cls for s in INFRA_CLS): return True
    if any(s in actor.get_name().lower() for s in INFRA_NM): return True
    try:
        lbl = actor.get_actor_label().lower()
        return any(lbl.startswith(p) for p in INFRA_LABEL_PREFIX)
    except: return False

# spawnedActors stores the full session-suffixed labels — match by exact label
by_label = {}
for a in all_actors:
    if is_infra(a): continue
    try:
        lbl = a.get_actor_label()
        if lbl: by_label[lbl] = a
    except: pass

if names_list:
    resolved = {n: by_label[n] for n in names_list if n in by_label}
else:
    # Fallback: scan all non-infra actors with reasonable bounds
    resolved = {}
    for a in all_actors:
        if is_infra(a): continue
        origin, ext = a.get_actor_bounds(False)
        if ext.x < 1 and ext.y < 1 and ext.z < 1: continue
        if max(ext.x, ext.y, ext.z) > 50000: continue
        lbl = ''
        try: lbl = a.get_actor_label()
        except: pass
        resolved[lbl if lbl else a.get_name()] = a

scene = []
for key, actor in resolved.items():
    origin, ext = actor.get_actor_bounds(False)
    if ext.x < 1 and ext.y < 1 and ext.z < 1: continue
    loc = actor.get_actor_location()
    scene.append({'actor': actor, 'name': key,
        'ox': origin.x, 'oy': origin.y, 'oz': origin.z,
        'ex': max(ext.x,1), 'ey': max(ext.y,1), 'ez': max(ext.z,1),
        'bot_z': origin.z - ext.z, 'lx': loc.x, 'ly': loc.y})

all_bounds = []
for a in all_actors:
    if is_infra(a): continue
    origin, ext = a.get_actor_bounds(False)
    if ext.x < 1 and ext.y < 1 and ext.z < 1: continue
    all_bounds.append({'actor': a,
        'ox': origin.x, 'oy': origin.y, 'oz': origin.z,
        'ex': max(ext.x,1), 'ey': max(ext.y,1), 'ez': max(ext.z,1),
        'top_z': origin.z + ext.z})

floating = []
if ${checkType === 'floating' || checkType === 'both' ? 'True' : 'False'}:
    for a in scene:
        best = None  # None = no surface found below
        for other in all_bounds:
            if other['actor'] is a['actor']: continue
            if other['top_z'] >= a['bot_z'] - 1: continue
            if abs(other['ox'] - a['lx']) > (a['ex']*0.5 + other['ex']): continue
            if abs(other['oy'] - a['ly']) > (a['ey']*0.5 + other['ey']): continue
            if best is None or other['top_z'] > best: best = other['top_z']
        if best is None and a['bot_z'] > 0:
            floating.append({'name': a['name'], 'gap_cm': None,
                             'bottom_z': round(a['bot_z'], 1), 'surface_z': None, 'no_surface': True})
        elif best is not None and a['bot_z'] - best > FLOAT_THRESHOLD:
            floating.append({'name': a['name'], 'gap_cm': round(a['bot_z'] - best, 1),
                             'bottom_z': round(a['bot_z'], 1), 'surface_z': round(best, 1)})

collisions = []
if ${checkType === 'collisions' || checkType === 'both' ? 'True' : 'False'}:
    for i in range(len(scene)):
        a = scene[i]
        for j in range(i+1, len(scene)):
            b = scene[j]
            dx = abs(a['ox']-b['ox']) - (a['ex']+b['ex'])
            dy = abs(a['oy']-b['oy']) - (a['ey']+b['ey'])
            dz = abs(a['oz']-b['oz']) - (a['ez']+b['ez'])
            if dx < -TOUCH_TOL and dy < -TOUCH_TOL and dz < -TOUCH_TOL:
                pen = min(abs(dx),abs(dy),abs(dz)) - TOUCH_TOL
                collisions.append({'actor1': a['name'], 'actor2': b['name'],
                                   'penetration_depth': round(pen,1)})

print('RESULT:' + json.dumps({
    'floating': floating, 'floating_count': len(floating),
    'collisions': collisions, 'collision_count': len(collisions),
    'checked': len(scene),
}))
`;
}

async function toolCheckFloating({threshold_cm, actor_names} = {}) {
  const threshold = threshold_cm || 10;
  const names = (actor_names && actor_names.length > 0) ? actor_names : [...spawnedActors];
  if (names.length === 0)
    return {status:"ok", message:"No spawned actors to check", floating:[], checked:0};
  const script = _buildSceneCheckScript(names, threshold, 'floating');
  const raw  = await ueCommand("execute_python_script", {script}, 30000);
  const logs = (raw?.result?.python_logs || []);
  let data   = {floating:[], checked:names.length};
  for (const l of logs) {
    if (l.includes("RESULT:")) { try { data = JSON.parse(l.split("RESULT:")[1]); } catch {} break; }
  }
  const n = data.floating_count ?? data.floating.length;
  return {
    status: n > 0 ? "warning" : "ok",
    message: n > 0
      ? `${n} actor(s) floating >${threshold}cm above surface — fix with set_actor_transform`
      : `All ${data.checked} actor(s) grounded (gap ≤ ${threshold}cm)`,
    floating: (data.floating||[]).sort((a,b)=>a.gap_cm-b.gap_cm), checked: data.checked, threshold_cm: threshold,
    ground_z: data.ground_z,
  };
}

async function toolCheckCollisions({actor_names, touch_tolerance_cm} = {}) {
  const names = (actor_names && actor_names.length > 0) ? actor_names : [...spawnedActors];
  if (names.length === 0)
    return {status:"ok", message:"No spawned actors to check", collisions:[], checked:0};
  const script = _buildSceneCheckScript(names, 50, 'collisions');
  const raw  = await ueCommand("execute_python_script", {script}, 30000);
  const logs = (raw?.result?.python_logs || []);
  let data   = {collisions:[], checked:names.length};
  for (const l of logs) {
    if (l.includes("RESULT:")) { try { data = JSON.parse(l.split("RESULT:")[1]); } catch {} break; }
  }
  const n = data.collision_count ?? data.collisions.length;
  return {
    status: n > 0 ? "warning" : "ok",
    message: n > 0
      ? `${n} collision pair(s) detected — overlapping actors need repositioning`
      : `No collisions detected among ${data.checked} actor(s)`,
    collisions: data.collisions, checked: data.checked,
  };
}

const TOOL_DEFS=[{name:"spawn_blueprint_actor",description:"Spawn a SimWorld Blueprint actor (building, tree, vehicle, prop). Use this for all CityDatabase assets. The blueprint_id can be a full path like '/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C', or a shorthand like 'BP_Building_01', 'BP_Tree1', etc. For buildings you can even use just the number like '01' through '06'.",inputSchema:{type:"object",properties:{actor_name:{type:"string",description:"Unique name for this actor (e.g. 'House_01', 'Tree_Left_1')"},blueprint_id:{type:"string",description:"Blueprint path or shorthand. Buildings: 'BP_Building_01' to 'BP_Building_06' (ONLY 01-06 available) (or just number). Trees: 'BP_Tree1'-'BP_Tree6'. Vehicles: 'BP_Scooter_01'-'BP_Scooter_04', 'BP_Cart'. Props: 'BP_Hydrant', 'BP_Trash_bin_a', 'BP_Table', etc."},location:{type:"array",items:{type:"number"},description:"[x, y, z] in UE units (cm). 1m=100 units. Ground is 200m x 200m centered at origin, so keep X and Y between -9500 and 9500. Values outside this range will be clamped to stay on the ground."},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll] in degrees"},scale:{type:"array",items:{type:"number"},description:"[x, y, z] scale multipliers, default [1,1,1]"}},required:["actor_name","blueprint_id","location"]}},{name:"spawn_actor",description:"Spawn a static mesh actor. Use for basic shapes (/Engine/BasicShapes/Cube, Plane, etc.) or SM_ meshes. For SimWorld buildings/trees/props, prefer spawn_blueprint_actor instead.",inputSchema:{type:"object",properties:{name:{type:"string",description:"Unique actor name"},static_mesh:{type:"string",description:"Full mesh path, e.g. '/Engine/BasicShapes/Cube.Cube' or '/Game/CityDatabase/meshes/SM_Road.SM_Road'"},location:{type:"array",items:{type:"number"},description:"[x, y, z]"},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll]"},scale:{type:"array",items:{type:"number"},description:"[x, y, z]"}},required:["name","static_mesh","location"]}},{name:"delete_actor",description:"Delete an actor by its name.",inputSchema:{type:"object",properties:{name:{type:"string",description:"Actor name to delete"}},required:["name"]}},{name:"delete_all_spawned",description:"Delete ALL actors spawned in this session. Use to clear the scene before rebuilding.",inputSchema:{type:"object",properties:{}}},{name:"get_actors_in_level",description:"List all actors currently in the UE level.",inputSchema:{type:"object",properties:{}}},{name:"find_actors_by_name",description:"Search for actors whose name matches a pattern.",inputSchema:{type:"object",properties:{pattern:{type:"string",description:"Name pattern to search"}},required:["pattern"]}},{name:"set_actor_transform",description:"Move, rotate, or scale an existing actor.",inputSchema:{type:"object",properties:{name:{type:"string",description:"Actor name"},location:{type:"array",items:{type:"number"},description:"[x, y, z]"},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll]"},scale:{type:"array",items:{type:"number"},description:"[x, y, z]"}},required:["name"]}},{name:"take_screenshot",description:"Capture a screenshot of the current UE viewport and save it as PNG.",inputSchema:{type:"object",properties:{filename:{type:"string",description:"Output filename (optional, auto-generated if omitted)"}}}},{name:"execute_python_script",description:"Execute arbitrary Unreal Engine Python script. Use for advanced operations not covered by other tools.",inputSchema:{type:"object",properties:{script:{type:"string",description:"Python code to execute in UE"}},required:["script"]}},{name:"list_assets",description:"List available SimWorld assets. Returns buildings, trees, vehicles, street furniture, roads, and static meshes with their paths.",inputSchema:{type:"object",properties:{category:{type:"string",description:"Optional: 'buildings', 'trees', 'vehicles', 'street_furniture', 'roads', 'static_meshes'. Omit for all."}}}},{name:"verify_scene",description:"Call a verifier AI (Claude) to analyze the current scene. Takes a screenshot, gets all actors, then asks Claude to evaluate if placement is correct and matches the original request. Returns structured feedback with status (PASS/NEEDS_IMPROVEMENT/FAIL), issues found, and actionable suggestions. Use this after placing objects to check quality before finishing.",inputSchema:{type:"object",properties:{original_request:{type:"string",description:"The original scene generation request to verify against (e.g. 'a suburban street with 3 houses and 2 trees')"},focus_areas:{type:"string",description:"Optional: specific aspects to focus on (e.g. 'check building spacing', 'verify tree placement')"}},required:[]}},{name:"check_floating",description:"Check if spawned actors are floating above the ground. Uses bounding-box column search to find the nearest surface below each actor (works in editor mode without PIE). Warns if the gap exceeds threshold_cm. Call after placing objects to detect floating placement errors.",inputSchema:{type:"object",properties:{actor_names:{type:"array",items:{type:"string"},description:"Actor names to check. Omit to check all actors spawned this session."},threshold_cm:{type:"number",description:"Warning threshold in cm (default 50). Actors with gap > this are flagged."}}}},{name:"check_collisions",description:"Check if spawned actors are overlapping each other using AABB intersection. Works in editor mode without PIE. Surface contacts <=5cm are ignored as normal touching. Call after placing objects to detect interpenetrating actors.",inputSchema:{type:"object",properties:{actor_names:{type:"array",items:{type:"string"},description:"Actor names to check. Omit to check all actors spawned this session."}}}},{name:"spawn_agent",description:"Spawn a controllable agent. Requires PIE mode. Types: "+Object.keys(AGENT_REGISTRY.agentTypes).join(", "),inputSchema:{type:"object",properties:{agent_name:{type:"string",description:"Unique name"},agent_type:{type:"string",description:"Agent type: "+Object.keys(AGENT_REGISTRY.agentTypes).join(", ")},location:{type:"array",items:{type:"number"},description:"[x, y, z] spawn location"},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll]"}},required:["agent_name","agent_type"]}},{name:"agent_stop",description:"Stop an agent's movement.",inputSchema:{type:"object",properties:{agent_name:{type:"string"},agent_type:{type:"string",description:"Agent type (determines stop command)"}},required:["agent_name"]}},{name:"agent_rotate",description:"Rotate an agent.",inputSchema:{type:"object",properties:{agent_name:{type:"string"},angle:{type:"number",description:"Degrees"},direction:{type:"string",enum:["left","right"]},agent_type:{type:"string"}},required:["agent_name","angle","direction"]}},{name:"agent_action",description:"Perform an action. Actions vary by agent type ΓÇö call with an invalid action to see available ones.",inputSchema:{type:"object",properties:{agent_name:{type:"string"},action:{type:"string",description:"Action name (e.g. move_forward, set_speed, sit_down, pick_up, wave, etc.)"},agent_type:{type:"string",description:"Agent type"},params:{type:"object",description:"Action parameters (e.g. {speed:200}, {target:'Box_1'}, {duration:3})"}},required:["agent_name","action"]}},{name:"get_agent_state",description:"Get agent position and rotation.",inputSchema:{type:"object",properties:{agent_name:{type:"string"}},required:["agent_name"]}}],TOOL_HANDLERS={spawn_blueprint_actor:toolSpawnBlueprintActor,spawn_actor:toolSpawnActor,delete_actor:toolDeleteActor,delete_all_spawned:toolDeleteAllSpawned,get_actors_in_level:toolGetActors,find_actors_by_name:toolFindActors,set_actor_transform:toolSetActorTransform,take_screenshot:toolTakeScreenshot,execute_python_script:toolExecutePython,list_assets:toolListAssets,verify_scene:toolVerifyScene,check_floating:toolCheckFloating,check_collisions:toolCheckCollisions,spawn_agent:toolSpawnAgent,agent_stop:toolAgentStop,agent_rotate:toolAgentRotate,agent_action:toolAgentAction,get_agent_state:toolGetAgentState};function sendResponse(e,t){const s=JSON.stringify({jsonrpc:"2.0",id:e,result:t});process.stdout.write(s+`
`)}function sendError(e,t,s){const n=JSON.stringify({jsonrpc:"2.0",id:e,error:{code:t,message:s}});process.stdout.write(n+`
`)}async function handleRequest(e){const{id:t,method:s,params:n}=e;if(s==="initialize")return sendResponse(t,{protocolVersion:"2024-11-05",capabilities:{tools:{listChanged:!1}},serverInfo:{name:"simworld-arena-mcp",version:"1.0.0"}});if(s!=="notifications/initialized"){if(s==="tools/list")return sendResponse(t,{tools:TOOL_DEFS});if(s==="tools/call"){const o=n?.name,r=n?.arguments||{},c=TOOL_HANDLERS[o];if(!c)return sendResponse(t,{content:[{type:"text",text:JSON.stringify({error:`Unknown tool: ${o}`})}],isError:!0});try{const a=await c(r);return sendResponse(t,{content:[{type:"text",text:JSON.stringify(a,null,2)}],isError:!1})}catch(a){return sendResponse(t,{content:[{type:"text",text:JSON.stringify({error:a.message})}],isError:!0})}}if(s==="resources/list")return sendResponse(t,{resources:[]});if(s==="prompts/list")return sendResponse(t,{prompts:[]});t!==void 0&&sendError(t,-32601,`Method not found: ${s}`)}}const rl=readline.createInterface({input:process.stdin,terminal:!1});rl.on("line",e=>{const t=e.trim();if(t)try{const s=JSON.parse(t);handleRequest(s).catch(n=>{process.stderr.write(`[mcp-server] Error: ${n.message}
`),s.id!==void 0&&sendError(s.id,-32603,n.message)})}catch{process.stderr.write(`[mcp-server] Invalid JSON: ${t.slice(0,100)}
`)}}),process.stderr.write(`[mcp-server] SimWorld Studio MCP server started (stdio)
`);
