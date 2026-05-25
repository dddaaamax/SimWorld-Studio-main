'use strict';
/**
 * middleware/auth.js  (CommonJS)
 * Validates sessionToken on protected routes.
 * Attaches req.sessionToken, req.slotId, req.ueConfig to request.
 */

const PUBLIC_PATHS = new Set([
  '/api/health',
  '/api/session/acquire',
  '/api/session/status',
  '/api/session/heartbeat',
  '/api/session/release',
  '/api/events',     // SSE uses token via query param, checked inside handler
  '/api/poll',
]);

function authMiddleware(sessionManager) {
  return function(req, res, next) {
    if (PUBLIC_PATHS.has(req.path)) return next();

    const token =
      (req.headers['authorization'] || '').replace(/^Bearer\s+/i, '') ||
      req.query?.token ||
      req.body?.sessionToken ||
      '';

    if (!token) {
      return res.status(401).json({ error: 'No session token. Call /api/session/acquire first.' });
    }

    const rec = sessionManager && sessionManager.touch(token);
    if (!rec) {
      // In dev mode (no session manager), allow through
      if (!sessionManager) { req.sessionToken = '_dev'; return next(); }
      return res.status(401).json({ error: 'Session expired or invalid. Refresh to start a new session.' });
    }

    req.sessionToken = token;
    req.slotId       = rec.slotId;
    req.userId       = rec.userId;
    req.ueConfig     = rec.uePorts;
    next();
  };
}

module.exports = { authMiddleware };
