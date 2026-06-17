const crypto = require('node:crypto');

// Password hashing with scrypt (built into Node) — replaces bcryptjs.
// Stored format: "<saltHex>:<hashHex>".

const KEYLEN = 64;

function hash(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const derived = crypto.scryptSync(password, salt, KEYLEN).toString('hex');
  return `${salt}:${derived}`;
}

function verify(password, stored) {
  const [salt, hashHex] = stored.split(':');
  if (!salt || !hashHex) return false;
  const derived = crypto.scryptSync(password, salt, KEYLEN);
  const stbuf = Buffer.from(hashHex, 'hex');
  return derived.length === stbuf.length && crypto.timingSafeEqual(derived, stbuf);
}

module.exports = { hash, verify };
