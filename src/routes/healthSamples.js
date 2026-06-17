const db = require('../db');
const { resolveSourceUuid } = require('../utils/dedup');
const { validateSample } = require('../utils/validation');

const MAX_BATCH_SIZE = 1000;

const insertStmt = db.prepare(`
  INSERT OR IGNORE INTO health_samples (
    user_id, source_uuid, type, unit, value, numeric_value,
    date_from, date_to, source_platform, source_device_id,
    source_id, source_name, recording_method, device_model,
    workout_summary, metadata
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`);

// POST /v1/health-samples:batch
function batchIngest({ body, user, send }) {
  const samples = body && body.samples;

  if (!Array.isArray(samples)) {
    return send(400, { error: 'Request body must include a "samples" array' });
  }
  if (samples.length > MAX_BATCH_SIZE) {
    return send(413, {
      error: `Batch too large: max ${MAX_BATCH_SIZE} samples per request, received ${samples.length}`,
    });
  }

  let inserted = 0;
  let duplicates = 0;
  const rejected = [];

  // Single transaction: all valid rows commit together; invalid ones are skipped.
  db.exec('BEGIN');
  try {
    for (let i = 0; i < samples.length; i++) {
      const sample = samples[i];

      const validationError = validateSample(sample);
      if (validationError) {
        rejected.push({ index: i, uuid: sample.uuid ?? '', reason: validationError });
        continue;
      }

      const sourceUuid = resolveSourceUuid(sample);

      // Promote numericValue to a top-level column for cheap aggregation
      let numericValue = null;
      if (
        sample.value != null &&
        typeof sample.value.numericValue === 'number' &&
        isFinite(sample.value.numericValue)
      ) {
        numericValue = sample.value.numericValue;
      }

      const result = insertStmt.run(
        user.id,
        sourceUuid,
        sample.type,
        sample.unit,
        JSON.stringify(sample.value),
        numericValue,
        sample.dateFrom,
        sample.dateTo,
        sample.sourcePlatform,
        sample.sourceDeviceId ?? null,
        sample.sourceId ?? null,
        sample.sourceName ?? null,
        sample.recordingMethod,
        sample.deviceModel ?? null,
        sample.workoutSummary != null ? JSON.stringify(sample.workoutSummary) : null,
        sample.metadata != null ? JSON.stringify(sample.metadata) : null,
      );

      // INSERT OR IGNORE → 0 changes means the unique constraint fired (duplicate)
      if (Number(result.changes) === 1) inserted++;
      else duplicates++;
    }
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }

  return send(200, { received: samples.length, inserted, duplicates, rejected });
}

// GET /v1/health-samples?type=&from=&to=&limit=&cursor=
function listSamples({ query, user, send }) {
  const { type, from, to, cursor } = query;
  const limit = Math.min(parseInt(query.limit, 10) || 100, 1000);

  const conditions = ['user_id = ?'];
  const params = [user.id];

  if (type) {
    conditions.push('type = ?');
    params.push(type);
  }
  if (from) {
    conditions.push('date_to >= ?');
    params.push(from);
  }
  if (to) {
    conditions.push('date_from <= ?');
    params.push(to);
  }

  // Stable cursor encodes { dateTo, id } of the last row from the previous page
  if (cursor) {
    let cursorData;
    try {
      cursorData = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
    } catch {
      return send(400, { error: 'invalid cursor' });
    }
    conditions.push('(date_to < ? OR (date_to = ? AND id < ?))');
    params.push(cursorData.dateTo, cursorData.dateTo, cursorData.id);
  }

  const where = conditions.join(' AND ');
  const rows = db
    .prepare(
      `SELECT * FROM health_samples WHERE ${where} ORDER BY date_to DESC, id DESC LIMIT ?`,
    )
    .all(...params, limit);

  const samples = rows.map((row) => ({
    ...row,
    value: JSON.parse(row.value),
    workout_summary: row.workout_summary ? JSON.parse(row.workout_summary) : null,
    metadata: row.metadata ? JSON.parse(row.metadata) : null,
  }));

  let nextCursor = null;
  if (rows.length === limit) {
    const last = rows[rows.length - 1];
    nextCursor = Buffer.from(
      JSON.stringify({ dateTo: last.date_to, id: last.id }),
    ).toString('base64url');
  }

  return send(200, { samples, nextCursor, count: samples.length });
}

module.exports = { batchIngest, listSamples };
