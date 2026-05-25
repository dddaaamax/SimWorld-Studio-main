'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { LearnedToolStore } = require('../learned-tools-store');

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function validTool(id = 'valid_tool') {
  return {
    id,
    mcpName: `learned__${id}`,
    name: 'Valid Tool',
    description: 'Valid learned tool',
    paramsSchema: {
      type: 'object',
      properties: {
        repeat: { type: 'number' },
      },
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
          location: {
            op: 'vec_add',
            values: [
              '$args.origin',
              { op: 'vec_scale', value: [1, 0, 0], factor: { op: 'mul', values: ['$index', '$args.spacing'] } },
            ],
          },
        },
      },
    ],
    enabled: true,
    archived: false,
    version: 1,
  };
}

function malformedTool(id = 'malformed_tool') {
  return {
    id,
    mcpName: `learned__${id}`,
    name: 'Malformed Tool',
    description: 'Invalid learned tool',
    paramsSchema: {
      type: 'object',
      properties: {},
    },
    defaults: {},
    program: [
      {
        primitive: 'spawn_blueprint_actor',
        argumentsTemplate: {
          actor_name: { $op: 'concat', values: ['Tree_', '$index1'] },
          blueprint_id: 'BP_Tree1',
          location: { x: 0, y: 0, z: 0 },
        },
      },
    ],
    enabled: true,
    archived: false,
    version: 1,
  };
}

test('store accessors auto-prune invalid learned tools', () => {
  const root = tmp('learned-tool-store-');
  const filePath = path.join(root, 'learned_tools.json');
  fs.writeFileSync(filePath, `${JSON.stringify([malformedTool('bad_tool'), validTool('good_tool')], null, 2)}\n`, 'utf-8');

  const store = new LearnedToolStore({ filePath });
  const listed = store.list();

  assert.equal(listed.length, 1);
  assert.equal(listed[0].id, 'good_tool');
  assert.equal(store.get('bad_tool'), null);
  assert.ok(store.get('good_tool'));
  assert.equal(store.getEnabled().length, 1);
  assert.equal(store.getEnabled()[0].id, 'good_tool');

  const persisted = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  assert.equal(Array.isArray(persisted), true);
  assert.equal(persisted.length, 1);
  assert.equal(persisted[0].id, 'good_tool');
});
