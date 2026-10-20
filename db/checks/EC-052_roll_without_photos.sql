-- EC-052 · Completeness · warning
-- Purpose: list film rolls without a single photo.
-- Rationale: an empty roll shows an empty strip on the film page; usually its scans have not been added yet.
SELECT 'EC-052'                                  AS check_id,
       'warning'                                 AS severity,
       'film_rolls'                              AS table_name,
       r.roll                                    AS record_id,
       'roll'                                    AS field,
       '0'                                       AS value,
       'Roll has no photos'                      AS message
FROM film_rolls r
WHERE NOT EXISTS (SELECT 1 FROM photos p WHERE p.roll_id = r.roll)
ORDER BY r.roll;
