-- EC-043 · Cross-field consistency · warning
-- Purpose: find flights whose average speed (great-circle distance between the airports ÷ flight time) is below
-- 150 km/h or above 1,100 km/h.
-- Rationale: catches a wrong airport code or a wrong time that still passes EC-041. Flights outside the EC-041
-- duration window are left out (already reported there).
SELECT 'EC-043'                                  AS check_id,
       'warning'                                 AS severity,
       'flights'                                 AS table_name,
       id                                        AS record_id,
       'to_iata'                                 AS field,
       from_iata || '-' || to_iata               AS value,
       printf('%.0f km in %d min is %.0f km/h (expected 150–1,100 km/h)', km, CAST(round(minutes) AS INTEGER), km / (minutes / 60)) AS message
FROM (SELECT f.*, haversine_km(a.lat, a.lng, b.lat, b.lng) AS km,
             (julianday(f.arr_utc) - julianday(f.dep_utc)) * 1440 AS minutes
      FROM flights f
      JOIN airports a ON a.iata = f.from_iata
      JOIN airports b ON b.iata = f.to_iata)
WHERE minutes BETWEEN 20 AND 20 * 60
  AND (km / (minutes / 60) < 150 OR km / (minutes / 60) > 1100)
ORDER BY id;
