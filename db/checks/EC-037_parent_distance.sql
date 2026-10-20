-- EC-037 · Cross-field consistency · error
-- Purpose: find small locations more than 200 km from their parent.
-- Rationale: a small place is a day trip from its main place; a distant parent means a wrong parent or wrong
-- coordinates. haversine_km(lat1, lng1, lat2, lng2) is the great-circle distance, registered from Python by
-- tools/load_sqlite.py (SQLite has no trigonometry built in on every platform).
SELECT 'EC-037'                                  AS check_id,
       'error'                                   AS severity,
       'locations'                               AS table_name,
       l.id                                      AS record_id,
       'parent'                                  AS field,
       l.parent_id                               AS value,
       printf('%.0f km from parent %s (limit 200 km)', haversine_km(l.lat, l.lng, p.lat, p.lng), p.id) AS message
FROM locations l
JOIN locations p ON p.id = l.parent_id
WHERE haversine_km(l.lat, l.lng, p.lat, p.lng) > 200
ORDER BY l.id;
