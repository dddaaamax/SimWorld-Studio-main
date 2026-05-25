'use strict';

const { spawn } = require('child_process');
const path = require('path');

const NL = String.fromCharCode(10);

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function trimText(text, maxLen) {
  const src = String(text || '');
  if (src.length <= maxLen) return src;
  return `${src.slice(0, maxLen)}${NL}...`;
}

function uniqueSkillIds(values, allowedIds) {
  const out = [];
  const seen = new Set();

  for (const value of values || []) {
    const id = String(value || '').trim();
    if (!id || seen.has(id)) continue;
    if (allowedIds && !allowedIds.has(id)) continue;
    seen.add(id);
    out.push(id);
  }

  return out;
}

function extractCandidateJson(text) {
  const cleaned = String(text || '').trim();
  if (!cleaned) return null;

  const direct = safeJsonParse(cleaned);
  if (direct && typeof direct === 'object') return direct;

  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start >= 0 && end > start) {
    const maybe = safeJsonParse(cleaned.slice(start, end + 1));
    if (maybe && typeof maybe === 'object') return maybe;
  }

  const fenceMatches = [...cleaned.matchAll(/```(?:json)?\s*([\s\S]*?)\s*```/gi)];
  for (const match of fenceMatches) {
    const maybe = safeJsonParse(String(match[1] || '').trim());
    if (maybe && typeof maybe === 'object') return maybe;
  }

  const objects = [];
  let depth = 0;
  let inString = false;
  let escape = false;
  let objStart = -1;

  for (let i = 0; i < cleaned.length; i += 1) {
    const ch = cleaned[i];

    if (inString) {
      if (escape) {
        escape = false;
        continue;
      }
      if (ch === '\\') {
        escape = true;
        continue;
      }
      if (ch === '"') inString = false;
      continue;
    }

    if (ch === '"') {
      inString = true;
      continue;
    }

    if (ch === '{') {
      if (depth === 0) objStart = i;
      depth += 1;
      continue;
    }

    if (ch === '}') {
      if (depth > 0) depth -= 1;
      if (depth === 0 && objStart >= 0) {
        objects.push(cleaned.slice(objStart, i + 1));
        objStart = -1;
      }
    }
  }

  for (let i = objects.length - 1; i >= 0; i -= 1) {
    const maybe = safeJsonParse(objects[i]);
    if (maybe && typeof maybe === 'object') return maybe;
  }

  return null;
}

function buildSelectorPrompt(userPrompt, availableSkills) {
  return [
    'You are a skill-routing assistant for a scene generation coding agent.',
    'Read every available skill and pick the smallest useful set for the user request.',
    'Return STRICT JSON only with this exact schema:',
    '{',
    '  "selectedSkillIds": ["skill_id"],',
    '  "reasoning": "short reason"',
    '}',
    '',
    'Rules:',
    '- Use only IDs from the provided skills list.',
    '- Prefer minimal coverage; avoid unnecessary skills.',
    '- If none are needed, return an empty array.',
    '- Do not include markdown fences or extra text.',
    '',
    `User request: ${String(userPrompt || '').trim()}`,
    '',
    'Available skills:',
    JSON.stringify(availableSkills, null, 2),
  ].join(NL);
}

async function selectSkillsWithClaude(options) {
  const opts = options || {};
  const prompt = String(opts.prompt || '').trim();
  const registry = opts.skillRegistry;
  const claudeBin = String(opts.claudeBin || process.env.CLAUDE_BIN || 'claude');
  const timeoutMs = Math.max(5000, Number(opts.timeoutMs || 60000));
  const model =
    opts.model == null || opts.model === ''
      ? null
      : String(opts.model);

  if (!registry || typeof registry.list !== 'function' || typeof registry.get !== 'function') {
    throw new Error('Skill registry is unavailable');
  }

  if (!prompt) {
    return { selectedSkillIds: [], reasoning: 'empty_prompt', rawText: '' };
  }

  const metas = registry.list();
  const availableSkills = [];
  const allowedIds = new Set();

  for (const meta of metas) {
    const full = registry.get(meta.id) || meta;
    const id = String((full && full.id) || '').trim();
    if (!id) continue;
    allowedIds.add(id);
    availableSkills.push({
      id,
      name: String((full && full.name) || id),
      description: String((full && full.description) || ''),
      tags: Array.isArray(full && full.tags) ? full.tags : [],
      dependencies: Array.isArray(full && full.dependencies) ? full.dependencies : [],
      source: String((full && full.source) || ''),
      content: trimText(full && full.content, 1600),
    });
  }

  if (availableSkills.length === 0) {
    return { selectedSkillIds: [], reasoning: 'no_skills_available', rawText: '' };
  }

  const selectorPrompt = buildSelectorPrompt(prompt, availableSkills);
  const args = [
    '-p',
    selectorPrompt,
    '--output-format',
    'stream-json',
    '--include-partial-messages',
    '--verbose',
    '--dangerously-skip-permissions',
  ];
  if (model) args.push('--model', model);

  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    delete env.CLAUDECODE;
    delete env.CLAUDE_SESSION_ID;
    delete env.CLAUDE_CODE_ENTRYPOINT;

    const proc = spawn(claudeBin, args, {
      cwd: path.resolve(__dirname, '..'),
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let stdoutBuffer = '';
    let stderrBuffer = '';
    let assistantText = '';
    let resultText = '';
    let resultIsError = false;

    const timeout = setTimeout(() => {
      proc.kill('SIGTERM');
      reject(new Error('Skill selector timed out'));
    }, timeoutMs);

    function handleLine(line) {
      const evt = safeJsonParse(String(line || '').trim());
      if (!evt) return;

      if (evt.type === 'assistant') {
        const blocks = evt.message && Array.isArray(evt.message.content) ? evt.message.content : [];
        for (const block of blocks) {
          if (block.type === 'text' && block.text) assistantText += String(block.text);
        }
        return;
      }

      if (evt.type === 'result') {
        resultIsError = Boolean(evt.is_error || evt.subtype === 'error_during_turn');
        if (typeof evt.result === 'string' && evt.result) resultText += evt.result;
      }
    }

    proc.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString();
      const lines = stdoutBuffer.split(NL);
      stdoutBuffer = lines.pop() || '';
      for (const line of lines) {
        if (line.trim()) handleLine(line);
      }
    });

    proc.stderr.on('data', (chunk) => {
      stderrBuffer += chunk.toString();
    });

    proc.on('error', (err) => {
      clearTimeout(timeout);
      reject(err);
    });

    proc.on('close', (code) => {
      clearTimeout(timeout);
      if (stdoutBuffer.trim()) handleLine(stdoutBuffer);

      const rawText = `${resultText}${NL}${assistantText}`.trim();
      const parsed = extractCandidateJson(rawText);
      const rawIds =
        (parsed && parsed.selectedSkillIds) ||
        (parsed && parsed.skillIds) ||
        (parsed && parsed.skills) ||
        [];
      const selectedSkillIds = uniqueSkillIds(rawIds, allowedIds);
      const reasoning =
        parsed && typeof parsed.reasoning === 'string' ? parsed.reasoning.trim() : '';

      if (!parsed) {
        return reject(new Error('Skill selector returned non-JSON output'));
      }
      if (resultIsError || code !== 0) {
        return reject(new Error(`Skill selector exited with code ${code}: ${stderrBuffer.slice(0, 240)}`));
      }

      resolve({
        selectedSkillIds,
        reasoning,
        rawText,
      });
    });
  });
}

module.exports = { selectSkillsWithClaude };
