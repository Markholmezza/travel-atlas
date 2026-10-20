-- EC-035 · Cross-field consistency · error
-- Purpose: find small locations whose parent is not a big location.
-- Rationale: the hierarchy has exactly two levels; a small place listed under another small place disappears
-- from the sidebar.
SELECT 'EC-035'                                  AS check_id,
       'error'                                   AS severity,
       'locations'                               AS table_name,
       l.id                                      AS record_id,
       'parent'                                  AS field,
       l.parent_id                               AS value,
       'Parent ' || p.id || ' is a ' || p.size || ' location, not a big one' AS message
FROM locations l
JOIN locations p ON p.id = l.parent_id
WHERE p.size <> 'big'
ORDER BY l.id;
