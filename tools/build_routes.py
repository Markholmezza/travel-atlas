#!/usr/bin/env python3
"""Ground transport (rail / drive / water): the routes/raw/ inbox + routes/routes.csv → routes/routes.geojson. Flights use tools/build_flights.py.

Usage:
    python3 tools/build_routes.py

Each route comes from one of two sources:
  1. A track file in routes/raw/<mode>/, where mode is drive / rail / water;
     .gpx .kml .geojson are supported. The file name (without extension) is the route id.
     GPS tracks lose 500 m at each end (so they don't reveal where you stayed), then get simplified (about 50 m tolerance).
  2. No track: add a row to routes/routes.csv whose via column lists the stops in order (start and end included), separated by |:
       Atlanta Peachtree|Gainesville GA|…|Washington Union Station
     Stop coordinates come from routes/cache/places.json, otherwise from one OpenStreetMap (Nominatim) lookup that is cached there;
     places already on the site can be written by name (e.g. "Chișinău|Tiraspol", coordinates from data/cities.js);
     if a lookup is off, write "name@lat,lng" in via, or edit places.json.
     A stop starting with ~ only steers the route (e.g. forces a particular road) and isn't shown on cards: "~RP82@-33.0020,-69.1302"
     Rail follows OpenStreetMap tracks via OpenRailRouting, driving is routed once with OSRM, and water routes are straight lines;
     results are cached in routes/cache/<id>.json and only recomputed when the stops change.

routes.csv columns: id, mode, date, from, to, number, operator, distance_km, duration, note, show, via
  - a new track file adds a row; cells that can be inferred (mode, date, nearby places at each end, distance, duration) are filled in,
    and cells that already have content are never touched
  - duration is 9:30 (hours:minutes) or 570 (minutes); routes with show = no aren't drawn

Rail has a details table too, routes/rail.csv (one row per rail route, ids matching routes.csv):
  id, operator_code, train_number, train_name, dep_station, arr_station, dep_local, arr_local,
  rolling_stock, class, coach, seat_or_berth, note
  - a new rail route adds a row with what can be inferred (operator code, first and last station); existing cells are never touched
  - dep_local / arr_local are local times at the departure and arrival stations: 2025-11-03 21:05
Rail operator logo inbox rail_logos_inbox/ (not committed): a folder "<code> <name>" is created for each operator in the table;
drop a logo in it and it is processed into assets/operators/<code>.png (+ .webp) or .svg, the same way as airline logos.
The network parts use only the Python standard library; raster logos need Pillow.
"""

import csv
import io
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_flights import (haversine, simplify, split_antimeridian, unwrap,          # noqa: E402
                           excel_fix, sync_logo_box, logo_props)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "routes" / "raw"
TABLE = ROOT / "routes" / "routes.csv"
CACHE = ROOT / "routes" / "cache"
PLACES = CACHE / "places.json"
OUT = ROOT / "routes" / "routes.geojson"
CITIES_JS = ROOT / "data" / "cities.js"
RAIL_TABLE = ROOT / "routes" / "rail.csv"
OPERATOR_INBOX = ROOT / "rail_logos_inbox"
OPERATOR_DIR = ROOT / "assets" / "operators"
OPERATOR_MANIFEST = ROOT / "tools" / ".operator-logo-manifest.json"
RAIL_COLUMNS = ["id", "operator_code", "train_number", "train_name", "dep_station", "arr_station", "dep_local", "arr_local",
                "rolling_stock", "class", "coach", "seat_or_berth", "note"]

# Rail operators: code → (display name, words in routes.csv's operator column that identify it). Add a line for new ones.
OPERATORS = {
    "AMTRAK": ("Amtrak", ["amtrak"]),
    "VIA": ("VIA Rail Canada", ["via rail"]),
    "OBB": ("ÖBB", ["öbb", "obb", "nightjet"]),
    "SBB": ("SBB", ["sbb", "cff", "ffs"]),
    "DB": ("Deutsche Bahn", ["deutsche bahn", "db"]),
    "SNCF": ("SNCF", ["sncf", "ouigo", "tgv inoui"]),
    "TRENITALIA": ("Trenitalia", ["trenitalia", "frecciarossa"]),
    "ITALO": ("Italo", ["italo"]),
    "RENFE": ("Renfe", ["renfe"]),
    "NS": ("Nederlandse Spoorwegen", ["nederlandse spoorwegen", "ns"]),
    "SNCB": ("SNCB", ["sncb", "nmbs"]),
    "EUROSTAR": ("Eurostar", ["eurostar"]),
    "CD": ("České dráhy", ["české dráhy", "ceske drahy", "čd"]),
    "SZ": ("Slovenske železnice", ["slovenske železnice", "sž"]),
    "HZPP": ("HŽ Putnički prijevoz", ["hžpp", "hzpp"]),
    "TRAINOSE": ("Hellenic Train", ["hellenic train", "trainose"]),
    "JR": ("Japan Railways", ["jr east", "jr central", "jr west", "jr"]),
    "CR": ("China Railway", ["china railway", "12306"]),
}
MODES = {"drive": "drive", "rail": "rail", "water": "water"}
COLUMNS = ["id", "mode", "date", "from", "to", "number", "operator", "distance_km", "duration", "note", "show", "via"]
TRIM_M = 500
TOLERANCE_KM = 0.05
NEAR_CITY_KM = 30
UA = {"User-Agent": "travel-atlas/1.0 (travel map demo; route build)"}


# ---------- track files ----------

def parse_time(s):
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def read_gpx(path):
    pts = []
    root = ET.parse(path).getroot()
    for el in root.iter():
        if el.tag.split("}")[-1] in ("trkpt", "rtept"):
            t = next((c.text for c in el if c.tag.split("}")[-1] == "time"), None)
            pts.append((float(el.get("lat")), float(el.get("lon")), parse_time(t) if t else None))
    return pts


def read_kml(path):
    pts = []
    root = ET.parse(path).getroot()
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "coordinates" and el.text:
            for tok in el.text.split():
                v = tok.split(",")
                if len(v) >= 2:
                    pts.append((float(v[1]), float(v[0]), None))
        elif tag == "Track":                      # gx:Track: <when> and <gx:coord> pair up one to one
            whens = [parse_time(c.text) for c in el if c.tag.split("}")[-1] == "when"]
            coords = [c.text.split() for c in el if c.tag.split("}")[-1] == "coord"]
            for i, c in enumerate(coords):
                pts.append((float(c[1]), float(c[0]), whens[i] if i < len(whens) else None))
    return pts


def read_geojson(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    feats = d.get("features", [d] if d.get("type") == "Feature" else [{"geometry": d}])
    pts = []
    for f in feats:
        g = f.get("geometry") or {}
        lines = [g["coordinates"]] if g.get("type") == "LineString" else g.get("coordinates", []) if g.get("type") == "MultiLineString" else []
        times = (f.get("properties") or {}).get("coordTimes") or []
        for line in lines:
            for i, c in enumerate(line):
                pts.append((c[1], c[0], parse_time(times[i]) if i < len(times) and isinstance(times[i], str) else None))
    return pts


READERS = {".gpx": read_gpx, ".kml": read_kml, ".geojson": read_geojson, ".json": read_geojson}


def trim_ends(pts, meters):
    """Drop `meters` from the start and from the end."""
    if len(pts) < 3:
        return pts
    def cut(seq):
        d = 0
        for i in range(1, len(seq)):
            d += haversine(seq[i - 1][0], seq[i - 1][1], seq[i][0], seq[i][1]) * 1000
            if d >= meters:
                return seq[i:]
        return seq[-2:]
    out = cut(pts)
    out = cut(out[::-1])[::-1]
    return out if len(out) >= 2 else pts


def length_km(pts):
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))


# ---------- stop coordinates and online routing ----------

def get_json(url, timeout=90):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


CITY_POS = {}      # name of a place on the site (English or local) → [lat, lng] (filled from data/cities.js in main)


def resolve(name, places):
    """Stop → (name, [lat, lng]). Accepts "name@lat,lng"; places already on the site can be given by name (coordinates from data/cities.js);
    otherwise the cache, then Nominatim."""
    m = re.match(r"^(.*?)@\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$", name)
    if m:
        return m.group(1).strip(), [float(m.group(2)), float(m.group(3))]
    if name in CITY_POS:
        return name, CITY_POS[name]
    if name in places:
        return name, places[name]
    r = get_json("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": name, "format": "json", "limit": 3}))
    time.sleep(1.1)                                   # Nominatim allows at most one request per second
    if not r:
        raise RuntimeError(f'no location found for "{name}"; write it in via as "{name}@lat,lng"')
    pick = next((x for x in r if x.get("class") == "railway" or x.get("type") in ("station", "train_station")), r[0])
    places[name] = [round(float(pick["lat"]), 5), round(float(pick["lon"]), 5)]
    return name, places[name]


RAIL_PROFILE = "all_tracks_1435"   # standard gauge (1435 mm) only; otherwise narrow-gauge lines next to big stations (e.g. the Appenzell railway at St. Gallen) pull the route off course


def route_rail(points):
    """OpenRailRouting: follow OpenStreetMap tracks (station coordinates must be on or near the platforms / tracks).
    Standard gauge first; if no route is found (a genuine narrow-gauge mountain line, say), all tracks."""
    for profile in (RAIL_PROFILE, "all_tracks"):
        q = [("point", f"{la},{lo}") for la, lo in points] + [("profile", profile), ("points_encoded", "false")]
        try:
            d = get_json("https://routing.openrailrouting.org/route?" + urllib.parse.urlencode(q), timeout=180)
        except Exception:
            if profile == "all_tracks":
                raise
            continue
        return [(la, lo) for lo, la in d["paths"][0]["points"]["coordinates"]]


def route_drive(points):
    """OSRM: one road route for a drive."""
    s = ";".join(f"{lo},{la}" for la, lo in points)
    d = get_json(f"https://router.project-osrm.org/route/v1/driving/{s}?overview=full&geometries=geojson", timeout=120)
    return [(la, lo) for lo, la in d["routes"][0]["geometry"]["coordinates"]]


def route_water(points):
    return list(points)                               # no public waterway network: straight lines between the stops


ROUTERS = {"rail": route_rail, "drive": route_drive, "water": route_water}


# ---------- CSV tables like the flight table: only empty cells are filled ----------

def read_table():
    if not TABLE.exists():
        return [], list(COLUMNS)
    with open(TABLE, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        fields = list(r.fieldnames or COLUMNS)
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in r]
    fields += [c for c in COLUMNS if c not in fields]
    return rows, fields


def write_table(rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    TABLE.write_text(buf.getvalue(), encoding="utf-8-sig")     # with a BOM so Excel reads it as UTF-8


def minutes(s):
    s = (s or "").strip()
    if re.fullmatch(r"\d+:\d{1,2}", s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", s)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return None


def operator_code(text):
    t = (text or "").lower()
    for code, (_, words) in OPERATORS.items():
        if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in words):
            return code
    return ""


def read_rail():
    if not RAIL_TABLE.exists():
        return [], list(RAIL_COLUMNS)
    with open(RAIL_TABLE, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        fields = list(r.fieldnames or RAIL_COLUMNS)
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in r]
    fields += [c for c in RAIL_COLUMNS if c not in fields]
    return rows, fields


def write_csv(path, rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def parse_local(s):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return None


def load_cities():
    """data/cities.js → [(English name, local name or "", lat, lng)]."""
    js = CITIES_JS.read_text(encoding="utf-8")
    return [(en, local, float(la), float(lo)) for en, local, la, lo in
            re.findall(r'\ben:"([^"]+)",(?:\s*local:"([^"]+)",)?.*?lat:(-?[\d.]+),\s*lng:(-?[\d.]+)', js)]


def near_city(lat, lng, cities):
    en, _, la, lo = min(cities, key=lambda c: haversine(lat, lng, c[2], c[3]))
    return en if haversine(lat, lng, la, lo) <= NEAR_CITY_KM else ""


# ---------- main ----------

def main():
    for m in MODES:
        (RAW / m).mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    places = json.loads(PLACES.read_text(encoding="utf-8")) if PLACES.exists() else {}
    rows, fields = read_table()
    by_id = {r["id"]: r for r in rows}
    cities = load_cities()
    for en, local, la, lo in cities:
        CITY_POS[en] = [la, lo]
        if local:
            CITY_POS[local] = [la, lo]
    tracks, problems, added, filled, routed = {}, [], [], {}, []

    # 1. track files
    for mode in MODES:
        for f in sorted((RAW / mode).iterdir()):
            if f.name.startswith(".") or f.suffix.lower() not in READERS:
                if not f.name.startswith("."):
                    problems.append(f"routes/raw/{mode}/{f.name}: unsupported format (use .gpx .kml .geojson)")
                continue
            try:
                pts = [p for p in READERS[f.suffix.lower()](f) if p[0] is not None]
            except Exception as e:
                problems.append(f"routes/raw/{mode}/{f.name}: unreadable ({type(e).__name__}: {e})")
                continue
            if len(pts) < 2:
                problems.append(f"routes/raw/{mode}/{f.name}: fewer than 2 track points")
                continue
            raw_first, raw_last = pts[0], pts[-1]
            pts = trim_ends(pts, TRIM_M)
            tracks[f.stem] = [(la, lo) for la, lo, _ in pts]
            t0, t1 = raw_first[2], raw_last[2]
            auto = {"id": f.stem, "mode": mode, "show": "yes",
                    "date": t0.astimezone(timezone.utc).strftime("%Y-%m-%d") if t0 else "",
                    "from": near_city(raw_first[0], raw_first[1], cities), "to": near_city(raw_last[0], raw_last[1], cities),
                    "distance_km": str(round(length_km(pts))),
                    "duration": (lambda m: f"{m // 60}:{m % 60:02d}")(round((t1 - t0).total_seconds() / 60)) if t0 and t1 and t1 > t0 else ""}
            row = by_id.get(f.stem)
            if row is None:
                row = {c: "" for c in fields}
                row.update(auto)
                rows.append(row)
                by_id[f.stem] = row
                added.append(f.stem)
            else:
                for k, v in auto.items():
                    if v and not row.get(k, "").strip():
                        row[k] = v
                        filled.setdefault(f.stem, []).append(k)

    # 2. routes without a track file: route them once online from the stops (cached)
    stops_of = {}
    for row in rows:
        rid, mode = row["id"], row.get("mode", "").strip()
        if rid in tracks or not row.get("via", "").strip():
            continue
        if mode not in ROUTERS:
            problems.append(f"{rid}: mode must be drive / rail / water")
            continue
        try:
            raw_stops = [s.strip() for s in row["via"].split("|") if s.strip()]
            stops = [resolve(s.lstrip("~").strip(), places) for s in raw_stops]
            shown = [st for st, s in zip(stops, raw_stops) if not s.startswith("~")]    # stops starting with ~ only steer the route
        except Exception as e:
            problems.append(f"{rid}: {e}")
            continue
        stops_of[rid] = shown
        sig = {"mode": mode, "points": [p for _, p in stops]}
        if mode == "rail":
            sig["profile"] = RAIL_PROFILE         # re-route when the rail profile changes
        cache = CACHE / f"{rid}.json"
        cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
        if cached and cached.get("sig") == sig:
            line = [tuple(p) for p in cached["line"]]
        else:
            try:
                line = ROUTERS[mode]([p for _, p in stops])
            except Exception as e:
                problems.append(f"{rid}: routing failed ({type(e).__name__}: {e}); check the stop coordinates or add a track file")
                continue
            line = simplify(line, TOLERANCE_KM)
            cache.write_text(json.dumps({"sig": sig, "line": [[round(a, 5), round(b, 5)] for a, b in line]}), encoding="utf-8")
            routed.append(rid)
        tracks[rid] = line
        if not row.get("distance_km", "").strip():
            row["distance_km"] = str(round(length_km(line)))
            filled.setdefault(rid, []).append("distance_km")
        if not row.get("show", "").strip():
            row["show"] = "yes"
    PLACES.write_text(json.dumps(places, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if added or filled or not TABLE.exists():
        write_table(rows, fields)

    # 2b. rail details table routes/rail.csv: a row for each new rail route, empty cells only; times mangled by Excel are restored
    rail_rows, rail_fields = read_rail()
    rail_by_id = {r["id"]: r for r in rail_rows}
    rail_added, rail_filled, rail_fixed = [], {}, 0
    for row in rows:
        if row.get("mode", "").strip() != "rail":
            continue
        rid, stops = row["id"], stops_of.get(row["id"], [])
        auto = {"id": rid, "operator_code": operator_code(row.get("operator")),
                "dep_station": stops[0][0] if stops else "", "arr_station": stops[-1][0] if stops else ""}
        r = rail_by_id.get(rid)
        if r is None:
            r = {c: "" for c in rail_fields}
            r.update(auto)
            rail_rows.append(r)
            rail_by_id[rid] = r
            rail_added.append(rid)
        else:
            for k, v in auto.items():
                if v and not r.get(k, "").strip():
                    r[k] = v
                    rail_filled.setdefault(rid, []).append(k)
        for k in ("dep_local", "arr_local"):
            fixed = excel_fix(r.get(k, ""))
            if fixed != r.get(k, ""):
                r[k] = fixed
                rail_fixed += 1
    if rail_added or rail_filled or rail_fixed or not RAIL_TABLE.exists():
        write_csv(RAIL_TABLE, rail_rows, rail_fields)

    # 2c. rail operator logo inbox
    op_names = {}
    for r in rail_rows:
        code = r.get("operator_code", "").strip().upper()
        if code and code not in op_names:
            op_names[code] = OPERATORS.get(code, ("",))[0]
    logos = sync_logo_box(op_names, OPERATOR_INBOX, OPERATOR_DIR, OPERATOR_MANIFEST)

    # 3. map data
    features, hidden = [], []
    for row in rows:
        rid = row["id"]
        if rid not in tracks:
            continue
        if row.get("show", "").strip().lower() in ("no", "n", "0", "false"):
            hidden.append(rid)
            continue
        line = simplify(tracks[rid], TOLERANCE_KM)
        parts = split_antimeridian(unwrap(line))
        props = {c: row.get(c, "").strip() for c in COLUMNS if c not in ("show", "via")}
        props["minutes"] = minutes(row.get("duration"))
        props["stops"] = [n for n, _ in stops_of.get(rid, [])]
        props["from_pos"], props["to_pos"] = [line[0][1], line[0][0]], [line[-1][1], line[-1][0]]
        rr = rail_by_id.get(rid) if row.get("mode", "").strip() == "rail" else None
        if rr:      # rail details: operator, train number and name, stations, local times, rolling stock, class, coach, berth
            code = rr.get("operator_code", "").strip().upper()
            props["rail"] = {k: rr.get(k, "").strip() for k in RAIL_COLUMNS if k != "id"}
            props["rail"].update(operator_code=code, operator_name=OPERATORS.get(code, ("",))[0] or props.get("operator", ""),
                                 **logo_props(code, OPERATOR_DIR))
            dep, arr = parse_local(rr.get("dep_local")), parse_local(rr.get("arr_local"))
            if dep:
                props["date"] = dep.strftime("%Y-%m-%d")        # date in local time at the departure station
            if props["minutes"] is None and dep and arr and arr > dep:
                props["minutes"] = round((arr - dep).total_seconds() / 60)   # local times subtracted (only right within one time zone)
        geom = ({"type": "LineString", "coordinates": parts[0]} if len(parts) == 1
                else {"type": "MultiLineString", "coordinates": parts})
        features.append({"type": "Feature", "properties": props, "geometry": geom})
    order = {r["id"]: i for i, r in enumerate(rows)}      # same-day routes keep their routes.csv order (when there's no time)
    features.sort(key=lambda f: (f["properties"]["date"] or "9999", order.get(f["properties"]["id"], 0)))
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    # 4. summary
    print("========== Routes routes/ ==========")
    for f in features:
        p = f["properties"]
        n = sum(len(x) for x in (f["geometry"]["coordinates"] if f["geometry"]["type"] == "MultiLineString" else [f["geometry"]["coordinates"]]))
        print(f"  [{MODES.get(p['mode'], p['mode'])}] {p['id']}: {p['from'] or '?'} → {p['to'] or '?'}  "
              f"{p['distance_km'] or '?'} km, {n} points" + (f", {len(p['stops'])} stops" if p["stops"] else "")
              + (" (re-routed this run)" if p["id"] in routed else ""))
    if added:
        print("Added: " + ", ".join(added))
    for rid, cols in filled.items():
        print(f"Filled empty cells in {rid}: {', '.join(cols)}")
    if hidden:
        print("Not drawn (show=no): " + ", ".join(hidden))
    for p in problems:
        print("⚠ " + p)
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(features)} routes, {OUT.stat().st_size / 1024:.1f} KB")

    print("\n========== Rail details routes/rail.csv ==========")
    print(f"Added {len(rail_added)} rows" + (": " + ", ".join(rail_added) if rail_added else ""))
    for rid, cols in rail_filled.items():
        print(f"Filled empty cells in {rid}: {', '.join(cols)}")
    if rail_fixed:
        print(f"⚠ Excel changed the time format in rail.csv; restored {rail_fixed} cells to the 2025-11-03 21:05 form")
    blank = {c: [r["id"] for r in rail_rows if not r.get(c, "").strip()] for c in RAIL_COLUMNS if c not in ("id", "note")}
    blank = {c: ids for c, ids in blank.items() if ids}
    if blank:
        print("Columns still empty: " + "; ".join(f"{c} ({len(ids)})" for c, ids in blank.items()))
    no_code = [r["id"] for r in rail_rows if not r.get("operator_code", "").strip()]
    if no_code:
        print("⚠ Unknown rail operator (operator_code is empty); fill in a code in rail.csv: " + ", ".join(no_code))

    print("\n========== Rail operator logos rail_logos_inbox/ ==========")
    if logos["created"]:
        print("✦ New operators; folders created, drop their logos in:")
        for name in logos["created"]:
            print(f"    rail_logos_inbox/{name}/" + ("" if " " in name else " (unknown operator: add a line to OPERATORS in this script)"))
    print("Updated logos: " + ("none" if not logos["updated"] else ""))
    for line in logos["updated"]:
        print("    " + line)
    if logos["missing"]:
        print("⚠ No logo yet (cards show a code badge): " + ", ".join(f"{c} {logos['names'][c]}" for c in logos["missing"]))
    for code, why in logos["failed"]:
        print(f"✗ {code}: logo processing failed: {why}")
    for f in logos["unreadable"]:
        print(f"⚠ rail_logos_inbox/{f}: neither a readable bitmap nor an SVG, skipped")
    for name in logos["unknown"]:
        print(f"⚠ rail_logos_inbox/{name} matches no operator_code in rail.csv, not processed")


if __name__ == "__main__":
    main()
