-- EC-042 · Cross-field consistency · error
-- Purpose: find pairs of flights whose times overlap.
-- Rationale: one traveller cannot be on two aircraft at once, so an overlap means a wrong time or date on one of
-- them. Pairs that EC-024 already reports as duplicate entries are left out, so one mistake raises one query.
SELECT 'EC-042'                                  AS check_id,
       'error'                                   AS severity,
       'flights'                                 AS table_name,
       b.id                                      AS record_id,
       'dep_utc'                                 AS field,
       b.dep_utc                                 AS value,
       'Overlaps flight ' || a.id || ' (' || a.dep_utc || ' → ' || a.arr_utc || ')' AS message
FROM flights a
JOIN flights b
  ON a.id < b.id
 AND julianday(b.dep_utc) < julianday(a.arr_utc)
 AND julianday(a.dep_utc) < julianday(b.arr_utc)
WHERE NOT (a.flight_no = b.flight_no AND a."date" = b."date" AND a.from_iata = b.from_iata)
ORDER BY b.id, a.id;
