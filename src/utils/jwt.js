const crypto = require('node:crypto');

// Minimal HS256 JWT implementation using Node's built-in crypto.
// Replaces the `jsonwebtoken` package so the server needs no external deps.

function base64url(input) {
  return Buffer.from(input).toString('base64url');
}

function parseExpiry(str) {
  // Supports "7d", "12h", "30m", "60s", or a raw number of seconds.
  if (typeof str === 'number') return str;
  const m = /^(\d+)([smhd])?$/.exec(String(str || '').trim());
  if (!m) return 7 * 24 * 3600;
  const n = parseInt(m[1], 10);
  const mult = { s: 1, m: 60, h: 3600, d: 86400 }[m[2] || 's'];
  return n * mult;
}

function sign(payload, secret, expiresIn) {
  const header = { alg: 'HS256', typ: 'JWT' };
  const now = Math.floor(Date.now() / 1000);
  const body = {
    ...payload,
    iat: now,
    exp: now + parseExpiry(expiresIn),
  };

  const data = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(body))}`;
  const sig = crypto.createHmac('sha256', secret).update(data).digest('base64url');
  return `${data}.${sig}`;
}

function verify(token, secret) {
  const parts = token.split('.');
  if (parts.length !== 3) throw new Error('malformed token');

  const [headerB64, payloadB64, sig] = parts;
  const data = `${headerB64}.${payloadB64}`;
  const expected = crypto.createHmac('sha256', secret).update(data).digest('base64url');

  const sigBuf = Buffer.from(sig);
  const expBuf = Buffer.from(expected);
  if (sigBuf.length !== expBuf.length || !crypto.timingSafeEqual(sigBuf, expBuf)) {
    throw new Error('invalid signature');
  }

  const payload = JSON.parse(Buffer.from(payloadB64, 'base64url').toString('utf8'));
  if (payload.exp && Math.floor(Date.now() / 1000) > payload.exp) {
    throw new Error('token expired');
  }
  return payload;
}

module.exports = { sign, verify };
