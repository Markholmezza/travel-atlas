-- EC-041 · Cross-field consistency · error
-- Purpose: find flights shorter than 20 minutes or longer than 20 hours (arr_utc − dep_utc).
-- Rationale: the shortest scheduled hops take about 20 minutes and the longest non-stop flights under 19 hours;
-- anything outside is a wrong time or date. Arrival before departure is already rejected by CHECK "EC-039:arr_utc".
SELECT 'EC-041'                                  AS check_id,
       'error'                                   AS severity,
       'flights'                                 AS table_name,
       id                                        AS record_id,
       'arr_utc'                                 AS field,
       arr_utc                                   AS value,
       printf('Flight time %d min (%s → %s) is outside 20 min – 20 h', CAST(round(minutes) AS INTEGER), dep_utc, arr_utc) AS message
FROM (SELECT *, (julianday(arr_utc) - julianday(dep_utc)) * 1440 AS minutes FROM flights)
WHERE minutes < 20 OR minutes > 20 * 60
ORDER BY id;
