'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

function tmpDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function validLearnedTool(id) {
  return {
    id,
    mcpName: `learned__${id}`,
    name: 'Valid Learned Tool',
    description: 'Valid schema + program',
    paramsSchema: {
      type: 'object',
      properties: {
        repeat: { type: 'number' },
      },
      required: [],
    },
    defaults: {
      repeat: 1,
      actor_name_prefix: 'Tree',
      blueprint_id: 'BP_Tree1',
      origin: [0, 0, 0],
      spacing: 1000,
    },
    program: [
      {
        primitive: 'spawn_blueprint_actor',
        argumentsTemplate: {
          actor_name: { op: 'concat', values: ['$args.actor_name_prefix', '_', '$index1'] },
          blueprint_id: '$args.blueprint_id',
          location: '$args.origin',
        },
      },
    ],
    enabled: true,
    archived: false,
    version: 1,
  };
}

function malformedLearnedTool(id) {
  return {
    id,
    mcpName: `learned__${id}`,
    name: 'Malformed Learned Tool',
    description: 'Invalid program contract',
    paramsSchema: {
      type: 'object',
      properties: {
        center_x: { type: 'number' },
      },
      required: ['center_x'],
    },
    defaults: { center_x: 0, center_y: 0, center_z: 0 },
    program: [
      {
        primitive: 'spawn_blueprint_actor',
        argumentsTemplate: {
          actor_name: { $op: 'concat', values: ['Tree_', '$index1'] },
          blueprint_id: 'BP_Tree1',
          location: {
            x: '$args.center_x',
            y: '$args.center_y',
            z: '$args.center_z',
          },
        },
      },
    ],
    enabled: true,
    archived: false,
    version: 1,
  };
}

test('tools/list keeps static tools and excludes malformed learned tools', async () => {
  const root = '/data/jingtian/work/SimWorld-Studio-Internal-main/simworld_studio_workspace';
  const serverPath = path.join(root, 'web/server/mcp-server.js');
  const tmp = tmpDir('mcp-server-test-');
  const learnedFile = path.join(tmp, 'learned_tools.json');

  fs.writeFileSync(
    learnedFile,
    `${JSON.stringify([malformedLearnedTool('bad_schema_tool'), validLearnedTool('good_schema_tool')], null, 2)}\n`,
    'utf-8'
  );

  const proc = spawn('node', [serverPath], {
    env: {
      ...process.env,
      LEARNED_TOOLS_FILE: learnedFile,
      UNREAL_HOST: '127.0.0.1',
      UNREAL_PORT: '65530',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let stdoutBuf = '';
  let stderrBuf = '';
  const responses = new Map();

  proc.stdout.on('data', (chunk) => {
    stdoutBuf += chunk.toString();
    const lines = stdoutBuf.split('\n');
    stdoutBuf = lines.pop() || '';
    for (const line of lines) {
      const text = line.trim();
      if (!text) continue;
      let obj = null;
      try {
        obj = JSON.parse(text);
      } catch {
        continue;
      }
      if (obj && Object.prototype.hasOwnProperty.call(obj, 'id')) {
        responses.set(obj.id, obj);
      }
    }
  });

  proc.stderr.on('data', (chunk) => {
    stderrBuf += chunk.toString();
  });

  const send = (payload) => {
    proc.stdin.write(`${JSON.stringify(payload)}\n`);
  };

  send({
    jsonrpc: '2.0',
    id: 0,
    method: 'initialize',
    params: {
      protocolVersion: '2025-11-25',
      capabilities: {},
      clientInfo: { name: 'test-client', version: '1.0.0' },
    },
  });
  send({ jsonrpc: '2.0', method: 'notifications/initialized' });
  send({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} });

  const start = Date.now();
  while (!responses.has(1) && Date.now() - start < 4000) {
    await new Promise((r) => setTimeout(r, 20));
  }

  try {
    assert.ok(responses.has(1), `Expected tools/list response. stderr=${stderrBuf}`);
    const listResp = responses.get(1);
    const tools = (listResp && listResp.result && Array.isArray(listResp.result.tools))
      ? listResp.result.tools
      : [];
    const names = tools.map((t) => String(t.name || ''));

    assert.equal(names.includes('delete_all_spawned'), true);
    assert.equal(names.includes('spawn_blueprint_actor'), true);
    assert.equal(names.includes('learned__bad_schema_tool'), false);
    assert.equal(names.includes('learned__good_schema_tool'), true);

    const after = JSON.parse(fs.readFileSync(learnedFile, 'utf-8'));
    assert.equal(Array.isArray(after), true);
    const ids = after.map((t) => t.id);
    assert.equal(ids.includes('bad_schema_tool'), false);
    assert.equal(ids.includes('good_schema_tool'), true);
  } finally {
    proc.kill('SIGTERM');
  }
});
