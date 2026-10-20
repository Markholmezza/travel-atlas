-- EC-036 · Cross-field consistency · error
-- Purpose: find locations whose chain of parents leads back to themselves (A → B → A, or longer).
-- Rationale: a cycle makes the hierarchy impossible to render. EC-034 (only small places have parents) and
-- EC-035 (a parent is big) rule cycles out today; this query is kept as a structural guarantee in case deeper
-- hierarchies are ever allowed. A one-row cycle (parent = itself) is rejected by CHECK "EC-034:parent".
-- The walk stops at depth 100, so a cycle that does not pass through the starting row still terminates.
WITH RECURSIVE chain (start_id, cur_id, depth, path) AS (
    SELECT id, parent_id, 1, id
    FROM locations
    WHERE parent_id IS NOT NULL
  UNION ALL
    SELECT c.start_id, l.parent_id, c.depth + 1, c.path || ' → ' || l.id
    FROM chain c
    JOIN locations l ON l.id = c.cur_id
    WHERE l.parent_id IS NOT NULL
      AND c.cur_id <> c.start_id
      AND c.depth < 100
)
SELECT 'EC-036'                                  AS check_id,
       'error'                                   AS severity,
       'locations'                               AS table_name,
       start_id                                  AS record_id,
       'parent'                                  AS field,
       (SELECT parent_id FROM locations WHERE id = start_id) AS value,
       'Parent chain loops back: ' || path || ' → ' || start_id AS message
FROM chain
WHERE cur_id = start_id
ORDER BY start_id;
