'use strict';
/**
 * middleware/rate-limit.js  (CommonJS)
 * Simple in-process rate limiter — no Redis required for single-instance.
 */

const isDev = process.env.NODE_ENV !== 'production';

class RateLimiter {
  constructor({ windowMs, max, keyGenerator, message }) {
    this.windowMs     = windowMs;
    this.max          = isDev ? 100_000 : max;
    this.keyGenerator = keyGenerator || (req => req.ip);
    this.message      = message || { error: 'Too many requests' };
    this._hits        = new Map();
    // Cleanup old windows every windowMs
    this._cleaner = setInterval(() => {
      const cutoff = Date.now() - this.windowMs;
      for (const [key, entry] of this._hits) {
        if (entry.windowStart < cutoff) this._hits.delete(key);
      }
    }, this.windowMs);
    if (this._cleaner.unref) this._cleaner.unref();
  }

  middleware() {
    return (req, res, next) => {
      const key = this.keyGenerator(req);
      const now = Date.now();
      let entry = this._hits.get(key);

      if (!entry || now - entry.windowStart > this.windowMs) {
        entry = { windowStart: now, count: 0 };
        this._hits.set(key, entry);
      }

      entry.count++;
      res.setHeader('X-RateLimit-Limit',     this.max);
      res.setHeader('X-RateLimit-Remaining', Math.max(0, this.max - entry.count));

      if (entry.count > this.max) {
        return res.status(429).json(this.message);
      }
      next();
    };
  }
}

const globalLimiter = new RateLimiter({
  windowMs: 15 * 60 * 1000, max: 1000,
  message:  { error: 'Too many requests. Please slow down.' },
});

const chatLimiter = new RateLimiter({
  windowMs: 60 * 1000, max: 20,
  keyGenerator: req => req.sessionToken || req.ip,
  message:  { error: 'Chat rate limit exceeded.' },
});

const arenaLimiter = new RateLimiter({
  windowMs: 60 * 1000, max: 3,
  keyGenerator: req => req.sessionToken || req.ip,
  message:  { error: 'Arena rate limit exceeded.' },
});

module.exports = { RateLimiter, globalLimiter, chatLimiter, arenaLimiter };
