const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { URL } = require('node:url');
const db = require('./db');
const { authenticate } = require('./middleware/auth');
const authRoutes = require('./routes/auth');
const healthRoutes = require('./routes/healthSamples');
const statsRoutes = require('./routes/stats');

const PORT = process.env.PORT || 3000;
const MAX_BODY_BYTES = 10 * 1024 * 1024; // 10 MB
const DASHBOARD_HTML = path.join(__dirname, '..', 'public', 'index.html');

// Route table. `auth: true` means a valid Bearer JWT is required.
// Paths are matched exactly (including the ":batch" custom-method suffix).
const routes = [
  { method: 'POST', path: '/v1/auth/register', handler: authRoutes.register },
  { method: 'POST', path: '/v1/auth/login', handler: authRoutes.login },
  { method: 'POST', path: '/v1/health-samples:batch', handler: healthRoutes.batchIngest, auth: true },
  { method: 'GET', path: '/v1/health-samples', handler: healthRoutes.listSamples, auth: true },
  { method: 'GET', path: '/v1/summary', handler: statsRoutes.summary, auth: true },
  { method: 'GET', path: '/v1/series', handler: statsRoutes.series, auth: true },
  { method: 'GET', path: '/health', handler: ({ send }) => send(200, { status: 'ok' }) },
  // The dashboard GUI — served at the root.
  { method: 'GET', path: '/', handler: ({ res }) => {
      const html = fs.readFileSync(DASHBOARD_HTML);
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
  } },
];

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('payload too large'), { statusCode: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (chunks.length === 0) return resolve(undefined);
      const raw = Buffer.concat(chunks).toString('utf8');
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(Object.assign(new Error('malformed JSON body'), { statusCode: 400 }));
      }
    });
    req.on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  const send = (status, obj) => {
    const payload = JSON.stringify(obj);
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(payload);
  };

  try {
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const route = routes.find(
      (r) => r.method === req.method && r.path === url.pathname,
    );

    if (!route) return send(404, { error: 'Not found' });

    let user = null;
    if (route.auth) {
      user = authenticate(req);
      if (!user) return send(401, { error: 'Missing or invalid Authorization token' });
    }

    const body = req.method === 'POST' ? await readBody(req) : undefined;
    const query = Object.fromEntries(url.searchParams.entries());

    await route.handler({ req, res, body, query, user, send });
  } catch (err) {
    if (err.statusCode) return send(err.statusCode, { error: err.message });
    console.error(err);
    return send(500, { error: 'Internal server error' });
  }
});

server.listen(PORT, () => {
  console.log(`XC Training server listening on http://0.0.0.0:${PORT}`);
});

function shutdown() {
  server.close(() => {
    db.close();
    process.exit(0);
  });
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
