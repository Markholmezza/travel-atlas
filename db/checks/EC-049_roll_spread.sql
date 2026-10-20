-- EC-049 · Cross-field consistency · warning
-- Purpose: find film rolls whose photos are spread over more than 5 main places (a small place counts as its
-- parent, so a day trip does not add a place).
-- Rationale: one roll over many places is possible on a long trip, but it is also what a mistyped roll name
-- looks like (two rolls merged into one). A warning asks for review, not a correction.
SELECT 'EC-049'                                  AS check_id,
       'warning'                                 AS severity,
       'film_rolls'                              AS table_name,
       roll                                      AS record_id,
       'roll'                                    AS field,
       COUNT(DISTINCT main_place)                AS value,
       printf('Photos spread over %d main places (more than 5): %s', COUNT(DISTINCT main_place), group_concat(DISTINCT main_place)) AS message
FROM (SELECT r.roll, COALESCE(l.parent_id, l.id) AS main_place
      FROM film_rolls r
      JOIN photos p ON p.roll_id = r.roll
      JOIN locations l ON l.id = p.location_id
      ORDER BY main_place)
GROUP BY roll
HAVING COUNT(DISTINCT main_place) > 5
ORDER BY roll;
