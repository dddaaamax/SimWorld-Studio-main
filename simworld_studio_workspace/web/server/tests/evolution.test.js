'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { LearnedToolStore } = require('../learned-tools-store');
const {
  EvolutionManager,
  extractToolTraceFromStreamJson,
  extractSessionModelFromStreamJson,
} = require('../evolution');

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function parseMeta(raw) {
  const m = String(raw || '').match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!m) return null;
  const lines = m[1].split('\n');
  const meta = {};
  for (const line of lines) {
    const kv = line.match(/^(\w+):\s*(.*)$/);
    if (!kv) continue;
    const key = kv[1];
    const value = kv[2].trim();
    if (value.startsWith('[') && value.endsWith(']')) {
      meta[key] = value
        .slice(1, -1)
        .split(',')
        .map((v) => v.trim())
        .filter(Boolean)
        .map((v) => v.replace(/^['"]|['"]$/g, ''));
    } else {
      meta[key] = value.replace(/^['"]|['"]$/g, '');
    }
  }
  return { meta, content: m[2].trim() };
}

function mkSkillRegistryStub(skillsDir) {
  const map = new Map();

  function loadAll() {
    map.clear();
    if (!fs.existsSync(skillsDir)) return;

    for (const entry of fs.readdirSync(skillsDir)) {
      if (!entry.endsWith('.md')) continue;
      const filePath = path.join(skillsDir, entry);
      const parsed = parseMeta(fs.readFileSync(filePath, 'utf8'));
      if (!parsed) continue;
      const meta = parsed.meta || {};
      const id = meta.id || path.basename(entry, '.md');
      map.set(id, {
        id,
        name: meta.name || id,
        version: meta.version || '1.0.0',
        author: meta.author || 'evolution-engine',
        tags: Array.isArray(meta.tags) ? meta.tags : [],
        dependencies: Array.isArray(meta.dependencies) ? meta.dependencies : [],
        imports: Array.isArray(meta.imports) ? meta.imports : [],
        description: meta.description || '',
        content: parsed.content || '',
        filePath,
        source: 'custom',
      });
    }
  }

  loadAll();

  return {
    list() {
      return [...map.values()].map(({ content, ...rest }) => rest);
    },
    get(id) {
      return map.get(id) || null;
    },
    reload() {
      loadAll();
    },
    retrieveForPrompt() {
      return [];
    },
  };
}

function makeSession(id, prompt, trace = []) {
  return {
    id,
    source: 'chat',
    prompt,
    model: 'claude-sonnet-4-6',
    skills: [],
    toolTrace: trace,
    result: { isError: false, screenshot: null },
    createdAt: new Date().toISOString(),
    state: 'pending',
    batchId: null,
    archivedAt: null,
    archiveReason: null,
  };
}

function makeManager(extra = {}) {
  const arenaDataDir = tmp('evo-arena-');
  const skillsDir = tmp('evo-skills-');
  const logsDir = tmp('evo-logs-');
  const store = new LearnedToolStore({ filePath: path.join(arenaDataDir, 'learned_tools.json') });
  const registry = mkSkillRegistryStub(skillsDir);

  const mgr = new EvolutionManager({
    skillRegistry: registry,
    learnedToolStore: store,
    arenaDataDir,
    skillsDir,
    logsDir,
    autoSweepEnabled: false,
    ...extra,
  });

  return { mgr, store, registry, arenaDataDir, skillsDir, logsDir };
}

function writeLearnedSkillFile(skillsDir, id, content) {
  const text = [
    '---',
    `id: "${id}"`,
    `name: "${id}"`,
    'version: "1.0.0"',
    'author: "evolution-engine"',
    'tags: ["learned"]',
    'dependencies: []',
    `description: "${id}"`,
    '---',
    '',
    String(content || ''),
    '',
  ].join('\n');
  fs.writeFileSync(path.join(skillsDir, `${id}.md`), text, 'utf-8');
}

function validProgramTool(id = 'program_tool_new') {
  return {
    id,
    name: 'Program Tool',
    description: 'Reusable program tool',
    paramsSchema: {
      type: 'object',
      properties: {
        repeat: { type: 'number' },
        origin: { type: 'array' },
        spacing: { type: 'number' },
      },
    },
    defaults: {
      repeat: 4,
      origin: [0, 0, 0],
      spacing: 1000,
      blueprint_id: 'BP_Tree1',
      actor_name_prefix: 'Tree',
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
  };
}

test('extractToolTraceFromStreamJson parses tool calls and results', () => {
  const lines = [
    JSON.stringify({
      type: 'stream_event',
      event: {
        type: 'content_block_start',
        content_block: {
          type: 'tool_use',
          id: 'tool-1',
          name: 'mcp__simworld__spawn_blueprint_actor',
        },
      },
    }),
    JSON.stringify({
      type: 'assistant',
      message: {
        content: [
          {
            type: 'tool_use',
            id: 'tool-1',
            name: 'mcp__simworld__spawn_blueprint_actor',
            input: { actor_name: 'Tree_1', blueprint_id: 'BP_Tree1', location: [0, 0, 0] },
          },
        ],
      },
    }),
    JSON.stringify({
      type: 'user',
      message: {
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'tool-1',
            content: [{ text: '{"status":"success"}' }],
            is_error: false,
          },
        ],
      },
    }),
  ].join('\n');

  const trace = extractToolTraceFromStreamJson(lines);
  assert.equal(trace.length, 1);
  assert.equal(trace[0].name, 'spawn_blueprint_actor');
  assert.equal(trace[0].ok, true);
});

test('extractSessionModelFromStreamJson parses init model', () => {
  const raw = [
    JSON.stringify({ type: 'system', subtype: 'init', model: 'claude-sonnet-4-6' }),
    JSON.stringify({ type: 'result', subtype: 'success', is_error: false }),
  ].join('\n');

  const model = extractSessionModelFromStreamJson(raw);
  assert.equal(model, 'claude-sonnet-4-6');
});

test('batch min pending defaults to 1', () => {
  const { mgr } = makeManager();
  assert.equal(mgr.batchMinPending, 1);
  assert.equal(mgr.autoSweepEnabled, false);
});

test('create-only pipeline creates tool and linked skill and archives when all subparts resolved', async () => {
  const { mgr, store } = makeManager();

  mgr._saveSessionArtifact(makeSession('s1', 'place trees in a row around a road'));
  mgr._decomposePrompt = async () => ({
    summary: 'row arrangement',
    subparts: [{ id: 'sp1', text: 'arrange trees in a row' }],
    debug: { mocked: true },
  });
  mgr._decideSubpart = async () => ({ action: 'construct', reason: 'no matching tool', matchToolId: '', debug: { mocked: true } });
  mgr._constructGeneralTool = async () => ({ candidateTool: validProgramTool('trees_program_arrangement'), debug: { mocked: true } });

  const out = await mgr._runBatch({ type: 'batch_check', trigger: 'test' });

  assert.equal(out.status, 'completed');
  assert.equal(out.createdToolIds.length, 1);
  assert.equal(out.createdSkillIds.length, 1);
  assert.equal(out.archivedSessionIds.length, 1);

  const createdTool = store.get('trees_program_arrangement');
  assert.ok(createdTool);
  assert.ok(Array.isArray(createdTool.program));
});

test('match path reuses existing dynamic tool and archives session', async () => {
  const { mgr, store } = makeManager();

  store.upsert({
    ...validProgramTool('existing_program_tool'),
    mcpName: 'learned__existing_program_tool',
    enabled: true,
    version: 1,
  });

  mgr._saveSessionArtifact(makeSession('s1', 'reuse known placement pattern'));
  mgr._decomposePrompt = async () => ({
    summary: 'reuse',
    subparts: [{ id: 'sp1', text: 'arrange trees in a row' }],
    debug: { mocked: true },
  });
  mgr._decideSubpart = async () => ({
    action: 'match',
    reason: 'covered by existing tool',
    matchToolId: 'existing_program_tool',
    debug: { mocked: true },
  });

  const out = await mgr._runBatch({ type: 'batch_check', trigger: 'test' });

  assert.equal(out.status, 'completed');
  assert.equal(out.createdToolIds.length, 0);
  assert.equal(out.reusedToolIds.includes('existing_program_tool'), true);
  assert.equal(out.archivedSessionIds.includes('s1'), true);
});

test('repair loop retries exactly 2 times then marks session failed_unresolved when still invalid', async () => {
  const { mgr, store } = makeManager({ repairBudget: 2 });

  mgr._saveSessionArtifact(makeSession('s1', 'attempt invalid construction'));
  mgr._decomposePrompt = async () => ({
    summary: 'invalid',
    subparts: [{ id: 'sp1', text: 'build reusable pattern' }],
    debug: { mocked: true },
  });
  mgr._decideSubpart = async () => ({ action: 'construct', reason: 'construct', matchToolId: '', debug: { mocked: true } });
  mgr._constructGeneralTool = async () => ({ candidateTool: { id: 'bad_tool', paramsSchema: {}, defaults: {} }, debug: { mocked: true } });

  let repairCalls = 0;
  mgr._repairTool = async () => {
    repairCalls += 1;
    return { candidateTool: { id: 'still_bad_tool', paramsSchema: {}, defaults: {} }, debug: { mocked: true } };
  };

  const out = await mgr._runBatch({ type: 'batch_check', trigger: 'test' });
  const session = mgr._loadSessionArtifact('s1');

  assert.equal(repairCalls, 2);
  assert.equal(out.createdToolIds.length, 0);
  assert.equal(out.deferredSessionIds.includes('s1'), false);
  assert.equal(session.state, 'failed_unresolved');
  assert.equal(mgr.listPending().some((s) => s.id === 's1'), false);
  assert.equal(store.list().length, 0);
});

test('_validateCreateTool rejects malformed paramsSchema and allows repair flow to handle it', () => {
  const { mgr } = makeManager();

  const out = mgr._validateCreateTool({
    id: 'bad_params_schema_tool',
    name: 'Bad Params Schema Tool',
    description: 'Malformed paramsSchema shape',
    paramsSchema: {
      repeat: { type: 'number' },
    },
    defaults: { repeat: 2, blueprint_id: 'BP_Tree1', origin: [0, 0, 0], spacing: 1000, actor_name_prefix: 'Tree' },
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
  });

  assert.equal(out.ok, false);
  assert.match(String(out.reason || ''), /params_schema_invalid/);
});

test('_validateCreateTool rejects malformed primitive argument contracts', () => {
  const { mgr } = makeManager();

  const out = mgr._validateCreateTool({
    id: 'bad_contract_tool',
    name: 'Bad Contract Tool',
    description: 'Uses malformed arguments template',
    paramsSchema: {
      type: 'object',
      properties: {
        center_x: { type: 'number' },
        center_y: { type: 'number' },
      },
      required: ['center_x', 'center_y'],
    },
    defaults: {
      center_x: 0,
      center_y: 0,
      center_z: 0,
      tree_asset: '/Game/Trees/Broadleaf_Desktop/Meshes/SM_BroadLeafTree_3',
    },
    program: [
      {
        primitive: 'spawn_actor',
        argumentsTemplate: {
          name: { $op: 'concat', values: ['tree_', '$index1'] },
          static_mesh: '$args.tree_asset',
          location: { x: '$args.center_x', y: '$args.center_y', z: '$args.center_z' },
        },
      },
    ],
  });

  assert.equal(out.ok, false);
  assert.match(String(out.reason || ''), /schema:program_invalid/);
});

test('linked skills are created only for newly created tools', async () => {
  const { mgr, store } = makeManager();

  store.upsert({
    ...validProgramTool('existing_program_tool'),
    mcpName: 'learned__existing_program_tool',
    enabled: true,
    version: 1,
  });

  mgr._saveSessionArtifact(makeSession('s1', 'reuse one and create one'));
  mgr._decomposePrompt = async () => ({
    summary: 'two parts',
    subparts: [
      { id: 'a', text: 'arrange first group' },
      { id: 'b', text: 'arrange second group with larger spacing' },
    ],
    debug: { mocked: true },
  });
  mgr._decideSubpart = async (session, subpart) => {
    if (subpart.id === 'a') {
      return { action: 'match', reason: 'existing', matchToolId: 'existing_program_tool', debug: { mocked: true } };
    }
    return { action: 'construct', reason: 'new variant', matchToolId: '', debug: { mocked: true } };
  };
  mgr._constructGeneralTool = async () => ({ candidateTool: validProgramTool('new_program_tool_variant'), debug: { mocked: true } });

  const out = await mgr._runBatch({ type: 'batch_check', trigger: 'test' });

  assert.equal(out.reusedToolIds.includes('existing_program_tool'), true);
  assert.equal(out.createdToolIds.includes('new_program_tool_variant'), true);
  assert.equal(out.createdSkillIds.length, 1);
  assert.equal(out.createdSkillIds[0], 'new_program_tool_variant_skill');
});

test('post-persist invalid tool is rolled back and does not create linked skill', async () => {
  const { mgr, store } = makeManager();

  mgr._saveSessionArtifact(makeSession('s1', 'create but fail persisted check'));
  mgr._decomposePrompt = async () => ({
    summary: 'one part',
    subparts: [{ id: 'sp1', text: 'arrange trees in a row' }],
    debug: { mocked: true },
  });
  mgr._decideSubpart = async () => ({ action: 'construct', reason: 'new', matchToolId: '', debug: { mocked: true } });
  mgr._constructGeneralTool = async () => ({ candidateTool: validProgramTool('persist_fail_tool'), debug: { mocked: true } });
  mgr._validatePersistedTool = () => ({ ok: false, reason: 'post_persist_schema:forced_failure' });

  const out = await mgr._runBatch({ type: 'batch_check', trigger: 'test' });
  const session = mgr._loadSessionArtifact('s1');

  assert.equal(out.createdToolIds.length, 0);
  assert.equal(out.createdSkillIds.length, 0);
  assert.equal(out.deferredSessionIds.includes('s1'), false);
  assert.equal(session.state, 'failed_unresolved');
  assert.equal(store.get('persist_fail_tool'), null);
});

test('reprocess is disabled in strict one-pass mode and does not enqueue work', () => {
  const { mgr } = makeManager();
  const beforeQueue = mgr.queue.length;
  const out = mgr.reprocess({ sessionId: 's1' });

  assert.deepEqual(out, {
    sessionIds: [],
    disabled: true,
    reason: 'strict_one_pass_mode',
  });
  assert.equal(mgr.queue.length, beforeQueue);
});

test('queueFromPrompt starts async processing without blocking caller', async () => {
  const { mgr } = makeManager();

  let runCalls = 0;
  mgr._runBatch = async () => {
    runCalls += 1;
    await new Promise((r) => setTimeout(r, 60));
    return mgr._buildSkippedBatchResult('mocked', [], []);
  };

  const start = Date.now();
  const session = mgr.queueFromPrompt({ prompt: 'build a plaza', skills: [] });
  const elapsed = Date.now() - start;

  assert.ok(session && session.id);
  assert.ok(elapsed < 40);

  await new Promise((r) => setTimeout(r, 120));
  assert.equal(runCalls, 1);
});

test('update/self-modify paths are removed from EvolutionManager', () => {
  const { mgr } = makeManager();

  assert.equal(typeof mgr._updateSkill, 'undefined');
  assert.equal(typeof mgr._updateTool, 'undefined');
  assert.equal(typeof mgr._generateBatchDecisionLLM, 'undefined');
  assert.equal(typeof mgr._alignDecisionSkillToolLinks, 'undefined');
});

test('prompt contracts are generalized and contain no template lock wording', async () => {
  const { mgr } = makeManager();
  const prompts = [];
  mgr._invokeClaudeJson = async (systemPrompt) => {
    prompts.push(systemPrompt);
    return { parsed: {}, debug: { mocked: true } };
  };

  await mgr._decomposePrompt('prompt', [], null);
  await mgr._decideSubpart({ prompt: 'prompt' }, { id: 'sp', text: 'x' }, [], [], null);
  await mgr._constructGeneralTool({ prompt: 'prompt' }, { id: 'sp', text: 'x' }, [], null);
  await mgr._repairTool({}, 'error', { prompt: 'prompt' }, { id: 'sp', text: 'x' }, [], null);

  const joined = prompts.join('\n').toLowerCase();
  assert.equal(joined.includes('spawn_row|spawn_grid|spawn_ring|spawn_polygon'), false);
  assert.equal(joined.includes('template must be one of'), false);
  assert.equal(joined.includes('program'), true);
  assert.equal(joined.includes('1 to 3 subparts'), true);
  assert.equal(joined.includes('spawn_actor: name, static_mesh, location'), true);
  assert.equal(joined.includes('spawn_blueprint_actor: actor_name, blueprint_id, location'), true);
  assert.equal(joined.includes('use only supported ops with key "op"'), true);
});

test('startup cleanup removes invalid learned tools and keeps valid ones', () => {
  const { arenaDataDir, skillsDir, logsDir } = makeManager();
  const filePath = path.join(arenaDataDir, 'learned_tools.json');
  const malformed = {
    id: 'bad_schema_tool',
    mcpName: 'learned__bad_schema_tool',
    name: 'Bad Schema Tool',
    description: 'invalid params schema',
    paramsSchema: { repeat: { type: 'number' } },
    defaults: { repeat: 2 },
    program: [
      {
        primitive: 'spawn_blueprint_actor',
        argumentsTemplate: {
          actor_name: { op: 'concat', values: ['Tree_', '$index1'] },
          blueprint_id: 'BP_Tree1',
          location: [0, 0, 0],
        },
      },
    ],
    enabled: true,
    version: 1,
    archived: false,
  };
  const valid = {
    ...validProgramTool('good_schema_tool'),
    mcpName: 'learned__good_schema_tool',
    enabled: true,
    version: 1,
    archived: false,
  };
  fs.writeFileSync(filePath, `${JSON.stringify([malformed, valid], null, 2)}\n`, 'utf-8');
  writeLearnedSkillFile(skillsDir, 'bad_schema_tool_skill', 'Use `learned__bad_schema_tool` for this pattern.');
  writeLearnedSkillFile(skillsDir, 'bad_helper_skill', 'Combine `learned__bad_schema_tool` with defaults.');
  writeLearnedSkillFile(skillsDir, 'mixed_skill', 'Use `learned__bad_schema_tool` and `learned__good_schema_tool` together.');
  writeLearnedSkillFile(skillsDir, 'good_schema_tool_skill', 'Use `learned__good_schema_tool` for this pattern.');

  const store = new LearnedToolStore({ filePath });
  const registry = mkSkillRegistryStub(skillsDir);
  const mgr2 = new EvolutionManager({
    skillRegistry: registry,
    learnedToolStore: store,
    arenaDataDir,
    skillsDir,
    logsDir,
    autoSweepEnabled: false,
  });

  assert.equal(store.get('bad_schema_tool'), null);
  assert.ok(store.get('good_schema_tool'));
  assert.equal(mgr2._activeLearnedToolsFull().length, 1);
  assert.equal(mgr2._activeLearnedToolsFull()[0].id, 'good_schema_tool');
  assert.equal(mgr2.cleanupReport.removedCount, 1);
  assert.equal(mgr2.cleanupReport.removedToolIds.includes('bad_schema_tool'), true);
  assert.equal(mgr2.cleanupReport.removedSkillIds.includes('bad_schema_tool_skill'), true);
  assert.equal(mgr2.cleanupReport.removedSkillIds.includes('bad_helper_skill'), true);
  assert.equal(mgr2.cleanupReport.removedSkillIds.includes('mixed_skill'), false);
  assert.equal(mgr2.cleanupReport.removedSkillIds.includes('good_schema_tool_skill'), false);
  assert.equal(fs.existsSync(path.join(skillsDir, 'bad_schema_tool_skill.md')), false);
  assert.equal(fs.existsSync(path.join(skillsDir, 'bad_helper_skill.md')), false);
  assert.equal(fs.existsSync(path.join(skillsDir, 'mixed_skill.md')), true);
  assert.equal(fs.existsSync(path.join(skillsDir, 'good_schema_tool_skill.md')), true);
});
