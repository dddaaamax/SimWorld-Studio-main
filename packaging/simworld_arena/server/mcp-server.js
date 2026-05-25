"use strict";const net=require("net"),fs=require("fs"),path=require("path"),readline=require("readline"),UE_HOST=process.env.UNREAL_HOST||"127.0.0.1",UE_PORT=parseInt(process.env.UNREAL_PORT||"55559",10),SCREENSHOT_DIR=path.resolve(__dirname,"../../tmp/screens"),ASSETS=JSON.parse(fs.readFileSync(path.resolve(__dirname,"assets.json"),"utf-8"));fs.mkdirSync(SCREENSHOT_DIR,{recursive:!0});const spawnedActors=new Set,cmdQueue=[];let cmdRunning=!1;function ueCommand(e,t,s=3e4){return new Promise((n,o)=>{cmdQueue.push({type:e,params:t,timeoutMs:s,resolve:n,reject:o}),processQueue()})}function processQueue(){if(cmdRunning||cmdQueue.length===0)return;cmdRunning=!0;const{type:e,params:t,timeoutMs:s,resolve:n,reject:o}=cmdQueue.shift(),r=new net.Socket,c=setTimeout(()=>{r.destroy(),cmdRunning=!1,o(new Error(`UE command '${e}' timed out after ${s}ms`)),processQueue()},s);r.connect(UE_PORT,UE_HOST,()=>{r.write(JSON.stringify({type:e,params:t})+`
`)});let a="";r.on("data",i=>{a+=i.toString();try{const p=JSON.parse(a);clearTimeout(c),r.destroy(),cmdRunning=!1,n(p),processQueue()}catch{}}),r.on("error",i=>{clearTimeout(c),cmdRunning=!1,o(new Error(`UE connection error: ${i.message}`)),processQueue()}),r.on("close",()=>{if(clearTimeout(c),a.trim())try{cmdRunning=!1,n(JSON.parse(a)),processQueue()}catch{cmdRunning=!1,o(new Error("Incomplete response from UE")),processQueue()}})}async function toolSpawnBlueprintActor({actor_name:e,blueprint_id:t,location:s,rotation:n,scale:o}){let r=t;if(!r.startsWith("/Game/")){let a=!1;for(const i of["trees","vehicles","street_furniture","roads"]){const l=(ASSETS[i]?.items||[]).find(_=>{const u=_.split("/").pop().split(".")[0];return u===r||u.toLowerCase()===r.toLowerCase()});if(l){r=l,a=!0;break}}if(!a){const i=parseInt(r.replace(/\D/g,""),10);if((/^(BP_Building_)?\d+$/.test(r)||/^Building_\d+$/.test(r))&&!isNaN(i)&&ASSETS.buildings.ids.includes(i)){const l=String(i).padStart(2,"0");r=`/Game/CityDatabase/blueprints/BP_Building_${l}.BP_Building_${l}_C`}else if((!isNaN(i)&&(/^(BP_Building_)?\d+$/.test(r)||/^Building_\d+$/.test(r)))){return{status:"error",message:`Building ${i} is not available. Only buildings 01-06 are included in this package. Use BP_Building_01 through BP_Building_06.`}}else{for(const l of["trees","vehicles","street_furniture","roads"]){const u=(ASSETS[l]?.items||[]).find(m=>m.toLowerCase().includes(r.toLowerCase()));if(u){r=u,a=!0;break}}a||(r=`/Game/CityDatabase/blueprints/${r}.${r}_C`)}}}if(r.startsWith("/Game/")&&!r.endsWith("_C")){const a=r.split(".");if(a.length===2)r=`${a[0]}.${a[1]}_C`;else{const i=r.split("/").pop();r=`${r}.${i}_C`}}const loc=s||[0,0,0],GROUND_HALF=9500;loc[0]=Math.max(-GROUND_HALF,Math.min(GROUND_HALF,loc[0]));loc[1]=Math.max(-GROUND_HALF,Math.min(GROUND_HALF,loc[1]));if(loc[2]<0)loc[2]=0;const c=await ueCommand("spawn_blueprint_actor",{actor_name:e,blueprint_name:r,location:loc,rotation:n||[0,0,0]});if(c.status==="success"){spawnedActors.add(e);const ueActorName=c.result?.name||e;try{await ueCommand("execute_python_script",{script:`
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
`},15000)}catch(fixErr){}if(o&&(o[0]!==1||o[1]!==1||o[2]!==1))await ueCommand("set_actor_transform",{name:e,scale:o})}return c}async function toolSpawnActor({name:e,static_mesh:t,location:s,rotation:n,scale:o}){const loc2=s||[0,0,0],GH2=9500;loc2[0]=Math.max(-GH2,Math.min(GH2,loc2[0]));loc2[1]=Math.max(-GH2,Math.min(GH2,loc2[1]));if(loc2[2]<0)loc2[2]=0;const r=await ueCommand("spawn_actor",{name:e,type:"StaticMeshActor",location:loc2,rotation:n||[0,0,0],scale:o||[1,1,1],static_mesh:t});return r.status==="success"&&spawnedActors.add(e),r}async function toolDeleteActor({name:e}){const t=await ueCommand("delete_actor",{name:e});if(t.status==="success")return spawnedActors.delete(e),t;const s=await ueCommand("execute_python_script",{script:`
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
`});spawnedActors.clear();const s=t?.result?.python_logs||[];return{result:t?.result,logs:s}}async function toolGetActors(){return ueCommand("get_actors_in_level",{})}async function toolFindActors({pattern:e}){return ueCommand("find_actors_by_name",{pattern:e})}async function toolSetActorTransform({name:e,location:t,rotation:s,scale:n}){const o={name:e};return t&&(o.location=t),s&&(o.rotation=s),n&&(o.scale=n),ueCommand("set_actor_transform",o)}async function toolTakeScreenshot({filename:e}){const t=e||`screenshot_${Date.now()}.png`,s=path.join(SCREENSHOT_DIR,t);return ueCommand("take_screenshot",{filepath:s})}async function toolSetCamera({location:e,rotation:t}){if(e&&t){const o=`
import unreal
subsys = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
loc = unreal.Vector(${e[0]}, ${e[1]}, ${e[2]})
rot = unreal.Rotator(${t[0]}, ${t[1]}, 0.0)
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
    cam_rot = unreal.Rotator(-90, 0, 0)
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
    cam_rot = unreal.Rotator(pitch, yaw, 0.0)

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
`});return spawnedActors.add("Arena_Env_Ground"),{status:"success",message:`Environment set up: sun (${n}), sky atmosphere, sky light, fog, ground (${s*100}m x ${s*100}m), view distance culling disabled`,steps:{atmosphere:c?.result?.python_logs,sun:a?.result?.python_logs,skylight:i?.result?.python_logs,fog:p?.result?.python_logs},ground:l?.result?.python_logs}}const TOOL_DEFS=[{name:"spawn_blueprint_actor",description:"Spawn a SimWorld Blueprint actor (building, tree, vehicle, prop). Use this for all CityDatabase assets. The blueprint_id can be a full path like '/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C', or a shorthand like 'BP_Building_01', 'BP_Tree1', etc. For buildings you can even use just the number like '01' through '06'.",inputSchema:{type:"object",properties:{actor_name:{type:"string",description:"Unique name for this actor (e.g. 'House_01', 'Tree_Left_1')"},blueprint_id:{type:"string",description:"Blueprint path or shorthand. Buildings: 'BP_Building_01' to 'BP_Building_06' (ONLY 01-06 available) (or just number). Trees: 'BP_Tree1'-'BP_Tree6'. Vehicles: 'BP_Scooter_01'-'BP_Scooter_04', 'BP_Cart'. Props: 'BP_Hydrant', 'BP_Trash_bin_a', 'BP_Table', etc."},location:{type:"array",items:{type:"number"},description:"[x, y, z] in UE units (cm). 1m=100 units. Ground is 200m x 200m centered at origin, so keep X and Y between -9500 and 9500. Values outside this range will be clamped to stay on the ground."},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll] in degrees"},scale:{type:"array",items:{type:"number"},description:"[x, y, z] scale multipliers, default [1,1,1]"}},required:["actor_name","blueprint_id","location"]}},{name:"spawn_actor",description:"Spawn a static mesh actor. Use for basic shapes (/Engine/BasicShapes/Cube, Plane, etc.) or SM_ meshes. For SimWorld buildings/trees/props, prefer spawn_blueprint_actor instead.",inputSchema:{type:"object",properties:{name:{type:"string",description:"Unique actor name"},static_mesh:{type:"string",description:"Full mesh path, e.g. '/Engine/BasicShapes/Cube.Cube' or '/Game/CityDatabase/meshes/SM_Road.SM_Road'"},location:{type:"array",items:{type:"number"},description:"[x, y, z]"},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll]"},scale:{type:"array",items:{type:"number"},description:"[x, y, z]"}},required:["name","static_mesh","location"]}},{name:"delete_actor",description:"Delete an actor by its name.",inputSchema:{type:"object",properties:{name:{type:"string",description:"Actor name to delete"}},required:["name"]}},{name:"delete_all_spawned",description:"Delete ALL actors spawned in this session. Use to clear the scene before rebuilding.",inputSchema:{type:"object",properties:{}}},{name:"get_actors_in_level",description:"List all actors currently in the UE level.",inputSchema:{type:"object",properties:{}}},{name:"find_actors_by_name",description:"Search for actors whose name matches a pattern.",inputSchema:{type:"object",properties:{pattern:{type:"string",description:"Name pattern to search"}},required:["pattern"]}},{name:"set_actor_transform",description:"Move, rotate, or scale an existing actor.",inputSchema:{type:"object",properties:{name:{type:"string",description:"Actor name"},location:{type:"array",items:{type:"number"},description:"[x, y, z]"},rotation:{type:"array",items:{type:"number"},description:"[pitch, yaw, roll]"},scale:{type:"array",items:{type:"number"},description:"[x, y, z]"}},required:["name"]}},{name:"take_screenshot",description:"Capture a screenshot of the current UE viewport and save it as PNG.",inputSchema:{type:"object",properties:{filename:{type:"string",description:"Output filename (optional, auto-generated if omitted)"}}}},{name:"execute_python_script",description:"Execute arbitrary Unreal Engine Python script. Use for advanced operations not covered by other tools.",inputSchema:{type:"object",properties:{script:{type:"string",description:"Python code to execute in UE"}},required:["script"]}},{name:"list_assets",description:"List available SimWorld assets. Returns buildings, trees, vehicles, street furniture, roads, and static meshes with their paths.",inputSchema:{type:"object",properties:{category:{type:"string",description:"Optional: 'buildings', 'trees', 'vehicles', 'street_furniture', 'roads', 'static_meshes'. Omit for all."}}}},{name:"setup_environment",description:"CALL THIS FIRST before spawning any objects! Sets up the scene environment: directional light (sun), sky atmosphere, sky light, fog, ground plane, and increases view distance. Without this, the scene will be black/empty.",inputSchema:{type:"object",properties:{ground_size:{type:"number",description:"Ground plane scale (default 200 = 20km x 20km). Use 100 for small scenes, 300 for large cities."},time_of_day:{type:"string",description:"'morning', 'noon', 'afternoon' (default), 'sunset', or 'night'"}}}}],TOOL_HANDLERS={spawn_blueprint_actor:toolSpawnBlueprintActor,spawn_actor:toolSpawnActor,delete_actor:toolDeleteActor,delete_all_spawned:toolDeleteAllSpawned,get_actors_in_level:toolGetActors,find_actors_by_name:toolFindActors,set_actor_transform:toolSetActorTransform,take_screenshot:toolTakeScreenshot,execute_python_script:toolExecutePython,list_assets:toolListAssets,setup_environment:toolSetupEnvironment};function sendResponse(e,t){const s=JSON.stringify({jsonrpc:"2.0",id:e,result:t});process.stdout.write(s+`
`)}function sendError(e,t,s){const n=JSON.stringify({jsonrpc:"2.0",id:e,error:{code:t,message:s}});process.stdout.write(n+`
`)}async function handleRequest(e){const{id:t,method:s,params:n}=e;if(s==="initialize")return sendResponse(t,{protocolVersion:"2024-11-05",capabilities:{tools:{listChanged:!1}},serverInfo:{name:"simworld-arena-mcp",version:"1.0.0"}});if(s!=="notifications/initialized"){if(s==="tools/list")return sendResponse(t,{tools:TOOL_DEFS});if(s==="tools/call"){const o=n?.name,r=n?.arguments||{},c=TOOL_HANDLERS[o];if(!c)return sendResponse(t,{content:[{type:"text",text:JSON.stringify({error:`Unknown tool: ${o}`})}],isError:!0});try{const a=await c(r);return sendResponse(t,{content:[{type:"text",text:JSON.stringify(a,null,2)}],isError:!1})}catch(a){return sendResponse(t,{content:[{type:"text",text:JSON.stringify({error:a.message})}],isError:!0})}}if(s==="resources/list")return sendResponse(t,{resources:[]});if(s==="prompts/list")return sendResponse(t,{prompts:[]});t!==void 0&&sendError(t,-32601,`Method not found: ${s}`)}}const rl=readline.createInterface({input:process.stdin,terminal:!1});rl.on("line",e=>{const t=e.trim();if(t)try{const s=JSON.parse(t);handleRequest(s).catch(n=>{process.stderr.write(`[mcp-server] Error: ${n.message}
`),s.id!==void 0&&sendError(s.id,-32603,n.message)})}catch{process.stderr.write(`[mcp-server] Invalid JSON: ${t.slice(0,100)}
`)}}),process.stderr.write(`[mcp-server] SimWorld Studio MCP server started (stdio)
`);
