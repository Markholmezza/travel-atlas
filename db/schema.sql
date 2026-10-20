-- Travel Atlas: SQLite schema for the validation layer (loaded by tools/load_sqlite.py into build/atlas.db).
-- The site keeps reading the original data files; this database exists only to check them.
--
-- Every constraint is labelled with the Check ID it implements (docs/validation_plan.md).
--   * CHECK constraints are named "EC-xxx:<field>"; SQLite puts the name in the error message, which is how the
--     loader maps a rejected row back to its Check ID and source field. When one field has two rules, the name
--     gets a qualifier ("EC-034:parent_size", "EC-034:parent_self") and the loader maps it to the field.
--   * EC-011 patterns are written with GLOB, which has no repetition counts, so each one is spelled out to accept
--     exactly what the Python regular expression in tools/validate_data.py accepts; tools/test_validation.py
--     compares the two on several thousand generated values.
--   * NOT NULL, UNIQUE and FOREIGN KEY errors carry no constraint name, so tools/load_sqlite.py maps them by
--     table and column (REQUIRED_CHECK, UNIQUE_CHECK, FOREIGN_KEY_CHECK); the comments here give the same IDs.
--   * Foreign keys are DEFERRABLE INITIALLY DEFERRED: rows may arrive in any order (a small place before its
--     parent, or two places pointing at each other), and the loader finds violations per row with
--     PRAGMA foreign_key_check before committing each table.
--   * Empty CSV cells are loaded as NULL, so NOT NULL also rejects empty strings.

PRAGMA foreign_keys = ON;

-- ---------- reference tables ----------

-- COUNTRIES in data/cities.js
CREATE TABLE countries (
  -- EC-005: a country needs its codes for the flag and the map shading
  name         TEXT NOT NULL,
  iso2         TEXT NOT NULL,
  iso_numeric  TEXT NOT NULL,
  -- EC-021: locations refer to countries by name
  PRIMARY KEY (name),
  -- EC-021: two countries with one code would share a flag or a map shape
  UNIQUE (iso2),
  UNIQUE (iso_numeric),
  -- EC-011: ISO 3166-1 alpha-2 in lower case, the flag file name
  CONSTRAINT "EC-011:iso2" CHECK (iso2 GLOB '[a-z][a-z]'),
  -- EC-011: ISO 3166-1 numeric, kept as text with leading zeros ("032"); matches the world map's feature ids
  CONSTRAINT "EC-011:iso" CHECK (iso_numeric GLOB '[0-9][0-9][0-9]')
);

-- REGIONS in data/cities.js: the region codelist (EC-017)
CREATE TABLE regions (
  -- EC-005: the sidebar heading and its URL
  name  TEXT NOT NULL,
  slug  TEXT NOT NULL,
  -- EC-021: one row per region
  PRIMARY KEY (name),
  -- EC-021: the slug is the URL hash (#latin-america)
  UNIQUE (slug),
  -- EC-011: slugs are lower-case letters, digits and single inner hyphens
  CONSTRAINT "EC-011:slug" CHECK (slug <> '' AND slug NOT GLOB '*[^a-z0-9-]*'
                                  AND slug NOT GLOB '-*' AND slug NOT GLOB '*-' AND slug NOT GLOB '*--*')
);

-- tools/airports.csv (OurAirports); only the airports that flights use are loaded
CREATE TABLE airports (
  -- EC-006: flights refer to airports by code
  iata          TEXT NOT NULL,
  name          TEXT,
  municipality  TEXT,
  country       TEXT,
  lat           REAL,
  lng           REAL,
  -- EC-021: flights refer to airports by IATA code
  PRIMARY KEY (iata),
  -- EC-011: IATA airport codes are three capital letters
  CONSTRAINT "EC-011:iata" CHECK (iata GLOB '[A-Z][A-Z][A-Z]'),
  -- EC-012: a reference position outside the globe (or missing: typeof is then 'null') would poison every
  -- distance computed from it
  CONSTRAINT "EC-012:lat" CHECK (typeof(lat) IN ('real', 'integer') AND lat BETWEEN -90 AND 90),
  CONSTRAINT "EC-012:lng" CHECK (typeof(lng) IN ('real', 'integer') AND lng BETWEEN -180 AND 180)
);

-- ---------- places and photos ----------

-- CITIES in data/cities.js
CREATE TABLE locations (
  -- EC-005: needed to place a location on the map and in the sidebar
  id          TEXT NOT NULL,
  en          TEXT NOT NULL,
  local_name  TEXT,
  region      TEXT NOT NULL,
  lat         REAL NOT NULL,
  lng         REAL NOT NULL,
  -- EC-016: size is "big" when omitted (data/cities.js documents this default); the loader fills it in
  size        TEXT NOT NULL DEFAULT 'big',
  parent_id   TEXT,
  hue         INTEGER,
  place       TEXT,
  -- EC-021: the id is the URL and the photo folder name
  PRIMARY KEY (id),
  -- EC-022: routes.csv names its stops by English name, so a name must identify one location
  UNIQUE (en),
  -- EC-011: the id is a slug (URL hash and photos/<id>/ folder)
  CONSTRAINT "EC-011:id" CHECK (id <> '' AND id NOT GLOB '*[^a-z0-9-]*'
                                AND id NOT GLOB '-*' AND id NOT GLOB '*-' AND id NOT GLOB '*--*'),
  -- EC-012: a position the map can draw (typeof also rejects text such as '47.3N')
  CONSTRAINT "EC-012:lat" CHECK (typeof(lat) IN ('real', 'integer') AND lat BETWEEN -90 AND 90),
  CONSTRAINT "EC-012:lng" CHECK (typeof(lng) IN ('real', 'integer') AND lng BETWEEN -180 AND 180),
  -- EC-015: hue is a colour-wheel angle
  CONSTRAINT "EC-015:hue" CHECK (hue IS NULL OR (typeof(hue) = 'integer' AND hue BETWEEN 0 AND 359)),
  -- EC-016: the two levels the sidebar knows
  CONSTRAINT "EC-016:size" CHECK (size IN ('big', 'small')),
  -- EC-034: a small place hangs under a main place; a main place hangs under nothing
  CONSTRAINT "EC-034:parent_size" CHECK ((size = 'small') = (parent_id IS NOT NULL)),
  -- EC-034: a place cannot be its own parent (a foreign key alone allows this one-row cycle)
  CONSTRAINT "EC-034:parent_self" CHECK (parent_id IS NULL OR parent_id <> id),
  -- EC-017: region comes from the REGIONS codelist
  FOREIGN KEY (region) REFERENCES regions (name) DEFERRABLE INITIALLY DEFERRED,
  -- EC-029: the parent must exist (self-reference; deferred so a child may precede its parent and cycles load,
  -- leaving them to the EC-036 query)
  FOREIGN KEY (parent_id) REFERENCES locations (id) DEFERRABLE INITIALLY DEFERRED
);

-- country / countries of a location (a mountain pass can lie in two countries)
CREATE TABLE location_countries (
  -- EC-005: a location has at least one country
  location_id  TEXT NOT NULL,
  country      TEXT NOT NULL,
  -- EC-021: a country is listed once per location
  PRIMARY KEY (location_id, country),
  -- structural: rows are derived from locations; a rejected location's rows are skipped, never reported twice
  FOREIGN KEY (location_id) REFERENCES locations (id) DEFERRABLE INITIALLY DEFERRED,
  -- EC-026: every country is defined in COUNTRIES
  FOREIGN KEY (country) REFERENCES countries (name) DEFERRABLE INITIALLY DEFERRED
);

-- film stocks, derived from film/rolls.csv: the first roll of a stock defines its ISO
CREATE TABLE film_stocks (
  -- EC-008: a stock is a name with a box speed
  name  TEXT NOT NULL,
  iso   INTEGER NOT NULL,
  -- EC-046: one ISO per stock
  PRIMARY KEY (name),
  -- target of the composite foreign key film_rolls (film_stock, iso)
  UNIQUE (name, iso)
);

-- film/rolls.csv
CREATE TABLE film_rolls (
  -- EC-008: the film page needs stock, speed and gauge
  roll          TEXT NOT NULL,
  film_stock    TEXT NOT NULL,
  iso           INTEGER NOT NULL,
  format_gauge  TEXT NOT NULL,   -- first word of rolls.csv format: 135 or 120
  format_note   TEXT,            -- the rest of format, free text (e.g. "medium format 6×7")
  camera        TEXT,
  lens          TEXT,
  date_from     TEXT,
  date_to       TEXT,
  lab           TEXT,
  note          TEXT,
  box_image     TEXT,
  -- EC-021: photos refer to rolls by name
  PRIMARY KEY (roll),
  -- EC-011: stock + ISO + "-" + two-digit sequence, e.g. Portra400-02 (^[A-Za-z]+[0-9]+-[0-9]{2}$): letters, then
  -- digits, then "-NN"; the part before "-NN" has only letters and digits and no letter after a digit
  CONSTRAINT "EC-011:roll" CHECK (roll GLOB '[A-Za-z]*[0-9]-[0-9][0-9]'
                                  AND substr(roll, 1, length(roll) - 3) NOT GLOB '*[^A-Za-z0-9]*'
                                  AND substr(roll, 1, length(roll) - 3) NOT GLOB '*[0-9]*[A-Za-z]*'),
  -- EC-015: box speeds of still films
  CONSTRAINT "EC-015:iso" CHECK (typeof(iso) = 'integer' AND iso BETWEEN 1 AND 6400),
  -- EC-019: the gauge selects canister (135) or spool (120) artwork
  CONSTRAINT "EC-019:format" CHECK (format_gauge IN ('135', '120')),
  -- EC-009: real calendar dates (SQLite's date() normalises 2026-02-30 to 2026-03-02, so it must round-trip)
  CONSTRAINT "EC-009:date_from" CHECK (date_from IS NULL OR date(date_from) IS date_from),
  CONSTRAINT "EC-009:date_to" CHECK (date_to IS NULL OR date(date_to) IS date_to),
  -- EC-047: a roll is finished after it is started
  CONSTRAINT "EC-047:date_to" CHECK (date_from IS NULL OR date_to IS NULL OR date_from <= date_to),
  -- EC-046: the roll's ISO agrees with its stock's ISO
  FOREIGN KEY (film_stock, iso) REFERENCES film_stocks (name, iso) DEFERRABLE INITIALLY DEFERRED
);

-- the photos lists in data/cities.js (a plain "01.jpg" or {file, film, roll, scan})
CREATE TABLE photos (
  -- EC-008: a photo is a file in a location's folder
  location_id  TEXT NOT NULL,
  file         TEXT NOT NULL,
  is_film      INTEGER NOT NULL DEFAULT 0,
  roll_id      TEXT,
  scan         TEXT,
  -- EC-021: a file is listed once per location
  PRIMARY KEY (location_id, file),
  -- EC-023: one negative (roll + frame scan) belongs to one photo; digital photos have NULLs, which never clash
  UNIQUE (roll_id, scan),
  -- EC-011: file names written by tools/add_photos.py (01.jpg, 02.jpg …)
  CONSTRAINT "EC-011:file" CHECK (file GLOB '[0-9][0-9].jpg' OR file GLOB '[0-9][0-9][0-9].jpg'),
  -- EC-011: scan numbers are the scanner's file names
  CONSTRAINT "EC-011:scan" CHECK (scan IS NULL OR (scan <> '' AND scan NOT GLOB '*[^A-Za-z0-9-]*')),
  -- EC-045: film photos and only film photos name a roll
  CONSTRAINT "EC-045:roll" CHECK ((is_film = 1) = (roll_id IS NOT NULL)),
  -- structural: rows are derived from locations
  FOREIGN KEY (location_id) REFERENCES locations (id) DEFERRABLE INITIALLY DEFERRED,
  -- EC-027: the roll is defined in film/rolls.csv
  FOREIGN KEY (roll_id) REFERENCES film_rolls (roll) DEFERRABLE INITIALLY DEFERRED
);

-- ---------- transport ----------

-- flights/flights.csv
CREATE TABLE flights (
  -- EC-006: needed to list a flight and to compute its duration
  id            TEXT NOT NULL,
  "date"        TEXT NOT NULL,
  flight_no     TEXT NOT NULL,
  airline       TEXT,
  airline_iata  TEXT,
  from_iata     TEXT NOT NULL,
  from_city     TEXT,
  to_iata       TEXT NOT NULL,
  to_city       TEXT,
  dep_utc       TEXT NOT NULL,
  arr_utc       TEXT NOT NULL,
  aircraft      TEXT,
  seat          TEXT,
  note          TEXT,
  show          TEXT,
  -- EC-021: one row per Flightradar24 track
  PRIMARY KEY (id),
  -- EC-011: flight number + "_" + 8 hex digits (^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?_[0-9a-f]{8}$): the "_" and hex
  -- suffix, and before it the flight number rule below
  CONSTRAINT "EC-011:id" CHECK (
    id GLOB '*_[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    AND substr(id, 1, length(id) - 9) GLOB '[A-Z0-9][A-Z0-9][0-9]*'
    AND length(substr(id, 1, length(id) - 9)) - length(rtrim(substr(id, 1, length(id) - 9), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')) <= 1
    AND substr(rtrim(substr(id, 1, length(id) - 9), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 3) NOT GLOB '*[^0-9]*'
    AND length(rtrim(substr(id, 1, length(id) - 9), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')) BETWEEN 3 AND 6),
  -- EC-011: two-character airline designator + 1–4 digits + optional suffix letter (^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?$):
  -- third character a digit; at most one trailing capital letter; between them only digits, 1–4 in all
  CONSTRAINT "EC-011:flight_no" CHECK (
    flight_no GLOB '[A-Z0-9][A-Z0-9][0-9]*'
    AND length(flight_no) - length(rtrim(flight_no, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')) <= 1
    AND substr(rtrim(flight_no, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 3) NOT GLOB '*[^0-9]*'
    AND length(rtrim(flight_no, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')) BETWEEN 3 AND 6),
  CONSTRAINT "EC-011:airline_iata" CHECK (airline_iata IS NULL OR airline_iata GLOB '[A-Z0-9][A-Z0-9]'),
  CONSTRAINT "EC-011:from_iata" CHECK (from_iata GLOB '[A-Z][A-Z][A-Z]'),
  CONSTRAINT "EC-011:to_iata" CHECK (to_iata GLOB '[A-Z][A-Z][A-Z]'),
  -- EC-009: a real calendar date
  CONSTRAINT "EC-009:date" CHECK (date("date") IS "date"),
  -- EC-010: times SQLite can parse (julianday returns NULL otherwise)
  CONSTRAINT "EC-010:dep_utc" CHECK (julianday(dep_utc) IS NOT NULL),
  CONSTRAINT "EC-010:arr_utc" CHECK (julianday(arr_utc) IS NOT NULL),
  -- EC-018: show codelist
  CONSTRAINT "EC-018:show" CHECK (show IS NULL OR show IN ('yes', 'no')),
  -- EC-039: an aircraft lands after it takes off …
  CONSTRAINT "EC-039:arr_utc" CHECK (julianday(arr_utc) > julianday(dep_utc)),
  -- EC-039: … somewhere else
  CONSTRAINT "EC-039:to_iata" CHECK (from_iata <> to_iata),
  -- EC-040 (coarse): date is the local departure date, and every UTC offset lies between -12 h and +14 h, so it is
  -- never more than one day from the UTC departure date. The exact rule needs time-zone data and runs in Python
  CONSTRAINT "EC-040:date" CHECK (abs(julianday("date") - julianday(date(dep_utc))) <= 1),
  -- EC-040: the airline code is the first two characters of the flight number
  CONSTRAINT "EC-040:airline_iata" CHECK (airline_iata IS NULL OR airline_iata = substr(flight_no, 1, 2)),
  -- EC-040: the id starts with the flight number
  CONSTRAINT "EC-040:id" CHECK (substr(id, 1, length(flight_no) + 1) = flight_no || '_'),
  -- EC-028: both airports are in the reference table
  FOREIGN KEY (from_iata) REFERENCES airports (iata) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (to_iata) REFERENCES airports (iata) DEFERRABLE INITIALLY DEFERRED
);

-- routes/routes.csv (ground transport)
CREATE TABLE routes (
  -- EC-007: a route needs a mode and two ends
  id           TEXT NOT NULL,
  mode         TEXT NOT NULL,
  "date"       TEXT,
  from_name    TEXT NOT NULL,
  to_name      TEXT NOT NULL,
  number       TEXT,
  operator     TEXT,
  distance_km  REAL,
  duration     TEXT,
  note         TEXT,
  show         TEXT,
  via          TEXT,
  -- EC-021: rail details and GeoJSON features refer to routes by id
  PRIMARY KEY (id),
  -- EC-011: the id is a slug (also the track file name in routes/raw/)
  CONSTRAINT "EC-011:id" CHECK (id <> '' AND id NOT GLOB '*[^a-z0-9-]*'
                                AND id NOT GLOB '-*' AND id NOT GLOB '*-' AND id NOT GLOB '*--*'),
  -- EC-009: a real calendar date when given (EC-053 warns when it is missing)
  CONSTRAINT "EC-009:date" CHECK ("date" IS NULL OR date("date") IS "date"),
  -- EC-015: positive and shorter than half the equator
  CONSTRAINT "EC-015:distance_km" CHECK (distance_km IS NULL
                                         OR (typeof(distance_km) IN ('real', 'integer') AND distance_km > 0 AND distance_km <= 20000)),
  -- EC-018: one map layer per mode
  CONSTRAINT "EC-018:mode" CHECK (mode IN ('rail', 'drive', 'water')),
  CONSTRAINT "EC-018:show" CHECK (show IS NULL OR show IN ('yes', 'no'))
);

-- routes/rail.csv: details of rail routes, one row per route
CREATE TABLE rail_details (
  -- EC-007: the row must say which route it belongs to
  route_id       TEXT NOT NULL,
  operator_code  TEXT,
  train_number   TEXT,
  train_name     TEXT,
  dep_station    TEXT,
  arr_station    TEXT,
  dep_local      TEXT,
  arr_local      TEXT,
  rolling_stock  TEXT,
  class          TEXT,
  coach          TEXT,
  seat_or_berth  TEXT,
  note           TEXT,
  -- EC-021: one details row per route
  PRIMARY KEY (route_id),
  -- EC-011: operator codes name the logo file assets/operators/<code>.svg|png
  CONSTRAINT "EC-011:operator_code" CHECK (operator_code IS NULL
                                           OR (operator_code NOT GLOB '*[^A-Z0-9]*' AND length(operator_code) BETWEEN 2 AND 10)),
  -- EC-010: local times SQLite can parse
  CONSTRAINT "EC-010:dep_local" CHECK (dep_local IS NULL OR julianday(dep_local) IS NOT NULL),
  CONSTRAINT "EC-010:arr_local" CHECK (arr_local IS NULL OR julianday(arr_local) IS NOT NULL),
  -- EC-047: the train arrives after it leaves
  CONSTRAINT "EC-047:arr_local" CHECK (dep_local IS NULL OR arr_local IS NULL OR julianday(arr_local) > julianday(dep_local)),
  -- EC-030: the route exists (that it is a rail route is checked in Python)
  FOREIGN KEY (route_id) REFERENCES routes (id) DEFERRABLE INITIALLY DEFERRED
);
