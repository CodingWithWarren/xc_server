const crypto = require('node:crypto');
const db = require('../db');
const jwt = require('../utils/jwt');
const password = require('../utils/password');

function issueToken(user) {
  return jwt.sign(
    { sub: user.id, email: user.email },
    process.env.JWT_SECRET,
    process.env.JWT_EXPIRES_IN || '7d',
  );
}

// POST /v1/auth/register
function register({ body, send }) {
  const { email, password: pw } = body || {};

  if (!email || typeof email !== 'string') {
    return send(400, { error: 'email is required' });
  }
  if (!pw || typeof pw !== 'string' || pw.length < 8) {
    return send(400, { error: 'password must be at least 8 characters' });
  }

  const userId = crypto.randomUUID();
  const normalizedEmail = email.toLowerCase().trim();

  try {
    db.prepare('INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)').run(
      userId,
      normalizedEmail,
      password.hash(pw),
    );
  } catch (err) {
    if (String(err.message).includes('UNIQUE constraint failed')) {
      return send(409, { error: 'Email already registered' });
    }
    throw err;
  }

  const user = { id: userId, email: normalizedEmail };
  return send(201, { userId, email: normalizedEmail, token: issueToken(user) });
}

// POST /v1/auth/login
function login({ body, send }) {
  const { email, password: pw } = body || {};

  if (!email || !pw) {
    return send(400, { error: 'email and password are required' });
  }

  const user = db
    .prepare('SELECT * FROM users WHERE email = ?')
    .get(email.toLowerCase().trim());

  if (!user || !password.verify(pw, user.password_hash)) {
    return send(401, { error: 'Invalid credentials' });
  }

  return send(200, { userId: user.id, email: user.email, token: issueToken(user) });
}

module.exports = { register, login };
