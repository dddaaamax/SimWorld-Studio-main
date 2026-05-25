/**
 * Patch: Add static frontend serving to index.js for production deployment.
 * This script appends frontend-serving code to the server before the listen() call.
 *
 * Run: node patch_server.js <input_index.js> <output_index.js>
 */
const fs = require('fs');

const inputPath = process.argv[2];
const outputPath = process.argv[3];

if (!inputPath || !outputPath) {
  console.error('Usage: node patch_server.js <input> <output>');
  process.exit(1);
}

let code = fs.readFileSync(inputPath, 'utf-8');

// Insert frontend static serving before app.listen()
const FRONTEND_PATCH = `
// ── Serve frontend (production build) ──────────────────────────────────────
const FRONTEND_DIR = path.resolve(__dirname, "../dist");
if (fs.existsSync(FRONTEND_DIR)) {
  app.use(express.static(FRONTEND_DIR));
  // SPA fallback: serve index.html for non-API routes
  app.get("*", (req, res) => {
    if (!req.path.startsWith("/api/") && !req.path.startsWith("/screenshots") && !req.path.startsWith("/thumbnails") && !req.path.startsWith("/ue")) {
      res.sendFile(path.join(FRONTEND_DIR, "index.html"));
    }
  });
  console.log("  Frontend served from:", FRONTEND_DIR);
}
`;

// Find the app.listen line and insert before it
const listenPattern = /^(\/\/ ─+\n)?app\.listen\(/m;
const match = code.match(listenPattern);

if (match && match.index !== undefined) {
  code = code.slice(0, match.index) + FRONTEND_PATCH + '\n' + code.slice(match.index);
  console.log('Frontend serving patch applied.');
} else {
  // Fallback: append before last line
  const lines = code.split('\n');
  const lastLine = lines.pop();
  code = lines.join('\n') + '\n' + FRONTEND_PATCH + '\n' + lastLine;
  console.log('Frontend serving patch appended (fallback).');
}

fs.writeFileSync(outputPath, code);
console.log(`Patched: ${outputPath}`);
