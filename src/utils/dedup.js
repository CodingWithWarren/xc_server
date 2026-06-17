const crypto = require('crypto');

/**
 * The health package returns uuid ?? "" on some platforms/records,
 * so a blank UUID arrives with no stable identity.
 *
 * When uuid is empty we synthesise a deterministic surrogate by hashing
 * the sample's natural identity: type | dateFrom | dateTo | sourceId | valueJSON.
 * This keeps re-uploads of the same sample idempotent without a real UUID.
 *
 * Hash inputs should stay in sync with the Flutter client if it ever pre-hashes.
 */
function resolveSourceUuid(sample) {
  if (sample.uuid && sample.uuid !== '') {
    return sample.uuid;
  }

  const input = [
    sample.type,
    sample.dateFrom,
    sample.dateTo,
    sample.sourceId || '',
    JSON.stringify(sample.value),
  ].join('|');

  return 'surrogate:' + crypto.createHash('sha256').update(input).digest('hex');
}

module.exports = { resolveSourceUuid };
