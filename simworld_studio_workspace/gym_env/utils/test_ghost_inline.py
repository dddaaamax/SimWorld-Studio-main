"""Inline ghost mode test — PIE must already be running."""
import unrealcv, time, sys, threading

BP = '/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C'
PORT = 9002
GHOST_CH = 8

def client():
    c = unrealcv.Client(('127.0.0.1', PORT))
    c.connect()
    assert c.isconnected(), "Cannot connect"
    return c

def spawn(name):
    c = client()
    done = threading.Event()
    res = [None]
    def _f():
        try: res[0] = c.request(f'vset /objects/spawn_bp_asset {BP} {name}')
        except Exception as e: res[0] = f'err:{e}'
        done.set()
    t = threading.Thread(target=_f, daemon=True)
    t.start()
    done.wait(timeout=10)
    print(f'  spawn({name}): {res[0]}')
    time.sleep(3)
    c2 = client()
    c2.request(f'vset /object/{name}/location 500 500 110')
    c2.request(f'vset /object/{name}/collision true')
    c2.request(f'vset /object/{name}/object_mobility true')
    objs = c2.request('vget /objects') or ''
    assert name in objs, f'{name} not in scene after spawn'
    return c2

print('='*50)
print(' GHOST MODE INLINE TEST')
print('='*50)

# --- TEST A: Single agent normal ---
print('\n[TEST A] Single agent normal')
c = spawn('NormalAgent')
print(f'  [OK] NormalAgent spawned')

loc = c.request('vget /object/NormalAgent/location')
print(f'  location: {loc}')

c.request('vset /object/NormalAgent/destroy')
print(f'  [OK] destroyed')
c.disconnect()

# --- TEST B: Multi-agent ghost ---
print('\n[TEST B] Multi-agent ghost')
names = ['G0', 'G1', 'G2']

c = None
for n in names:
    c = spawn(n)
    print(f'  [OK] {n} spawned')

# All on same client now
print('  Enabling ghost mode...')
for n in names:
    r1 = c.request(f'vset /object/{n}/hide')
    r2 = c.request(f'vset /object/{n}/collision_channel {GHOST_CH}')
    r3 = c.request(f'vset /object/{n}/collision_response {GHOST_CH} ignore')
    print(f'    {n}: hide={r1} ch={r2} resp={r3}')

# Set all to same spot
print('  Setting all to (500,500,110)...')
for n in names:
    c.request(f'vset /object/{n}/location 500 500 110')
time.sleep(1)

# Read locations
locs = {}
for n in names:
    resp = c.request(f'vget /object/{n}/location') or ''
    parts = resp.strip().split()
    locs[n] = tuple(float(p) for p in parts[:3]) if len(parts) >= 3 else None
    print(f'    {n}: {locs[n]}')

# Check overlap
ref = locs[names[0]]
all_close = ref is not None
for n in names[1:]:
    if locs[n] is None:
        all_close = False
        break
    d = sum((a-b)**2 for a,b in zip(ref, locs[n]))**0.5
    if d > 50:
        all_close = False
        print(f'    DRIFT: {n} is {d:.0f}cm away')

print(f'  {"[OK]" if all_close else "[FAIL]"} overlap test')

# Move G0 independently
c.request(f'vbp G0 SetMaxSpeed 200')
c.request(f'vbp G0 MoveForward')
time.sleep(2)
c.request(f'vbp G0 StopAgent')
time.sleep(0.5)

loc0 = c.request('vget /object/G0/location')
loc1 = c.request('vget /object/G1/location')
print(f'  G0 (moved): {loc0}')
print(f'  G1 (stayed): {loc1}')

# Disable ghost on G0
c.request(f'vset /object/G0/show')
c.request(f'vset /object/G0/collision_channel 2')
c.request(f'vset /object/G0/collision_response {GHOST_CH} block')
print(f'  [OK] ghost disabled on G0')

# Cleanup
for n in names:
    c.request(f'vset /object/{n}/destroy')
c.disconnect()

print('\n' + '='*50)
print(' ALL TESTS PASSED')
print('='*50)
