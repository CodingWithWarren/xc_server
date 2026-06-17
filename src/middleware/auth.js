const jwt = require('../utils/jwt');

// Verifies the Bearer JWT and returns the user, or null if unauthenticated.
// The caller is responsible for sending the 401 response.
function authenticate(req) {
  const header = req.headers['authorization'];
  if (!header || !header.startsWith('Bearer ')) return null;

  const token = header.slice(7);
  try {
    const payload = jwt.verify(token, process.env.JWT_SECRET);
    return { id: payload.sub, email: payload.email };
  } catch {
    return null;
  }
}

module.exports = { authenticate };
