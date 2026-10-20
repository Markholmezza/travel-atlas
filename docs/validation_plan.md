# Data Validation Plan

Travel Atlas keeps its content in hand-edited data files (places, photos, film rolls, flights, ground routes) and
in files generated from them (GeoJSON). This plan lists every check those files must pass before the site is
published, in the spirit of a clinical Data Validation Plan: each check has an ID, a category, a severity and a
rationale, and names the layer that implements it.

**This table is the single list of rules.** Code refers to checks only by ID: `tools/validate_data.py` reads the
severity of each check from this table at run time, every SQL constraint and query is labelled with its ID, and
`tools/test_validation.py` fails if an ID is used in code but not listed here, or listed here but never implemented.

- **error**: blocks publishing (`validate_data.py` exits with code 1 and the Cloudflare build stops).
- **warning**: reported for review; publishing goes ahead.

## Checks

| Check ID | Category | Description | Severity | Rationale | Implemented in |
|---|---|---|---|---|---|
| EC-001 | Structure | Each data file exists and parses: `data/cities.js` and `data/articles.js` as JavaScript literals (duplicate keys rejected), CSV files as UTF-8 CSV, JSON and GeoJSON as JSON, each with the expected top-level shape. | error | A file that cannot be read empties a whole part of the site, and nothing in it can be checked further. | Python |
| EC-002 | Structure | Every CSV file has its required header columns (Appendix A.2). | error | Columns are read by name; a renamed or missing column turns every value in it into an empty field. | Python |
| EC-003 | Structure | GeoJSON files are a FeatureCollection of Features whose geometry is a LineString or MultiLineString; every line has at least 2 positions, every position is 2–3 numbers, and every feature has `properties.id`. | error | The map draws these geometries directly; one malformed feature breaks the layer. | Python |
| EC-004 | Structure | Location and photo objects in `data/cities.js` use only documented field names (Appendix A.3). | warning | An unknown field is usually a typo (`parnet`, `plce`) that the site silently ignores. | Python |
| EC-005 | Required | Locations have `id`, `en`, `lat`, `lng`, `region` and `country` or `countries`; countries have `iso2` and `iso`; regions have `name` and `slug`. | error | These fields place a location on the map, in the sidebar and under a flag. | Python + SQL constraint |
| EC-006 | Required | Flights have `id`, `date`, `flight_no`, `from_iata`, `to_iata`, `dep_utc` and `arr_utc`. | error | Date and airports are needed to list and draw a flight; the UTC times feed the duration and overlap checks. | Python + SQL constraint |
| EC-007 | Required | Ground routes have `id`, `mode`, `from` and `to`; rail detail rows have `id`. | error | A route without a mode or end points cannot be drawn or labelled. | Python + SQL constraint |
| EC-008 | Required | Film rolls have `roll`, `film_stock`, `iso` and `format`; photos have `file`. | error | The film page is built from these fields. | Python + SQL constraint |
| EC-009 | Format | Dates are `YYYY-MM-DD` and real calendar dates (flights, routes, film rolls, articles). | error | `2026-02-30` sorts and displays like a date but is not one. | Python + SQL constraint |
| EC-010 | Format | Date-times are ISO 8601: `YYYY-MM-DD`, then `T` or a space, then `HH:MM`, with optional seconds and `Z` or `±HH:MM` (flight UTC times, rail local times). | error | Times are compared and subtracted; an unparseable time disables every check that uses it. | Python + SQL constraint |
| EC-011 | Format | Identifiers and codes match Appendix A.1: location, route and region ids are slugs (lower-case letters, digits and single hyphens); flight ids, flight numbers, airline and airport codes, roll ids, country codes, photo file names, scan numbers, rail operator codes and durations follow their own patterns. | error | Slugs become URLs and folder names; codes are join keys to reference data and asset files. | Python + SQL constraint |
| EC-012 | Range | Latitudes are numbers in [−90, 90] and longitudes in [−180, 180]: locations, airports used by flights, and `@lat,lng` stops in `routes/routes.csv`. | error | Out-of-range coordinates cannot be drawn. | Python + SQL constraint |
| EC-013 | Range | GeoJSON positions (and `from_pos` / `to_pos`) are `[lon, lat]` with lon in [−180, 180] and lat in [−90, 90]; a position that is only valid when read as `[lat, lon]` is reported as swapped. | error | GeoJSON order is the reverse of the `lat, lng` used everywhere else, so a swap is the most common geometry error. | Python |
| EC-014 | Range | No date or date-time lies in the future (flights, routes, rail, film rolls, articles). | error | The site records past travel; a future date is a typo in the year or month. | Python |
| EC-015 | Range | Location `hue` is an integer 0–359; film `iso` is an integer 1–6400; route `distance_km` is a number in (0, 20000]. | error | Values outside these ranges break the colour scheme or are typos. | Python + SQL constraint |
| EC-016 | Enumeration | Location `size` is `big` or `small` (`big` when omitted). | error | The sidebar and the hierarchy rules know only these two values. | Python + SQL constraint |
| EC-017 | Enumeration | Location `region` is one of the names in the `REGIONS` codelist (Appendix B). | error | A location in an unknown region is missing from the sidebar. | Python + SQL constraint |
| EC-018 | Enumeration | Route `mode` is `rail`, `drive` or `water`; `show` in flights and routes is `yes`, `no` or empty. | error | The map has one layer per mode; an unknown mode is drawn as a road. | Python + SQL constraint |
| EC-019 | Enumeration | Film `format` starts with the gauge `135` or `120`; the rest of the cell is free text (e.g. `120 medium format 6×7`). | error | The gauge selects the canister or spool artwork and the strip height. | Python + SQL constraint |
| EC-020 | Enumeration | `state` / `states` are keys of `US_STATES` and appear only on United States locations; `canton` is a key of `SWISS_CANTONS` and appears only on Swiss locations. | error | The subdivision selects a flag file and a label. | Python |
| EC-021 | Uniqueness | Keys are unique: location, region, flight, route, rail and roll ids; photo files within a location; countries within a location; country codes; GeoJSON feature ids; airport codes in the reference table. | error | A duplicate key makes a lookup return the wrong record. | Python + SQL constraint |
| EC-022 | Uniqueness | Location English names are unique, and no region slug equals a location id. | error | `routes/routes.csv` refers to stops by English name; region slugs and location ids share the URL hash. | Python + SQL constraint |
| EC-023 | Uniqueness | A film frame (roll and scan number) belongs to at most one photo. | error | One negative cannot have been taken in two places. | SQL constraint |
| EC-024 | Uniqueness | A flight is not entered twice: no two flights share flight number, date and departure airport. | error | Duplicate entries double-count distance and time in the air. The key includes the departure airport because one flight number can fly several legs on the same date (revision 1.1). | SQL query |
| EC-025 | Referential integrity | Every photo listed in `data/cities.js` exists as `photos/<location id>/<file>`. | error | A missing file shows as a broken image. | Python |
| EC-026 | Referential integrity | Every country of a location is defined in `COUNTRIES`. | error | The country supplies the flag and the map shading. | Python + SQL constraint |
| EC-027 | Referential integrity | Every film photo's `roll` exists in `film/rolls.csv`. | error | The film page groups photos by roll and reads camera and stock from it. | Python + SQL constraint |
| EC-028 | Referential integrity | Every flight's `from_iata` and `to_iata` exists in the airport reference table `tools/airports.csv`. | error | Airports supply coordinates, names and time zones. | Python + SQL constraint |
| EC-029 | Referential integrity | A small location's `parent` is an existing location. | error | Small places are listed and mapped under their parent. | SQL constraint + SQL query |
| EC-030 | Referential integrity | Every row of `routes/rail.csv` belongs to a route in `routes/routes.csv` whose mode is `rail`. | error | Rail details are shown on that route's card. | Python + SQL constraint |
| EC-031 | Referential integrity | Referenced asset files exist: location flags, US state and Swiss canton flags, airline and rail operator logos, film box images. | error | A missing asset shows as a broken image. | Python |
| EC-032 | Referential integrity | Every article belongs to an existing location and its cover is one of that location's photos. | error | Articles are opened from the location page and show the cover first. | Python |
| EC-033 | Referential integrity | The generated GeoJSON matches its source table: one feature per shown row of `flights/flights.csv` and `routes/routes.csv`, no extra features, and equal key fields. | error | A table edited without rerunning the build tool publishes a stale map. | Python |
| EC-034 | Cross-field consistency | A location has a `parent` exactly when its size is `small`, and is never its own parent. | error | Only small places hang under a main place. | Python + SQL constraint |
| EC-035 | Cross-field consistency | A small location's parent is a big location. | error | The hierarchy has exactly two levels. | SQL query |
| EC-036 | Cross-field consistency | Following `parent` links never leads back to the starting location. | error | A cycle makes the hierarchy impossible to render. EC-034 and EC-035 already rule cycles out; this check is kept as a structural guarantee in case deeper hierarchies are ever allowed. | SQL query |
| EC-037 | Cross-field consistency | A small location lies within 200 km of its parent (great-circle distance). | error | A distant parent means a wrong parent or wrong coordinates. | SQL query |
| EC-038 | Cross-field consistency | A location lies inside its country's reference extent (bounding box of the country's airports in `tools/airports.csv`, padded by 3°); swapped or sign-flipped coordinates are named as such. | error | A range check cannot see a `lat`/`lng` swap or a lost minus sign when the wrong values are still in range. | Python |
| EC-039 | Cross-field consistency | A flight arrives after it departs and lands at a different airport from the one it left. | error | Both are physically impossible. | Python + SQL constraint |
| EC-040 | Cross-field consistency | A flight's `date` is its local departure date: the date of `dep_local`, the departure time that `tools/build_flights.py` converts to the departure airport's time zone (UTC when unknown) and writes to `flights/flights.geojson`. Flights hidden with `show = no` have no feature and are not compared. `airline_iata` is the first two characters of `flight_no`; `id` starts with `flight_no` followed by `_`. | error | The data dictionary (`tools/build_flights.py`) defines `date` as the local departure date, the date on the boarding pass and on the map. Revision 1.2 replaced a ±1 day tolerance around the UTC date, which had hidden 7 flights holding the UTC date; the codes are derived from each other. | Python + SQL constraint |
| EC-041 | Cross-field consistency | Flight duration (`arr_utc` − `dep_utc`) is between 20 minutes and 20 hours. | error | The shortest scheduled hops take about 20 minutes and the longest non-stop flights under 19 hours. | SQL query |
| EC-042 | Cross-field consistency | No two flights overlap in time. Pairs already reported by EC-024 are excluded. | error | One traveller cannot be on two aircraft at once. | SQL query |
| EC-043 | Cross-field consistency | The average speed implied by the distance between the airports and the duration is 150–1,100 km/h. Flights already outside EC-041 are excluded. | warning | Catches a wrong airport or time that still passes EC-041. | SQL query |
| EC-044 | Cross-field consistency | GeoJSON track ends lie near their declared ends: within 200 km of the airports for flights (and `from_pos` / `to_pos` within 5 km of them), within 25 km of `from_pos` / `to_pos` for ground routes; a swapped `[lat, lon]` track is named as such. | error | Detects a swapped track that EC-013 cannot see because every value is in range. | Python |
| EC-045 | Cross-field consistency | A photo marked `film:true` has a `roll`, and a photo with a `roll` is marked `film:true`. | error | Film photos are listed on the film page by roll. | Python + SQL constraint |
| EC-046 | Cross-field consistency | A roll's `iso` equals the speed in its stock name when the name contains one, and all rolls of one stock have the same ISO. | error | Box speed is a property of the stock, not of the roll. | Python + SQL constraint |
| EC-047 | Cross-field consistency | Time order: roll `date_from` ≤ `date_to`; rail `dep_local` < `arr_local`; the rail departure date equals the route's `date`. | error | An end before its start is a typo. | Python + SQL constraint |
| EC-048 | Cross-field consistency | A route's `distance_km` is within 25% of the length of its GeoJSON track. | warning | A hand-edited distance with a typo (1021 → 10210) is shown on the card. | Python |
| EC-049 | Cross-field consistency | The photos of one roll are spread over at most 5 main places (a small place counts as its parent). | warning | One roll over many places is possible on a long trip, but it is also what a mistyped roll name looks like. | SQL query |
| EC-050 | Completeness | Every file and folder under `photos/` is referenced by a location. | warning | Orphan files are published but never shown. | Python |
| EC-051 | Completeness | Every location has at least one photo. | warning | A location without photos shows an empty page. | SQL query |
| EC-052 | Completeness | Every film roll has at least one photo. | warning | An empty roll shows an empty strip on the film page. | SQL query |
| EC-053 | Completeness | Every ground route has a date. | warning | An undated route cannot be placed in time on the transport page. | Python |
| EC-054 | Privacy | Images carry no embedded metadata: no EXIF (JPEG APP1, PNG `eXIf`, WebP `EXIF`), no XMP, no IPTC (JPEG APP13); GPS data is named explicitly. ICC colour profiles are allowed. | error | De-identification: EXIF holds GPS position, capture time and camera serial numbers. | Python |
| EC-055 | Privacy | No term from the privacy denylist appears in any published text or data file, or in any file path. A match is reported as `***` with file and line only. | error | De-identification: the denylist holds names that must never be published, so the report must not repeat them either. | Python |

## How the checks run

`python3 tools/validate_data.py [--data-dir DIR]` runs three layers in order and merges their findings into one
discrepancy report:

1. **Python checks** (`tools/validate_data.py`) read the raw files: structure, required fields, formats, ranges,
   codelists, files on disk, GeoJSON geometry and privacy. These are the front-line edit checks.
2. **SQLite load** (`tools/load_sqlite.py`) loads every row into `build/atlas.db`, built from `db/schema.sql`.
   PRIMARY KEY, FOREIGN KEY, NOT NULL, UNIQUE and named CHECK constraints (`CONSTRAINT "EC-012:lat"`) reject bad
   rows one at a time; each rejection is recorded with table, record id, constraint and raw value, and the load
   continues. Rows derived from a rejected row (its photos, for instance) are skipped and counted, not reported
   again.
3. **SQL query checks** (`db/checks/EC-xxx_*.sql`) run cross-record checks on the loaded data. Every query
   returns `check_id, severity, table_name, record_id, field, value, message`.

Row-level rules that can be written as constraints are enforced in both the Python and the SQL layer, as a
second line of defence. When both layers find the same discrepancy (same check, record, field and value), the
report lists it once with `detected by: python, sqlite`. Cross-record rules are implemented once, as SQL queries.
Cross-field checks only look at values that passed their own format and range checks, so one wrong value raises
one discrepancy rather than a chain of them.

**Outputs:** the terminal lists errors, then warnings, one per line
(`[EC-012] error locations zurich lat=-132.8 Latitude out of range [-90, 90]`), and ends with a summary line.
`build/validation_report.md` and `build/validation_report.csv` hold the same discrepancies, a per-check summary
and the SQLite load statistics. Exit code 0 means no errors (warnings allowed), 1 means at least one error, and 2
means the validator itself could not run.

**Privacy denylist:** EC-055 reads terms from `tools/privacy_denylist.txt` (one per line, `#` for comments; the
file is listed in `.gitignore` because it contains the names it protects) and from the environment variable
`ATLAS_PRIVACY_DENYLIST` (terms separated by newlines or commas; set as a GitHub Actions secret or a Cloudflare
environment variable). Matching ignores case and accents and treats spaces, hyphens, underscores and dots between
words alike. If neither source exists, EC-055 is skipped with a warning in the run notes. Every output (terminal,
Markdown, CSV) is passed through the same matcher, and matches are replaced by `***`.

**Scope of file scans** (EC-050, EC-054, EC-055): every file under the data directory except `.git/`, `build/`,
`tests/fixtures/` (test data contains deliberate violations) and paths listed in `.gitignore`, which are never
published.

## Appendix A: formats

### A.1 Identifiers and codes (EC-011)

| Field | Pattern | Example |
|---|---|---|
| Location `id`, route `id`, rail `id`, region `slug` | `^[a-z0-9]+(-[a-z0-9]+)*$` | `buenos-aires` |
| Flight `id` | `^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?_[0-9a-f]{8}$` (flight number + `_` + 8 hex digits from the Flightradar24 export) | `AM58_3dbd5d3f` |
| `flight_no` | `^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?$` | `5F5405` |
| `airline_iata` | `^[A-Z0-9]{2}$` | `5F` |
| `from_iata`, `to_iata`, airport `iata` | `^[A-Z]{3}$` | `EZE` |
| Roll id (`roll`) | `^[A-Za-z]+[0-9]+-[0-9]{2}$` (stock + ISO + `-` + sequence) | `Portra400-02` |
| Country `iso2` / `iso` | `^[a-z]{2}$` / `^[0-9]{3}$` | `ar` / `032` |
| US state / Swiss canton `code` | `^[A-Z]{2}$` | `GA` |
| Photo `file` | `^[0-9]{2,3}\.jpg$` | `01.jpg` |
| Photo `scan` | `^[A-Za-z0-9-]+$` | `3529-portra400-5493` |
| Rail `operator_code` | `^[A-Z0-9]{2,10}$` | `SBB` |
| Route `duration` | `H:MM` or whole minutes | `9:30`, `570` |

### A.2 Required CSV columns (EC-002)

| File | Columns |
|---|---|
| `film/rolls.csv` | roll, film_stock, iso, format, camera, lens, date_from, date_to, lab, note, box_image |
| `flights/flights.csv` | id, date, flight_no, airline, airline_iata, from_iata, from_city, to_iata, to_city, dep_utc, arr_utc, aircraft, seat, note, show |
| `routes/routes.csv` | id, mode, date, from, to, number, operator, distance_km, duration, note, show, via |
| `routes/rail.csv` | id, operator_code, train_number, train_name, dep_station, arr_station, dep_local, arr_local, rolling_stock, class, coach, seat_or_berth, note |
| `tools/airports.csv` | iata, name, municipality, country, lat, lng, size |
| `data/changelog-text.csv` | commit, text |

### A.3 Documented fields in `data/cities.js` (EC-004)

- Location: id, en, local, country, countries, state, states, canton, region, lat, lng, hue, size, parent, place, photos, flag, flagName
- Photo object: file, film, roll, scan (a photo can also be a plain file name string)

## Appendix B: codelists

| Codelist | Values | Defined in |
|---|---|---|
| Location size (EC-016) | `big`, `small` | this plan; `db/schema.sql` |
| Region (EC-017) | names in `REGIONS` | `data/cities.js` |
| Route mode (EC-018) | `rail`, `drive`, `water` | this plan (the map's layers); `db/schema.sql` |
| Show flag (EC-018) | `yes`, `no`, empty | this plan; `db/schema.sql` |
| Film gauge (EC-019) | `135`, `120` | this plan; `db/schema.sql` |
| US state, Swiss canton (EC-020) | keys of `US_STATES`, `SWISS_CANTONS` | `data/cities.js` |
| Airports (EC-028) | `tools/airports.csv` (OurAirports, medium and large airports) | reference table |

## Appendix C: thresholds

| Check | Threshold |
|---|---|
| EC-037 | small location ≤ 200 km from its parent |
| EC-038 | country extent = bounding box of the country's airports ± 3° |
| EC-040 | flight `date` equal to the date of `dep_local`, no tolerance (±1 day around the `dep_utc` date until revision 1.2) |
| EC-041 | 20 min ≤ flight duration ≤ 20 h |
| EC-043 | 150 km/h ≤ implied average speed ≤ 1,100 km/h |
| EC-044 | flight track ends ≤ 200 km from the airport (Flightradar24 tracks can miss take-off or landing; `tools/build_flights.py` matches airports up to 200 km away), `from_pos` / `to_pos` ≤ 5 km from it; ground route ends ≤ 25 km from `from_pos` / `to_pos` |
| EC-048 | stated distance within ±25% of the track length |
| EC-049 | at most 5 main places per roll |

## Revision history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-10-19 | First version. |
| 1.1 | 2026-10-19 | EC-024: the duplicate key was flight number + date. The first run on real data flagged AM58 on 2026-01-02, which is one flight number flying two legs (MEX → MTY → NRT) and correct data. The departure airport was added to the key (and to the EC-042 exclusion that reuses it), and a regression fixture (`tests/fixtures/OK-001_multi_leg_flight`) keeps a multi-leg flight passing. |
| 1.2 | 2026-10-19 | EC-040: `date` must equal the local departure date exactly; the ±1 day tolerance around the UTC date is gone. Profiling showed that all 23 rows of `flights/flights.csv` held the UTC departure date, although the data dictionary in `tools/build_flights.py` defines the local date (the map already showed local dates, recomputed into the GeoJSON). The tolerance had let 7 flights through. The 7 dates were corrected to the local date from the existing time-zone data (`dep_local` in `flights/flights.geojson`). New fixtures: `EC-040_date_not_local_departure_date` (a UTC date where the local date is the day before) and `OK-002_local_date_before_utc_date` (the same flight with the local date passes). After the correction the two AM58 legs fall on different local dates; the EC-024 key from 1.1 stays, since legs on the same local date remain possible (OK-001). Added the section "Considered but not implemented". |

## Considered but not implemented

Rules that were proposed, assessed and deliberately left out, so the question does not have to be reopened
without new information.

| Proposed check | Decision and reason |
|---|---|
| Frame order of a film roll follows the order of travel (scan numbers of a roll increase with the places and dates it was shot in). Prompted by Portra400-02, whose scans 000037 (Beijing), 000038 (Mexico City) and 000039–40 (Buenos Aires) run against the itinerary. | Not implemented (2026-10-19). Scan numbers are assigned by the lab when the film is scanned, and labs often number frames in an order that differs from the order of exposure (strips cut and scanned in batches, rescans appended at the end). They are not a reliable source for chronology, so the check would raise queries that no data correction could resolve: false positives with nothing to fix. Scan numbers stay identifiers only (EC-011, EC-023). |
