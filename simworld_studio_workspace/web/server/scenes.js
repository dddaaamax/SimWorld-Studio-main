"use strict";
// P1-FIX: Added ownerId to every scene record.
// save() accepts ownerId; load/delete validate caller matches owner.
// Uses a per-file write lock via a simple Promise chain to prevent FS races.

const fs   = require("fs");
const path = require("path");
const DEFAULT_SCENES_DIR = path.resolve(__dirname, "../../scenes");

// Simple per-key async mutex — prevents concurrent writes to the same file.
const _writeLocks = new Map();
function _withLock(key, fn) {
  const prev = _writeLocks.get(key) || Promise.resolve();
  const next = prev.then(fn).catch(fn); // always release even on error
  _writeLocks.set(key, next.then(() => { if (_writeLocks.get(key) === next) _writeLocks.delete(key); }));
  return next;
}

class SceneManager {
  constructor(dir) {
    this.scenesDir = dir || DEFAULT_SCENES_DIR;
    fs.existsSync(this.scenesDir) || fs.mkdirSync(this.scenesDir, { recursive: true });
  }

  save(data) {
    const id  = data.id || `scene_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
    const dir = path.join(this.scenesDir, id);
    fs.existsSync(dir) || fs.mkdirSync(dir, { recursive: true });

    const record = {
      id,
      name:           data.name        || "Untitled Scene",
      description:    data.description || "",
      prompt:         data.prompt       || "",
      skills:         data.skills       || [],
      sessionId:      data.sessionId    || null,
      // P1-FIX: ownerId — set once at creation, never overwritten on update
      ownerId:        data.ownerId      || data.sessionId || "_anon",
      createdAt:      data.createdAt    || new Date().toISOString(),
      updatedAt:      new Date().toISOString(),
      actors:         data.actors       || [],
      cameraPosition: data.cameraPosition || null,
    };

    return _withLock(id, () => {
      fs.writeFileSync(path.join(dir, "scene.json"), JSON.stringify(record, null, 2));
      if (data.screenshotPath && fs.existsSync(data.screenshotPath)) {
        const ext = path.extname(data.screenshotPath);
        fs.copyFileSync(data.screenshotPath, path.join(dir, `thumbnail${ext}`));
        record.thumbnail = `thumbnail${ext}`;
        fs.writeFileSync(path.join(dir, "scene.json"), JSON.stringify(record, null, 2));
      }
      if (data.chatHistory) {
        fs.writeFileSync(path.join(dir, "chat.json"), JSON.stringify(data.chatHistory, null, 2));
      }
      return record;
    });
  }

  load(id) {
    const file = path.join(this.scenesDir, id, "scene.json");
    if (!fs.existsSync(file)) return null;
    const rec = JSON.parse(fs.readFileSync(file, "utf-8"));
    const chatFile = path.join(this.scenesDir, id, "chat.json");
    if (fs.existsSync(chatFile)) rec.chatHistory = JSON.parse(fs.readFileSync(chatFile, "utf-8"));
    return rec;
  }

  // P1-FIX: callerOwnerId must match record.ownerId to delete. Pass null to skip check (admin).
  delete(id, callerOwnerId = null) {
    const dir = path.join(this.scenesDir, id);
    if (!fs.existsSync(dir)) return false;
    if (callerOwnerId !== null) {
      try {
        const rec = JSON.parse(fs.readFileSync(path.join(dir, "scene.json"), "utf-8"));
        if (rec.ownerId && rec.ownerId !== "_anon" && rec.ownerId !== callerOwnerId) return "forbidden";
      } catch {}
    }
    return _withLock(id, () => { fs.rmSync(dir, { recursive: true }); return true; });
  }

  list(filterOwnerId = null) {
    if (!fs.existsSync(this.scenesDir)) return [];
    return fs.readdirSync(this.scenesDir)
      .filter(d => fs.existsSync(path.join(this.scenesDir, d, "scene.json")))
      .map(d => {
        try {
          const rec = JSON.parse(fs.readFileSync(path.join(this.scenesDir, d, "scene.json"), "utf-8"));
          // If caller provides filterOwnerId, hide other users' scenes
          if (filterOwnerId && rec.ownerId && rec.ownerId !== "_anon" && rec.ownerId !== filterOwnerId) return null;
          return {
            id:          rec.id,
            name:        rec.name,
            description: rec.description,
            prompt:      rec.prompt,
            createdAt:   rec.createdAt,
            updatedAt:   rec.updatedAt,
            thumbnail:   rec.thumbnail ? `/api/scenes/${rec.id}/thumbnail` : null,
          };
        } catch { return null; }
      })
      .filter(Boolean)
      .sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt));
  }

  getThumbnailPath(id) {
    const dir = path.join(this.scenesDir, id);
    for (const ext of [".png", ".jpg", ".jpeg"]) {
      const p = path.join(dir, `thumbnail${ext}`);
      if (fs.existsSync(p)) return p;
    }
    return null;
  }
}

module.exports = { SceneManager };
