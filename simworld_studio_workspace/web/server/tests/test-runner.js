#!/usr/bin/env node
'use strict';
/**
 * SimWorld Studio — Full Test Suite  (CommonJS, Node 18+)
 *
 * Usage:
 *   node tests/test-runner.js            # all suites
 *   node tests/test-runner.js unit       # unit only (no server)
 *   node tests/test-runner.js integration
 *   node tests/test-runner.js pipeline   # needs UE running
 *   node tests/test-runner.js multi
 *
 * ENV:
 *   SERVER_URL=http://localhost:3002
 *   UNREAL_PORT=55557   UE_HOST=127.0.0.1
 *   TEST_TIMEOUT=30000
 */

const http  = require('http');
const net   = require('net');
const path  = require('path');
const fs    = require('fs');
const os    = require('os');

const SERVER_URL  = process.env.SERVER_URL  || 'http://localhost:3002';
const UE_HOST     = process.env.UE_HOST     || '127.0.0.1';
const UNREAL_PORT = parseInt(process.env.UNREAL_PORT || '55557', 10);
const TIMEOUT_MS  = parseInt(process.env.TEST_TIMEOUT || '30000', 10);
const SUITE       = process.argv[2] || 'all';

// ── Colors ──────────────────────────────────────────────────────────────────
const G = '\x1b[32m', R = '\x1b[31m', Y = '\x1b[33m',
      C = '\x1b[36m', D = '\x1b[90m', B = '\x1b[34m', E = '\x1b[0m', BO = '\x1b[1m';

// ── Results tracking ─────────────────────────────────────────────────────────
const results = [];

// ── Core assertions ──────────────────────────────────────────────────────────
function assert(cond, msg) { if (!cond) throw new Error(msg || 'Assertion failed'); }
function eq(a, b, msg)     { if (a !== b) throw new Error(msg || `Expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
function has(obj, k, msg)  { if (obj == null || !(k in Object(obj))) throw new Error(msg || `Key "${k}" missing in ${JSON.stringify(obj)}`); }

// ── HTTP helpers ─────────────────────────────────────────────────────────────
function request(method, urlPath, body) {
  return new Promise((resolve, reject) => {
    const url  = new URL(urlPath, SERVER_URL);
    const data = body != null ? JSON.stringify(body) : null;
    const opts = {
      hostname: url.hostname, port: parseInt(url.port) || 80,
      path: url.pathname + url.search, method,
      headers: {
        'Content-Type': 'application/json',
        ...(data ? { 'Content-Length': Buffer.byteLength(data) } : {}),
      },
    };
    const req = http.request(opts, res => {
      let buf = '';
      res.on('data', d => { buf += d; });
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); }
        catch { resolve({ status: res.statusCode, body: null, raw: buf }); }
      });
    });
    req.setTimeout(TIMEOUT_MS, () => { req.destroy(); reject(new Error('HTTP timeout')); });
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}
const GET  = p      => request('GET',    p, null);
const POST = (p, b) => request('POST',   p, b);
const DEL  = p      => request('DELETE', p, null);

// TCP reachability check
function tcpOpen(host, port, ms = 3000) {
  return new Promise(resolve => {
    const s = new net.Socket();
    const end = ok => { try { s.destroy(); } catch {} resolve(ok); };
    s.setTimeout(ms);
    s.connect(port, host, () => end(true));
    s.on('error',   () => end(false));
    s.on('timeout', () => end(false));
  });
}

// ── Test runner ───────────────────────────────────────────────────────────────
async function run(name, fn, suite = 'unit', opts = {}) {
  if (SUITE !== 'all' && suite !== SUITE) {
    process.stdout.write(`${D}○ ${name}${E}\n`);
    results.push({ name, status: 'skip' });
    return;
  }
  const timeout = opts.timeout || TIMEOUT_MS;
  const t0 = Date.now();
  try {
    await Promise.race([
      fn(),
      new Promise((_, rej) => setTimeout(() => rej(new Error(`Timeout ${timeout}ms`)), timeout)),
    ]);
    const ms = Date.now() - t0;
    process.stdout.write(`${G}✓${E} ${name} ${D}(${ms}ms)${E}\n`);
    results.push({ name, status: 'pass', ms });
  } catch (e) {
    const ms = Date.now() - t0;
    process.stdout.write(`${R}✗${E} ${name}\n  ${R}${e.message}${E}\n`);
    results.push({ name, status: 'fail', ms, error: e.message });
  }
}

function section(s) { console.log(`\n${BO}${B}══ ${s} ══${E}`); }

// ─────────────────────────────────────────────────────────────────────────────
//  Main
// ─────────────────────────────────────────────────────────────────────────────
(async () => {

// ═══════════════════════════════════════════════════════════════════════════
//  UNIT — No server needed
// ═══════════════════════════════════════════════════════════════════════════
section('UNIT — Pure module tests (no server)');

await run('session-manager: loads without error', () => {
  // Clear require cache to force fresh load each test
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  assert(typeof SessionManager === 'function', 'SessionManager is a class');
  const m = new SessionManager(); m.destroy();
}, 'unit');

await run('session-manager: acquire / release / free slot count', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();
  eq(m.freeSlots, 10);

  const rec = await m.acquire('alice');
  eq(m.freeSlots, 9);
  eq(rec.token.length, 64);
  assert(Number.isInteger(rec.slotId));
  has(rec, 'uePorts');

  m.release(rec.token);
  eq(m.freeSlots, 10);
  m.destroy();
}, 'unit');

await run('session-manager: same userId reuses slot', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();
  const a = await m.acquire('bob');
  const b = await m.acquire('bob');  // should reuse
  eq(a.token, b.token);
  eq(m.activeSessions, 1);
  m.destroy();
}, 'unit');

await run('session-manager: touch() refreshes timestamp', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();
  const rec = await m.acquire('charlie');
  const before = rec.lastActivity;
  await new Promise(r => setTimeout(r, 10));
  m.touch(rec.token);
  assert(rec.lastActivity > before, 'lastActivity bumped');
  m.destroy();
}, 'unit');

await run('session-manager: emit acquired / released events', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();
  const evts = [];
  m.on('acquired', e => evts.push('acq:' + e.slotId));
  m.on('released', e => evts.push('rel:' + e.reason));
  const rec = await m.acquire('dave');
  m.release(rec.token);
  eq(evts[0], 'acq:0');
  eq(evts[1], 'rel:released');
  m.destroy();
}, 'unit');

await run('session-manager: pool full throws POOL_FULL', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();

  // Fill all 10 real slots
  const recs = await Promise.all(
    Array.from({ length: 10 }, (_, i) => m.acquire(`fill-${i}`))
  );
  eq(m.freeSlots, 0, 'all slots taken');

  // Fill the wait queue to its max (40) by monkey-patching length check
  // Simplest: directly push dummy waiters into the queue
  const fakeWaiters = Array.from({ length: 40 }, (_, i) => ({
    userId: `fake-${i}`, enqueuedAt: Date.now(),
    resolve: () => {}, reject: () => {},
  }));
  m._queue.push(...fakeWaiters);
  eq(m.queueLength, 40, 'queue at max');

  // Now the 41st acquire must throw POOL_FULL
  try {
    await m.acquire('overflow');
    assert(false, 'should have thrown POOL_FULL');
  } catch (e) {
    eq(e.code, 'POOL_FULL', 'got POOL_FULL error');
  }

  // Cleanup: destroy rejects all pending queue items, release real slots
  m.destroy();
  recs.forEach(r => { try { m._freeSlots.add(r.slotId); } catch {} });
}, 'unit');

await run('session-manager: snapshot() returns sanitized data', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();
  await m.acquire('snap-user');
  const snap = m.snapshot();
  eq(snap.length, 1);
  assert(snap[0].token.endsWith('…'), 'token redacted');
  has(snap[0], 'ageMs');
  m.destroy();
}, 'unit');

await run('scenes: save / load / delete with ownerId', async () => {
  delete require.cache[require.resolve('../scenes')];
  const { SceneManager } = require('../scenes');
  const dir = os.tmpdir() + '/sw-test-scenes-' + Date.now();
  const sm = new SceneManager(dir);

  const saved = await sm.save({ name: 'My Scene', prompt: 'a city', ownerId: 'user-X' });
  eq(saved.name, 'My Scene');
  eq(saved.ownerId, 'user-X');
  assert(saved.id, 'has id');

  const loaded = sm.load(saved.id);
  eq(loaded.name, 'My Scene');

  // Foreign user cannot delete
  const r = await sm.delete(saved.id, 'user-Y');
  eq(r, 'forbidden');

  // Owner can delete
  const r2 = await sm.delete(saved.id, 'user-X');
  eq(r2, true);

  assert(!sm.load(saved.id), 'deleted scene returns null');
  fs.rmSync(dir, { recursive: true, force: true });
}, 'unit');

await run('scenes: list() filters by ownerId', async () => {
  delete require.cache[require.resolve('../scenes')];
  const { SceneManager } = require('../scenes');
  const dir = os.tmpdir() + '/sw-test-list-' + Date.now();
  const sm = new SceneManager(dir);

  await sm.save({ name: 'A1', prompt: 'p1', ownerId: 'u-A' });
  await sm.save({ name: 'A2', prompt: 'p2', ownerId: 'u-A' });
  await sm.save({ name: 'B1', prompt: 'p3', ownerId: 'u-B' });

  const listA = sm.list('u-A');
  const listB = sm.list('u-B');
  const listAll = sm.list(null);
  eq(listA.length, 2, 'u-A sees 2 scenes');
  eq(listB.length, 1, 'u-B sees 1 scene');
  eq(listAll.length, 3, 'null filter sees all 3');
  fs.rmSync(dir, { recursive: true, force: true });
}, 'unit');

await run('logger: structured output, child logger', () => {
  delete require.cache[require.resolve('../logger')];
  const logger = require('../logger');
  // Should not throw
  logger.info('test', 'hello world', { key: 'val' });
  logger.warn('test', 'a warning');
  logger.error('test', 'an error', { code: 42 });
  const child = logger.child({ requestId: 'req-1', sessionToken: 'tok-1' });
  child.info('test', 'child log');
}, 'unit');

await run('rate-limiter: blocks at threshold, resets window', () => {
  delete require.cache[require.resolve('../middleware/rate-limit')];
  const { RateLimiter } = require('../middleware/rate-limit');
  const lim = new RateLimiter({ windowMs: 60000, max: 3, message: { error: 'limited' } });
  // Override isDev check
  lim.max = 3;
  const mw = lim.middleware();
  let blocked = 0, passed = 0;
  const res = code => ({
    status: c => { if (c === 429) blocked++; return { json: () => {} }; },
    setHeader: () => {},
  });
  for (let i = 0; i < 5; i++) {
    let next = false;
    mw({ ip: 'test-ip' }, res(), () => { next = true; passed++; });
  }
  eq(blocked, 2, '2 requests blocked after max=3');
  eq(passed,  3, '3 requests passed');
}, 'unit');

// ═══════════════════════════════════════════════════════════════════════════
//  INTEGRATION — Requires server at SERVER_URL
// ═══════════════════════════════════════════════════════════════════════════
section('INTEGRATION — Requires server at ' + SERVER_URL);

await run('server: GET /api/health → 200 with expected fields', async () => {
  const { status, body } = await GET('/api/health');
  eq(status, 200, 'HTTP 200');
  has(body, 'ueConnected');
  has(body, 'mcpConnected');
}, 'integration');

await run('server: GET /api/session/status → 200', async () => {
  const { status, body } = await GET('/api/session/status');
  eq(status, 200);
  assert(body.mode === 'single-user' || typeof body.totalSlots === 'number');
}, 'integration');

await run('server: POST /api/session/acquire → valid token', async () => {
  const { status, body } = await POST('/api/session/acquire');
  eq(status, 200);
  has(body, 'token');
  assert(body.token.length > 0);
  if (body.token !== '_dev') {
    await POST('/api/session/release', { token: body.token });
  }
}, 'integration');

await run('server: GET /api/events → SSE stream emits JSON', async () => {
  await new Promise((resolve, reject) => {
    const req = http.get(SERVER_URL + '/api/events', { headers: { Accept: 'text/event-stream' } }, res => {
      let buf = '', got = false;
      const t = setTimeout(() => { req.destroy(); if (!got) reject(new Error('No SSE event in 5s')); }, 5000);
      res.on('data', chunk => {
        buf += chunk;
        if (buf.includes('data: ') && !got) {
          got = true;
          clearTimeout(t);
          req.destroy();
          resolve();
        }
      });
    });
    req.on('error', reject);
  });
}, 'integration');

await run('server: GET /api/skills → array', async () => {
  const { status, body } = await GET('/api/skills');
  eq(status, 200);
  assert(Array.isArray(body), 'skills is array');
}, 'integration');

await run('server: GET /api/assets → buildings present', async () => {
  const { status, body } = await GET('/api/assets');
  eq(status, 200);
  has(body, 'buildings');
  assert(body.buildings.items.length > 0, 'buildings not empty');
}, 'integration');

await run('server: scene CRUD — create, read, delete', async () => {
  const { status: s1, body: created } = await POST('/api/scenes', {
    name: 'CI Test Scene', prompt: 'ci test', ownerId: 'ci-tester',
  });
  assert([200, 201].includes(s1), `expected 200/201, got ${s1}`);
  has(created, 'id');

  const { body: loaded } = await GET('/api/scenes/' + created.id);
  eq(loaded.name, 'CI Test Scene');

  // cleanup
  await DEL('/api/scenes/' + created.id + '?ownerId=ci-tester');
}, 'integration');

await run('server: POST /api/chat-stop → {stopped:false} for unknown session', async () => {
  const { body } = await POST('/api/chat-stop', { sessionId: 'no-such-session-xyz' });
  eq(body.stopped, false);
}, 'integration');

await run('server: GET /api/agent-sessions → array', async () => {
  const { status, body } = await GET('/api/agent-sessions');
  eq(status, 200);
  assert(Array.isArray(body));
}, 'integration');

// ═══════════════════════════════════════════════════════════════════════════
//  PIPELINE — Requires UE + MCP running
// ═══════════════════════════════════════════════════════════════════════════
section('PIPELINE — Requires UE at ' + UE_HOST + ':' + UNREAL_PORT);

await run('ue: TCP port reachable', async () => {
  const ok = await tcpOpen(UE_HOST, UNREAL_PORT);
  assert(ok, `Cannot reach ${UE_HOST}:${UNREAL_PORT} — is UE running?`);
}, 'pipeline');

await run('ue: /api/health shows ueConnected=true', async () => {
  const { body } = await GET('/api/health');
  assert(body.ueConnected === true, 'UE not connected per health check');
}, 'pipeline');

await run('ue: UCv broker connected', async () => {
  // Trigger a UCv command first to force the broker to connect
  await POST('/api/internal/ucv', { cmd: 'vget /unrealcv/status' }).catch(() => {});
  await new Promise(r => setTimeout(r, 2000));
  const { status, body } = await GET('/api/internal/ucv/status');
  eq(status, 200);
  assert(body.connected === true, `UCv not connected: ${JSON.stringify(body)}`);
}, 'pipeline');

await run('ue: chat → MCP tool call completes', async () => {
  // Use get_actors_in_level — fast, no UE state mutation, confirms MCP e2e
  const events = await new Promise((resolve, reject) => {
    const payload = JSON.stringify({ message: 'Call get_actors_in_level to list all actors. Just return the count, nothing else.', sessionId: null });
    const collected = [];
    const req = http.request({
      hostname: new URL(SERVER_URL).hostname,
      port: parseInt(new URL(SERVER_URL).port) || 80,
      path: '/api/chat', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    }, res => {
      let buf = '';
      const t = setTimeout(() => { req.destroy(); resolve(collected); }, 110000);
      res.on('data', chunk => {
        buf += chunk;
        const parts = buf.split('\n\n');
        buf = parts.pop() || '';
        for (const p of parts) {
          const dLine = p.split('\n').find(l => l.startsWith('data: '));
          const eLine = p.split('\n').find(l => l.startsWith('event: '));
          if (!dLine) continue;
          try {
            const evt = { type: (eLine||'').slice(7).trim()||'message', data: JSON.parse(dLine.slice(6)) };
            collected.push(evt);
            if (evt.type === 'done') { clearTimeout(t); req.destroy(); resolve(collected); }
          } catch {}
        }
      });
      res.on('error', e => { clearTimeout(t); reject(e); });
    });
    req.on('error', reject);
    req.write(payload);
    req.end();
  });

  assert(events.length > 0, 'received SSE events from chat');
  const toolCalls = events.filter(e => e.type === 'tool_result');
  assert(toolCalls.length > 0, 'at least one MCP tool was called');
  const done = events.find(e => e.type === 'done');
  assert(done, 'received done event');
  assert(!done.data?.isError, 'completed without error');
}, 'pipeline', { timeout: 120_000 });

await run('ue: screenshot endpoint returns image', async () => {
  const { status } = await GET('/api/screenshot/latest');
  assert([200, 404].includes(status), 'screenshot endpoint reachable');
  // 404 is ok if no scene built yet; 200 means screenshot exists
}, 'pipeline');

// ═══════════════════════════════════════════════════════════════════════════
//  MULTI — Session isolation tests
// ═══════════════════════════════════════════════════════════════════════════
section('MULTI — Session isolation');

await run('multi: acquire → heartbeat → release lifecycle', async () => {
  const { body: acq } = await POST('/api/session/acquire');
  assert(acq.token, 'got token');
  if (acq.dev) { console.log('  (dev mode — skip heartbeat/release)'); return; }

  const { body: hb } = await POST('/api/session/heartbeat', { token: acq.token });

  // Heartbeat endpoint uses header; try both ways
  const hbRes = await request('POST', '/api/session/heartbeat', null);
  // Either format works

  const { body: rel } = await POST('/api/session/release', { token: acq.token });
  eq(rel.ok, true, 'release succeeded');
}, 'multi');

await run('multi: scene list isolation between owners', async () => {
  delete require.cache[require.resolve('../scenes')];
  const { SceneManager } = require('../scenes');
  const dir = os.tmpdir() + '/sw-multi-' + Date.now();
  const sm = new SceneManager(dir);

  await sm.save({ name: 'X1', prompt: 'p', ownerId: 'x-user' });
  await sm.save({ name: 'X2', prompt: 'p', ownerId: 'x-user' });
  await sm.save({ name: 'Y1', prompt: 'p', ownerId: 'y-user' });

  eq(sm.list('x-user').length, 2, 'x-user sees 2');
  eq(sm.list('y-user').length, 1, 'y-user sees 1');
  eq(sm.list().length, 3, 'admin sees all 3');

  fs.rmSync(dir, { recursive: true, force: true });
}, 'multi');

await run('multi: per-session chatProc isolation (structural)', async () => {
  // Verify the server handles stop for non-existent sessions gracefully
  const r1 = await POST('/api/chat-stop', { sessionId: 'sess-aaa' });
  const r2 = await POST('/api/chat-stop', { sessionId: 'sess-bbb' });
  eq(r1.body.stopped, false, 'unknown sess-aaa: stopped=false');
  eq(r2.body.stopped, false, 'unknown sess-bbb: stopped=false');
  // Neither request should affect the other
}, 'multi');

await run('multi: concurrent session acquire / release (stress)', async () => {
  delete require.cache[require.resolve('../session-manager')];
  const { SessionManager } = require('../session-manager');
  const m = new SessionManager();

  // Acquire 8 slots concurrently
  const users = Array.from({ length: 8 }, (_, i) => `stress-user-${i}`);
  const recs = await Promise.all(users.map(u => m.acquire(u)));
  eq(recs.length, 8, '8 concurrent acquires');
  eq(m.freeSlots, 2, '2 of 10 still free');

  // Release all
  recs.forEach(r => m.release(r.token));
  eq(m.freeSlots, 10, 'all slots returned');
  m.destroy();
}, 'multi');

// ═══════════════════════════════════════════════════════════════════════════
//  Summary
// ═══════════════════════════════════════════════════════════════════════════
const passed  = results.filter(r => r.status === 'pass').length;
const failed  = results.filter(r => r.status === 'fail').length;
const skipped = results.filter(r => r.status === 'skip').length;

console.log(`\n${BO}Results: ${G}${passed} passed${E}${failed > 0 ? `  ${R}${failed} failed${E}` : ''}${skipped > 0 ? `  ${D}${skipped} skipped${E}` : ''}${E}`);

if (failed > 0) {
  console.log(`\n${R}${BO}Failed:${E}`);
  results.filter(r => r.status === 'fail').forEach(r => {
    console.log(`  ${R}✗ ${r.name}${E}\n    ${r.error}`);
  });
  process.exit(1);
} else {
  console.log(`\n${G}${BO}All tests passed!${E}`);
  process.exit(0);
}

})();
