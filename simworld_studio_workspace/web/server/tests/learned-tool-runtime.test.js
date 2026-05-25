'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildDynamicToolDef,
  validateToolShape,
  expandLearnedTool,
  dryRunExpansion,
  signatureHash,
} = require('../learned-tool-runtime');

function validProgramTool(id = 'learned_program_tool') {
  return {
    id,
    mcpName: `learned__${id}`,
    name: 'Program Tool',
    description: 'Reusable program macro',
    defaults: {
      prefix: 'Tree',
      blueprint_id: 'BP_Tree1',
      origin: [0, 0, 0],
      spacing: 1200,
      repeat: 4,
    },
    paramsSchema: {
      type: 'object',
      properties: {
        repeat: { type: 'number' },
      },
    },
    program: [
      {
        primitive: 'spawn_blueprint_actor',
        argumentsTemplate: {
          actor_name: { op: 'concat', values: ['$args.prefix', '_', '$index1'] },
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
  };
}

test('expandLearnedTool expands program into primitive calls', () => {
  const tool = validProgramTool('row_program');
  const steps = expandLearnedTool(tool, { repeat: 4 });
  assert.equal(steps.length, 4);
  assert.equal(steps[0].name, 'spawn_blueprint_actor');
  assert.deepEqual(steps[1].arguments.location, [1200, 0, 0]);
  assert.equal(steps[3].arguments.actor_name, 'Tree_4');
});

test('dryRunExpansion respects max expansion cap via repeat clamp', () => {
  const tool = validProgramTool('massive_program');
  const out = dryRunExpansion(tool, { repeat: 1000 });
  assert.equal(out.ok, true);
  assert.equal(out.stepCount, 200);
});

test('signatureHash is stable for equivalent program shape', () => {
  const a = validProgramTool('a');
  const b = validProgramTool('b');
  b.paramsSchema = { type: 'object', properties: { repeat: { type: 'number' } } };

  assert.equal(signatureHash(a), signatureHash(b));
});

test('validateToolShape rejects malformed paramsSchema', () => {
  const tool = validProgramTool('bad_schema_tool');
  tool.paramsSchema = {
    repeat: { type: 'number' },
  };

  assert.throws(() => validateToolShape(tool), /paramsSchema:type_must_be_object/);
});

test('buildDynamicToolDef rejects malformed paramsSchema', () => {
  const tool = validProgramTool('bad_schema_export');
  tool.paramsSchema = {
    repeat: { type: 'number' },
  };

  assert.throws(() => buildDynamicToolDef(tool), /paramsSchema:type_must_be_object/);
});

test('validateToolShape rejects $op key and unsupported op names', () => {
  const tool = validProgramTool('bad_op_tool');
  tool.program[0].argumentsTemplate.actor_name = { $op: 'concat', values: ['$args.prefix', '_', '$index1'] };
  assert.throws(() => validateToolShape(tool), /operator_key_must_be_op/);

  const tool2 = validProgramTool('bad_op_name_tool');
  tool2.program[0].argumentsTemplate.location = {
    op: 'vec_add',
    values: [
      '$args.origin',
      { op: 'multiply', value: [1, 0, 0], factor: 1 },
    ],
  };
  assert.throws(() => validateToolShape(tool2), /unsupported_op:multiply|invalid_vector_op:multiply/);
});

test('validateToolShape rejects math-expression strings masquerading as refs', () => {
  const tool = validProgramTool('bad_expr_tool');
  tool.program[0].argumentsTemplate.location = '$args.origin + 100';
  assert.throws(() => validateToolShape(tool), /invalid_reference_expression/);
});

test('validateToolShape rejects object-style location vectors for spawn_actor', () => {
  const tool = validProgramTool('bad_location_shape');
  tool.program[0] = {
    primitive: 'spawn_actor',
    argumentsTemplate: {
      name: { op: 'concat', values: ['$args.prefix', '_', '$index1'] },
      static_mesh: '/Game/Trees/Broadleaf_Desktop/Meshes/SM_BroadLeafTree_3',
      location: { x: '$args.origin.0', y: '$args.origin.1', z: '$args.origin.2' },
      scale: [1, 1, 1],
    },
  };
  assert.throws(() => validateToolShape(tool), /vector_object_requires_op_or_ref/);
});

test('dryRunExpansion rejects unresolved required primitive arguments', () => {
  const tool = validProgramTool('unresolved_required_args');
  delete tool.defaults.blueprint_id;
  assert.throws(() => dryRunExpansion(tool, tool.defaults), /dry_run_contract_invalid|expanded_args_invalid/);
});
