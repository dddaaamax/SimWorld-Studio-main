"use strict";

const fs = require("fs");
const path = require("path");
const { buildDynamicToolDef, dryRunExpansion, validateToolShape } = require("./learned-tool-runtime");

const DEFAULT_FILE = path.resolve(__dirname, "../../arena_data/learned_tools.json");

function uniq(values) {
  const out = [];
  const seen = new Set();
  for (const v of values || []) {
    const s = String(v || "").trim();
    if (!s || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

class LearnedToolStore {
  constructor(opts = {}) {
    this.filePath = opts.filePath || DEFAULT_FILE;
    this._ensure();
  }

  _ensure() {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
    if (!fs.existsSync(this.filePath)) {
      fs.writeFileSync(this.filePath, "[]\n", "utf-8");
    }
  }

  _read() {
    this._ensure();
    try {
      const raw = fs.readFileSync(this.filePath, "utf-8");
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  _validateEntry(tool) {
    try {
      validateToolShape(tool);
      buildDynamicToolDef(tool);
      dryRunExpansion(tool, tool && tool.defaults ? tool.defaults : {});
      return { ok: true, reason: null };
    } catch (err) {
      return {
        ok: false,
        reason: err && err.message ? String(err.message) : "invalid_tool",
      };
    }
  }

  _partitionValidTools(list) {
    const valid = [];
    const invalid = [];
    for (const tool of Array.isArray(list) ? list : []) {
      const check = this._validateEntry(tool);
      if (check.ok) valid.push(tool);
      else {
        invalid.push({
          id: tool && tool.id ? String(tool.id) : "",
          reason: check.reason || "invalid_tool",
        });
      }
    }
    return { valid, invalid };
  }

  pruneInvalid(opts = {}) {
    const includeArchived = opts.includeArchived !== false;
    const list = this._read();
    const target = includeArchived ? list : list.filter((t) => !t.archived);
    const untouched = includeArchived ? [] : list.filter((t) => t.archived);
    const { valid, invalid } = this._partitionValidTools(target);
    if (invalid.length > 0) {
      this._write([...valid, ...untouched]);
    }
    return {
      checkedCount: target.length,
      removedCount: invalid.length,
      removedToolIds: invalid.map((it) => it.id).filter(Boolean),
      removedToolReasons: invalid,
    };
  }

  _readValidated(opts = {}) {
    const includeArchived = Boolean(opts.includeArchived);
    const report = this.pruneInvalid({ includeArchived: true });
    const list = this._read();
    const filtered = includeArchived ? list : list.filter((t) => !t.archived);
    return {
      list: filtered,
      cleanupReport: report,
    };
  }

  _write(list) {
    this._ensure();
    fs.writeFileSync(this.filePath, `${JSON.stringify(list, null, 2)}\n`, "utf-8");
  }

  list(opts = {}) {
    const includeArchived = Boolean(opts.includeArchived);
    return this._readValidated({ includeArchived }).list;
  }

  get(id) {
    const { list } = this._readValidated({ includeArchived: true });
    return list.find((t) => t.id === id && !t.archived) || null;
  }

  getAny(id) {
    const { list } = this._readValidated({ includeArchived: true });
    return list.find((t) => t.id === id) || null;
  }

  getByMcpName(name) {
    const { list } = this._readValidated({ includeArchived: true });
    return list.find((t) => t.mcpName === name && !t.archived) || null;
  }

  getEnabled() {
    const { list } = this._readValidated({ includeArchived: true });
    return list.filter((t) => !t.archived && t.enabled !== false);
  }

  upsert(tool) {
    const list = this._read();
    const now = new Date().toISOString();
    const idx = list.findIndex((t) => t.id === tool.id);
    const prev = idx >= 0 ? list[idx] : null;
    const cleanPrev = prev ? { ...prev } : null;
    if (cleanPrev) {
      delete cleanPrev.createdAt;
      if (cleanPrev.provenance && typeof cleanPrev.provenance === "object") {
        cleanPrev.provenance = { ...cleanPrev.provenance };
        delete cleanPrev.provenance.createdAt;
      }
    }

    const next = {
      ...(cleanPrev || {}),
      ...tool,
      mcpName: tool.mcpName || (prev && prev.mcpName) || `learned__${tool.id}`,
      enabled: tool.enabled !== false,
      version: Number(tool.version || (prev && prev.version) || 1),
      archived: Boolean(tool.archived),
      metrics: {
        estimatedCallSavings: Number(
          tool.metrics?.estimatedCallSavings != null
            ? tool.metrics.estimatedCallSavings
            : prev?.metrics?.estimatedCallSavings || 0
        ),
        usageCount: Number(tool.metrics?.usageCount != null ? tool.metrics.usageCount : prev?.metrics?.usageCount || 0),
        successCount: Number(
          tool.metrics?.successCount != null ? tool.metrics.successCount : prev?.metrics?.successCount || 0
        ),
      },
      provenance: {
        sessionId: tool.provenance?.sessionId || prev?.provenance?.sessionId || null,
      },
      createdByBatchId: String(tool.createdByBatchId || prev?.createdByBatchId || ""),
      updatedByBatchIds: uniq([...(prev?.updatedByBatchIds || []), ...(tool.updatedByBatchIds || [])]),
      sourceSessionIds: uniq([...(prev?.sourceSessionIds || []), ...(tool.sourceSessionIds || [])]),
      reusedByBatchIds: uniq([...(prev?.reusedByBatchIds || []), ...(tool.reusedByBatchIds || [])]),
      updatedAt: now,
    };

    if (idx >= 0) {
      list[idx] = { ...next, id: list[idx].id };
    } else {
      list.push({ ...next });
    }

    this._write(list);
    return this.get(tool.id);
  }

  patch(id, patch = {}) {
    const list = this._readValidated({ includeArchived: true }).list;
    const idx = list.findIndex((t) => t.id === id && !t.archived);
    if (idx < 0) return null;

    const prev = list[idx];

    list[idx] = {
      ...prev,
      ...patch,
      id: prev.id,
      mcpName: prev.mcpName,
      updatedByBatchIds: uniq([...(prev.updatedByBatchIds || []), ...(patch.updatedByBatchIds || [])]),
      sourceSessionIds: uniq([...(prev.sourceSessionIds || []), ...(patch.sourceSessionIds || [])]),
      reusedByBatchIds: uniq([...(prev.reusedByBatchIds || []), ...(patch.reusedByBatchIds || [])]),
      updatedAt: new Date().toISOString(),
    };
    delete list[idx].createdAt;
    if (list[idx].provenance && typeof list[idx].provenance === "object") {
      list[idx].provenance = { ...list[idx].provenance };
      delete list[idx].provenance.createdAt;
    }

    this._write(list);
    return this.get(id);
  }

  archive(id) {
    return this.patch(id, { archived: true, enabled: false });
  }

  remove(id) {
    const list = this._read();
    const next = list.filter((t) => t.id !== id);
    if (next.length === list.length) return false;
    this._write(next);
    return true;
  }

  recordUsage(id, ok) {
    const list = this._readValidated({ includeArchived: true }).list;
    const idx = list.findIndex((t) => t.id === id && !t.archived);
    if (idx < 0) return null;

    const cur = list[idx];
    cur.metrics = cur.metrics || {
      estimatedCallSavings: 0,
      usageCount: 0,
      successCount: 0,
    };
    cur.metrics.usageCount = Number(cur.metrics.usageCount || 0) + 1;
    if (ok) cur.metrics.successCount = Number(cur.metrics.successCount || 0) + 1;
    cur.updatedAt = new Date().toISOString();

    list[idx] = cur;
    this._write(list);
    return this.get(id);
  }
}

module.exports = {
  LearnedToolStore,
  DEFAULT_LEARNED_TOOLS_FILE: DEFAULT_FILE,
};
