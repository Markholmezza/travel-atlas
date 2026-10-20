-- EC-024 · Uniqueness · error
-- Purpose: find flights entered twice: same flight number, same date, same departure airport.
-- Rationale: a duplicate entry double-counts distance and time in the air. Each extra row is reported once,
-- pointing at the first row with the same key.
-- Revision 1.1: the key was flight number + date, which flagged AM58 on 2026-01-02. That is one flight number
-- flying two legs (MEX → MTY, then MTY → NRT after a 90-minute stop), both correct. The departure airport
-- separates legs; a re-entered leg still matches even if its times differ slightly between exports.
SELECT 'EC-024'                                  AS check_id,
       'error'                                   AS severity,
       'flights'                                 AS table_name,
       f.id                                      AS record_id,
       'flight_no'                               AS field,
       f.flight_no || ' ' || f."date" || ' ' || f.from_iata AS value,
       'Same flight number, date and departure airport as ' || MIN(d.id) AS message
FROM flights f
JOIN flights d
  ON d.flight_no = f.flight_no
 AND d."date" = f."date"
 AND d.from_iata = f.from_iata
 AND d.id < f.id
GROUP BY f.id
ORDER BY f.id;
