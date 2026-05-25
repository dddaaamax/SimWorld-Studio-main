'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { SkillRegistry } = require('../skills');

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function writeSkill(dir, id, sourceLabel) {
  const body = [
    '---',
    `id: ${id}`,
    `name: ${id}`,
    'version: 1.0.0',
    'author: test',
    'tags: [learned]',
    'dependencies: []',
    `description: ${sourceLabel}`,
    '---',
    '',
    `# ${id}`,
    `${sourceLabel} content`,
    '',
  ].join('\n');
  fs.writeFileSync(path.join(dir, `${id}.md`), body, 'utf-8');
}

test('SkillRegistry supports custom builtin/custom directories', () => {
  const builtinDir = tmp('skills-builtin-');
  const customDir = tmp('skills-custom-');

  writeSkill(builtinDir, 'builtin_only', 'builtin source');
  writeSkill(customDir, 'custom_only', 'custom source');

  const registry = new SkillRegistry({ builtinDir, customDir });
  const ids = registry.list().map((s) => s.id).sort();

  assert.deepEqual(ids, ['builtin_only', 'custom_only']);
  assert.equal(registry.get('builtin_only').source, 'builtin');
  assert.equal(registry.get('custom_only').source, 'custom');
});
