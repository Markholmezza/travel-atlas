-- EC-051 · Completeness · warning
-- Purpose: list locations without a single photo.
-- Rationale: such a location shows an empty page. Places visited without taking photos are legitimate, so this
-- is a warning for review.
SELECT 'EC-051'                                  AS check_id,
       'warning'                                 AS severity,
       'locations'                               AS table_name,
       l.id                                      AS record_id,
       'photos'                                  AS field,
       '0'                                       AS value,
       'Location has no photos'                  AS message
FROM locations l
WHERE NOT EXISTS (SELECT 1 FROM photos p WHERE p.location_id = l.id)
ORDER BY l.id;
