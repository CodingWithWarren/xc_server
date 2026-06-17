const db = require('../db');

// GET /v1/summary — per-type counts, ranges, and numeric aggregates for the user.
function summary({ user, send }) {
  const perType = db
    .prepare(
      `SELECT type,
              COUNT(*)            AS count,
              MIN(date_from)      AS firstFrom,
              MAX(date_to)        AS lastTo,
              AVG(numeric_value)  AS avg,
              MIN(numeric_value)  AS min,
              MAX(numeric_value)  AS max
       FROM health_samples
       WHERE user_id = ?
       GROUP BY type
       ORDER BY count DESC`,
    )
    .all(user.id);

  const sumOf = (type) =>
    db
      .prepare(
        `SELECT COALESCE(SUM(numeric_value), 0) AS s
         FROM health_samples WHERE user_id = ? AND type = ?`,
      )
      .get(user.id, type).s;

  const totals = {
    samples: db
      .prepare('SELECT COUNT(*) AS c FROM health_samples WHERE user_id = ?')
      .get(user.id).c,
    steps: sumOf('STEPS'),
    distanceMeters: sumOf('DISTANCE_DELTA'),
    calories: sumOf('TOTAL_CALORIES_BURNED'),
  };

  return send(200, { types: perType, totals });
}

// GET /v1/series?type=HEART_RATE&buckets=300&from=&to=
// Returns a downsampled time series (averaged into buckets) for charting.
function series({ query, user, send }) {
  const { type, from, to } = query;
  if (!type) return send(400, { error: 'type is required' });

  const buckets = Math.min(Math.max(parseInt(query.buckets, 10) || 300, 10), 2000);

  const conds = ['user_id = ?', 'type = ?', 'numeric_value IS NOT NULL'];
  const params = [user.id, type];
  if (from) {
    conds.push('date_to >= ?');
    params.push(from);
  }
  if (to) {
    conds.push('date_to <= ?');
    params.push(to);
  }

  const rows = db
    .prepare(
      `SELECT date_to AS t, numeric_value AS v
       FROM health_samples WHERE ${conds.join(' AND ')}
       ORDER BY date_to ASC`,
    )
    .all(...params);

  // Downsample: average contiguous groups so the chart stays light.
  let points = rows;
  if (rows.length > buckets) {
    const groupSize = Math.ceil(rows.length / buckets);
    points = [];
    for (let i = 0; i < rows.length; i += groupSize) {
      const group = rows.slice(i, i + groupSize);
      const avg = group.reduce((s, r) => s + r.v, 0) / group.length;
      // Use the middle timestamp of the group as the label
      points.push({ t: group[Math.floor(group.length / 2)].t, v: avg });
    }
  }

  return send(200, { type, count: rows.length, points });
}

module.exports = { summary, series };
