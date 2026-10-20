#!/usr/bin/env python3
"""Load the data files into SQLite so the constraints in db/schema.sql can check them, row by row.

Usage:
    python3 tools/load_sqlite.py [--data-dir DIR] [--db PATH]

  1. Creates the database from db/schema.sql (default build/atlas.db, replaced on every run; build/ is not committed)
  2. Inserts every row of the data files one at a time, tables in dependency order (countries and regions before
     locations, rolls before photos, airports before flights, routes before rail details)
  3. A row that violates a constraint is not loaded; the loader records table, record id, the constraint and the
     raw value, and carries on. Foreign keys are deferred, so after each table PRAGMA foreign_key_check lists the
     rows whose parent is missing; they are reported and removed before the table is committed
  4. Rows that only fail because the row they come from or point to was rejected (the photos of a rejected
     location, say) are skipped and counted, not reported a second time
  5. Prints a summary per table and the list of violations

Each violation carries the Check ID of docs/validation_plan.md: named CHECK constraints ("EC-012:lat") carry it in
their name; NOT NULL, UNIQUE and FOREIGN KEY errors are mapped by table and column below.
tools/validate_data.py runs this loader as its second layer and merges the violations into its report.
The site keeps reading the original files; nothing is exported from the database. Standard library only.
"""

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_data as ad      # noqa: E402

SCHEMA = ad.ROOT / "db" / "schema.sql"

# NOT NULL failures → the Required check of each table
REQUIRED_CHECK = {
    "countries": "EC-005", "regions": "EC-005", "locations": "EC-005", "location_countries": "EC-005",
    "airports": "EC-006", "flights": "EC-006",
    "routes": "EC-007", "rail_details": "EC-007",
    "film_stocks": "EC-008", "film_rolls": "EC-008", "photos": "EC-008",
}
# UNIQUE and PRIMARY KEY failures, by table and columns (as SQLite lists them)
UNIQUE_CHECK = {
    ("countries", "name"): "EC-021", ("countries", "iso2"): "EC-021", ("countries", "iso_numeric"): "EC-021",
    ("regions", "name"): "EC-021", ("regions", "slug"): "EC-021",
    ("airports", "iata"): "EC-021",
    ("locations", "id"): "EC-021", ("locations", "en"): "EC-022",
    ("location_countries", "location_id,country"): "EC-021",
    ("film_stocks", "name"): "EC-046", ("film_stocks", "name,iso"): "EC-046",
    ("film_rolls", "roll"): "EC-021",
    ("photos", "location_id,file"): "EC-021", ("photos", "roll_id,scan"): "EC-023",
    ("flights", "id"): "EC-021", ("routes", "id"): "EC-021", ("rail_details", "route_id"): "EC-021",
}
# FOREIGN KEY failures, by child table and columns; None marks a structural key (the child row is derived from
# the parent row, so a failure there is always a consequence of the parent's rejection, never a new finding)
FOREIGN_KEY_CHECK = {
    ("locations", "region"): "EC-017",
    ("locations", "parent_id"): "EC-029",
    ("location_countries", "location_id"): None,
    ("location_countries", "country"): "EC-026",
    ("film_rolls", "film_stock,iso"): "EC-046",
    ("photos", "location_id"): None,
    ("photos", "roll_id"): "EC-027",
    ("flights", "from_iata"): "EC-028", ("flights", "to_iata"): "EC-028",
    ("rail_details", "route_id"): "EC-030",
}
# Field name in the data files, where the column is named differently (findings use the data files' names)
FIELD_OF = {
    ("countries", "iso_numeric"): "iso",
    ("locations", "local_name"): "local", ("locations", "parent_id"): "parent",
    ("location_countries", "location_id"): "id",
    ("film_stocks", "name"): "film_stock",
    ("film_rolls", "format_gauge"): "format", ("film_rolls", "format_note"): "format",
    ("photos", "location_id"): "id", ("photos", "roll_id"): "roll", ("photos", "is_film"): "film",
    ("routes", "from_name"): "from", ("routes", "to_name"): "to",
    ("rail_details", "route_id"): "id",
}
TABLE_ORDER = ["countries", "regions", "locations", "location_countries", "film_stocks", "film_rolls", "photos",
               "airports", "flights", "routes", "rail_details"]


class Row:
    """One row to insert: column values, plus where it came from (for reports and for skipping dependants)."""

    def __init__(self, values, report_table, record_id, source, ref, parent_ref=None):
        self.values, self.report_table, self.record_id = values, report_table, record_id
        self.source, self.ref, self.parent_ref = source, ref, parent_ref


class Violation:
    def __init__(self, check_id, table, report_table, record_id, field, value, constraint, message):
        self.check_id, self.table, self.report_table, self.record_id = check_id, table, report_table, record_id
        self.field, self.value, self.constraint, self.message = field, value, constraint, message


class LoadResult:
    def __init__(self, db_path):
        self.db_path = db_path
        self.stats = {}             # table → {rows, loaded, rejected, skipped}
        self.violations = []
        self.skipped = []           # (table, record id, reason)
        self.not_loaded = []        # data files left out because they failed EC-001 / EC-002 / EC-003


def blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def nz(v):
    """Empty cells become NULL, so NOT NULL also rejects empty strings."""
    return None if blank(v) else v


def number(v):
    """CSV cell → int or float; text that is not a number is kept as text, for the CHECK constraint to reject."""
    if blank(v):
        return None
    for conv in (int, float):
        try:
            return conv(v)
        except (TypeError, ValueError):
            pass
    return v


def build_rows(ds):
    """{table: [Row]} from the data files."""
    t = defaultdict(list)

    if ds.ok("cities"):
        for name, c in ds.countries.items():
            if isinstance(c, dict):
                t["countries"].append(Row({"name": name, "iso2": nz(c.get("iso2")), "iso_numeric": nz(c.get("iso"))},
                                          "countries", name, c, f"countries:{name}"))
        for i, r in enumerate(ds.regions):
            if isinstance(r, dict):
                t["regions"].append(Row({"name": nz(r.get("name")), "slug": nz(r.get("slug"))},
                                        "regions", r.get("name") or f"REGIONS[{i + 1}]", r, f"regions#{i}"))
        for i, c in enumerate(ds.cities):
            if not isinstance(c, dict):
                continue
            ref, lid = f"locations#{i}", c.get("id") or f"CITIES[{i + 1}]"
            t["locations"].append(Row({
                "id": nz(c.get("id")), "en": nz(c.get("en")), "local_name": nz(c.get("local")),
                "region": nz(c.get("region")), "lat": c.get("lat"), "lng": c.get("lng"),
                "size": nz(c.get("size")) or "big",          # the documented default
                "parent_id": nz(c.get("parent")), "hue": c.get("hue"), "place": nz(c.get("place")),
            }, "locations", lid, c, ref))
            countries = c.get("countries") if isinstance(c.get("countries"), list) else \
                ([c["country"]] if not blank(c.get("country")) else [])
            for country in countries:
                t["location_countries"].append(Row({"location_id": c.get("id"), "country": nz(country)},
                                                   "locations", lid, {"id": c.get("id"), "country": country},
                                                   f"{ref}:country:{country}", ref))
            photos = c.get("photos") if isinstance(c.get("photos"), list) else []
            for j, p in enumerate(photos):
                if isinstance(p, str):
                    p = {"file": p}
                if not isinstance(p, dict):
                    continue
                scan = p.get("scan")
                t["photos"].append(Row({
                    "location_id": c.get("id"), "file": nz(p.get("file")), "is_film": 1 if p.get("film") is True else 0,
                    "roll_id": nz(p.get("roll")), "scan": None if scan is None else str(scan),
                }, "photos", f"{lid}/{p.get('file') or '#' + str(j + 1)}", p, f"{ref}:photo#{j}", ref))

    if ds.ok("rolls"):
        stocks = {}
        for r in ds.rows("rolls"):
            iso = number(r["iso"])
            if not blank(r["film_stock"]) and isinstance(iso, int) and r["film_stock"] not in stocks:
                stocks[r["film_stock"]] = Row({"name": r["film_stock"], "iso": iso}, "film_rolls", r["roll"],
                                              r, f"film_stocks:{r['film_stock']}")
        t["film_stocks"] = list(stocks.values())
        for r in ds.rows("rolls"):
            fmt = r["format"].split(None, 1)
            t["film_rolls"].append(Row({
                "roll": nz(r["roll"]), "film_stock": nz(r["film_stock"]), "iso": number(r["iso"]),
                "format_gauge": fmt[0] if fmt else None, "format_note": fmt[1] if len(fmt) > 1 else None,
                "camera": nz(r["camera"]), "lens": nz(r["lens"]), "date_from": nz(r["date_from"]),
                "date_to": nz(r["date_to"]), "lab": nz(r["lab"]), "note": nz(r["note"]), "box_image": nz(r["box_image"]),
            }, "film_rolls", r["roll"] or f"line {r['_line']}", r, f"film_rolls#{r['_line']}"))

    if ds.ok("flights"):
        used = {r[k] for r in ds.rows("flights") for k in ("from_iata", "to_iata") if r[k]}
        if ds.ok("airports"):
            seen = set()
            for a in ds.rows("airports"):
                if a["iata"] in used and a["iata"] not in seen:     # duplicates in the reference table: EC-021 in Python
                    seen.add(a["iata"])
                    t["airports"].append(Row({
                        "iata": a["iata"], "name": nz(a["name"]), "municipality": nz(a["municipality"]),
                        "country": nz(a["country"]), "lat": number(a["lat"]), "lng": number(a["lng"]),
                    }, "airports", a["iata"], a, f"airports:{a['iata']}"))
        # dep_local is not in flights.csv: tools/build_flights.py computes it from the airport's time zone and
        # writes it to flights.geojson; EC-040 compares the date column with it
        dep_local = {}
        if ds.ok("flights_geo"):
            for f in ds.data["flights_geo"]["features"]:
                p = f.get("properties") if isinstance(f, dict) else None
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    dep_local[p["id"]] = nz(p.get("dep_local"))
        cols = ad.CSV_COLUMNS["flights"]
        for r in ds.rows("flights"):
            values = {c: nz(r[c]) for c in cols}
            values["dep_local"] = dep_local.get(r["id"])
            t["flights"].append(Row(values, "flights", r["id"] or f"line {r['_line']}", r, f"flights#{r['_line']}"))

    if ds.ok("routes"):
        for r in ds.rows("routes"):
            t["routes"].append(Row({
                "id": nz(r["id"]), "mode": nz(r["mode"]), "date": nz(r["date"]), "from_name": nz(r["from"]),
                "to_name": nz(r["to"]), "number": nz(r["number"]), "operator": nz(r["operator"]),
                "distance_km": number(r["distance_km"]), "duration": nz(r["duration"]), "note": nz(r["note"]),
                "show": nz(r["show"]), "via": nz(r["via"]),
            }, "routes", r["id"] or f"line {r['_line']}", r, f"routes#{r['_line']}"))

    if ds.ok("rail"):
        cols = ad.CSV_COLUMNS["rail"][1:]
        for r in ds.rows("rail"):
            values = {"route_id": nz(r["id"])}
            values.update({c: nz(r[c]) for c in cols})
            t["rail_details"].append(Row(values, "rail_details", r["id"] or f"line {r['_line']}", r, f"rail#{r['_line']}"))

    return t


def connect(db_path):
    conn = sqlite3.connect(str(db_path), isolation_level=None)      # explicit BEGIN / COMMIT per table
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.create_function("haversine_km", 4, ad.haversine_km, deterministic=True)
    except (TypeError, sqlite3.NotSupportedError):      # SQLite before 3.8.3
        conn.create_function("haversine_km", 4, ad.haversine_km)
    return conn


def _violation(table, row, check_id, columns, constraint, message):
    field = FIELD_OF.get((table, columns[-1]), columns[-1])
    return Violation(check_id, table, row.report_table, row.record_id, field,
                     ad.fmt_value(row.source.get(field)), constraint, message)


def from_integrity_error(table, row, msg):
    """sqlite3.IntegrityError message → Violation with its Check ID."""
    kind, _, detail = msg.partition(" constraint failed: ")
    if kind == "CHECK":
        m = re.fullmatch(r"(EC-\d{3}):(\w+)", detail)
        if not m:
            raise RuntimeError(f"{table}: CHECK constraint without a Check ID: {detail}")
        field = m.group(2)
        return Violation(m.group(1), table, row.report_table, row.record_id, field,
                         ad.fmt_value(row.source.get(field)), f'CHECK "{detail}"', f'Rejected by SQLite: CHECK "{detail}"')
    columns = [c.split(".", 1)[1] for c in detail.split(", ")]
    if kind == "NOT NULL":
        return _violation(table, row, REQUIRED_CHECK[table], columns, f"NOT NULL {table}.{columns[0]}",
                          f"Required value is empty: NOT NULL {table}.{columns[0]}")
    if kind == "UNIQUE":
        key = (table, ",".join(columns))
        if key not in UNIQUE_CHECK:
            raise RuntimeError(f"UNIQUE constraint without a Check ID: {key}")
        return _violation(table, row, UNIQUE_CHECK[key], columns, f"UNIQUE {table}({', '.join(columns)})",
                          f"Value already used by another row: UNIQUE {table}({', '.join(columns)})")
    raise RuntimeError(f"{table}: unexpected constraint error: {msg}")


def load(ds, db_path):
    """Load a Dataset into a new database at db_path; returns a LoadResult (the database is left on disk)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    result = LoadResult(db_path)
    result.not_loaded = [p.path for p in ds.problems]
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        rows = build_rows(ds)
        rejected_refs = set()                  # rows that were not loaded
        rejected_values = defaultdict(list)    # table → values of its rows that were not loaded
        for table in TABLE_ORDER:
            stats = result.stats[table] = {"rows": len(rows[table]), "loaded": 0, "rejected": 0, "skipped": 0}
            cols = None
            by_rowid = {}
            conn.execute("BEGIN")
            for row in rows[table]:
                if row.parent_ref in rejected_refs:
                    stats["skipped"] += 1
                    rejected_refs.add(row.ref)
                    result.skipped.append((table, row.record_id, "comes from a row that was not loaded"))
                    continue
                cols = list(row.values)
                try:
                    cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                       [row.values[c] for c in cols])
                except sqlite3.IntegrityError as e:
                    result.violations.append(from_integrity_error(table, row, str(e)))
                    stats["rejected"] += 1
                    rejected_refs.add(row.ref)
                    rejected_values[table].append(row.values)
                    continue
                by_rowid[cur.lastrowid] = row
                stats["loaded"] += 1
            # deferred foreign keys: list the violating rows, report them, delete them, repeat (a deleted parent can
            # orphan another row of the same table), then commit
            fks = defaultdict(lambda: [None, [], []])
            for fid, _, parent, frm, to, *_ in conn.execute(f"PRAGMA foreign_key_list({table})"):
                fks[fid][0] = parent
                fks[fid][1].append(frm)
                fks[fid][2].append(to)
            while True:
                bad = defaultdict(list)
                for _, rowid, _, fid in conn.execute(f"PRAGMA foreign_key_check({table})"):
                    bad[rowid].append(fid)
                if not bad:
                    break
                for rowid, fids in bad.items():
                    row = by_rowid[rowid]
                    found = []
                    for fid in fids:
                        parent, frm, to = fks[fid]
                        vals = [row.values.get(c) for c in frm]
                        if any(all(r.get(tc) == v for tc, v in zip(to, vals)) for r in rejected_values[parent]):
                            continue        # the parent row exists in the data but was rejected
                        key = (table, ",".join(frm))
                        if key not in FOREIGN_KEY_CHECK or FOREIGN_KEY_CHECK[key] is None:
                            raise RuntimeError(f"FOREIGN KEY without a Check ID failed: {key}")
                        fk = f"FOREIGN KEY {table}({', '.join(frm)}) → {parent}({', '.join(to)})"
                        found.append(_violation(table, row, FOREIGN_KEY_CHECK[key], frm, fk,
                                                f"No matching row in {parent}: {fk}"))
                    if found:
                        result.violations += found
                        stats["rejected"] += 1
                    else:
                        stats["skipped"] += 1
                        result.skipped.append((table, row.record_id, "points to a row that was not loaded"))
                    stats["loaded"] -= 1
                    rejected_refs.add(row.ref)
                    rejected_values[table].append(row.values)
                    conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                    del by_rowid[rowid]
            conn.execute("COMMIT")
    finally:
        conn.close()
    return result


def main():
    ap = argparse.ArgumentParser(description="Load the data files into SQLite and report constraint violations.")
    ap.add_argument("--data-dir", default=str(ad.ROOT), help="site directory to load (default: this repository)")
    ap.add_argument("--db", help="database file (default: <data-dir>/build/atlas.db)")
    args = ap.parse_args()
    ds = ad.Dataset(args.data_dir)
    db = Path(args.db) if args.db else ds.dir / "build" / "atlas.db"
    res = load(ds, db)
    deny = ad.Denylist.load(ds.dir)
    say = lambda s: print(deny.redact(s))      # never print a denylisted name (EC-055)

    for path in res.not_loaded:
        say(f"Not loaded (fails a structure check, see validate_data.py): {path}")
    say(f"{'table':<20}{'rows':>6}{'loaded':>8}{'rejected':>10}{'skipped':>9}")
    for table in TABLE_ORDER:
        s = res.stats[table]
        say(f"{table:<20}{s['rows']:>6}{s['loaded']:>8}{s['rejected']:>10}{s['skipped']:>9}")
    if res.violations:
        say("\nRejected rows:")
        for v in res.violations:
            say(f"[{v.check_id}] {v.table} {v.record_id}: {v.constraint}; {v.field}={v.value!r}")
    if res.skipped:
        say(f"\nSkipped {len(res.skipped)} rows that depend on a rejected row:")
        for table, rid, why in res.skipped:
            say(f"  {table} {rid}: {why}")
    total = sum(s["rejected"] for s in res.stats.values())
    say(f"\n{sum(s['loaded'] for s in res.stats.values())} rows loaded into {db}, {total} rejected, "
        f"{len(res.skipped)} skipped")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
