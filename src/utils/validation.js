// Allow samples up to 5 minutes in the future (device clock skew)
const CLOCK_SKEW_MS = 5 * 60 * 1000;

// Hard range guards for known numeric types — flag, don't silently accept absurd values
const RANGE_GUARDS = {
  HEART_RATE: [0, 350],
  STEPS: [0, 100_000],
  TOTAL_CALORIES_BURNED: [0, 10_000],
  DISTANCE_DELTA: [0, 200_000],
};

/**
 * Validates a single health sample per the rules in schema §7.
 * Returns null if valid, or an error string if the sample should be rejected.
 * Only the offending sample is rejected — the rest of the batch still commits.
 */
function validateSample(sample) {
  // Required string fields
  for (const field of ['type', 'unit', 'sourcePlatform', 'recordingMethod']) {
    if (!sample[field] || typeof sample[field] !== 'string') {
      return `missing or invalid field: ${field}`;
    }
  }

  // Date presence
  if (!sample.dateFrom || !sample.dateTo) {
    return 'missing dateFrom or dateTo';
  }

  const dateFrom = new Date(sample.dateFrom);
  const dateTo = new Date(sample.dateTo);

  if (isNaN(dateFrom.getTime())) return 'invalid dateFrom';
  if (isNaN(dateTo.getTime())) return 'invalid dateTo';

  if (dateTo < dateFrom) {
    return 'dateTo before dateFrom';
  }

  if (dateTo.getTime() > Date.now() + CLOCK_SKEW_MS) {
    return 'dateTo too far in the future (exceeds 5-minute clock skew allowance)';
  }

  // Value presence
  if (!sample.value || typeof sample.value !== 'object') {
    return 'missing or invalid value object';
  }

  if (sample.type === 'WORKOUT') {
    if (!sample.value.workoutActivityType) {
      return 'WORKOUT sample missing value.workoutActivityType';
    }
  } else if ('numericValue' in sample.value) {
    const num = sample.value.numericValue;
    if (typeof num !== 'number' || !isFinite(num)) {
      return 'value.numericValue must be a finite number';
    }

    const range = RANGE_GUARDS[sample.type];
    if (range && (num < range[0] || num > range[1])) {
      return `value.numericValue ${num} out of plausible range [${range[0]}, ${range[1]}] for type ${sample.type}`;
    }
  }

  return null;
}

module.exports = { validateSample };
