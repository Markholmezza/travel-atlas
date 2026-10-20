-- EC-029 · Referential integrity · error
-- Purpose: find small locations whose parent is not a location.
-- Rationale: the deferred foreign key locations(parent_id) already rejects these rows while loading; this query
-- states the rule on its own so it still holds for a database loaded without PRAGMA foreign_keys = ON.
SELECT 'EC-029'                                  AS check_id,
       'error'                                   AS severity,
       'locations'                               AS table_name,
       l.id                                      AS record_id,
       'parent'                                  AS field,
       l.parent_id                               AS value,
       'Parent is not a location'                AS message
FROM locations l
LEFT JOIN locations p ON p.id = l.parent_id
WHERE l.parent_id IS NOT NULL
  AND p.id IS NULL
ORDER BY l.id;
