#!/usr/bin/env python3
"""Data validation: the edit checks of docs/validation_plan.md, run on the site's data files before publishing.

Usage:
    python3 tools/validate_data.py [--data-dir DIR] [--out-dir DIR] [--denylist FILE]

  1. Python checks on the raw files: structure, required fields, formats, ranges, codelists, files on disk,
     GeoJSON geometry, privacy (image metadata, denylisted names)
  2. SQLite load (tools/load_sqlite.py, db/schema.sql): rows rejected by PRIMARY KEY, FOREIGN KEY, NOT NULL,
     UNIQUE and CHECK constraints, one at a time
  3. SQL query checks (db/checks/EC-xxx_*.sql): cross-record checks on the loaded data
  The findings are merged into one discrepancy report (a discrepancy found by two layers is listed once, with
  both layers under "detected by"):
    - terminal: errors, then warnings, one per line, e.g.
        [EC-012] error locations zurich lat=-132.8 Latitude out of range [-90, 90]
      and a summary line at the end
    - <out-dir>/validation_report.md and <out-dir>/validation_report.csv

  --data-dir  the site to check (default: this repository); tools/test_validation.py points it at test data
  --out-dir   where the reports and atlas.db go (default: <data-dir>/build)
  --denylist  privacy denylist (default: <data-dir>/tools/privacy_denylist.txt); terms in $ATLAS_PRIVACY_DENYLIST
              are added. Without either, EC-055 is skipped with a warning. Matches are never printed: every output
              shows them as ***

Exit code: 0 no errors (warnings allowed), 1 at least one error, 2 the validator itself could not run.
Severity comes from the validation plan; code refers to checks only by ID. Standard library only (Python 3.8+).
"""

import argparse
import csv
import datetime as dt
import fnmatch
import io
import os
import platform
import re
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_data as ad      # noqa: E402
import load_sqlite           # noqa: E402

CHECKS_DIR = ad.ROOT / "db" / "checks"
QUERY_COLUMNS = ["check_id", "severity", "table_name", "record_id", "field", "value", "message"]

# Appendix A.1 of the validation plan (EC-011)
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FLIGHT_ID = re.compile(r"^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?_[0-9a-f]{8}$")
FLIGHT_NO = re.compile(r"^[A-Z0-9]{2}[0-9]{1,4}[A-Z]?$")
AIRLINE = re.compile(r"^[A-Z0-9]{2}$")
IATA = re.compile(r"^[A-Z]{3}$")
ROLL_ID = re.compile(r"^[A-Za-z]+[0-9]+-[0-9]{2}$")
ISO2, ISO_NUM, SUBDIVISION = re.compile(r"^[a-z]{2}$"), re.compile(r"^[0-9]{3}$"), re.compile(r"^[A-Z]{2}$")
PHOTO_FILE = re.compile(r"^[0-9]{2,3}\.jpg$")
SCAN = re.compile(r"^[A-Za-z0-9-]+$")
OPERATOR = re.compile(r"^[A-Z0-9]{2,10}$")
DURATION = re.compile(r"^(?:[0-9]+:[0-5][0-9]|[0-9]+)$")
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$")

# Appendix A.3 (EC-004) and Appendix B (codelists)
LOCATION_FIELDS = {"id", "en", "local", "country", "countries", "state", "states", "canton", "region", "lat", "lng",
                   "hue", "size", "parent", "place", "photos", "flag", "flagName"}
PHOTO_FIELDS = {"file", "film", "roll", "scan"}
SIZES = ("big", "small")
MODES = ("rail", "drive", "water")
SHOW = ("yes", "no")
GAUGES = ("135", "120")
HIDDEN = ("no", "n", "0", "false")     # rows the build tools leave out of the GeoJSON

# Appendix C: thresholds
EXTENT_MARGIN_DEG = 3          # EC-038
FLIGHT_END_KM = 200            # EC-044
FLIGHT_POS_KM = 5
ROUTE_END_KM = 25
DISTANCE_TOLERANCE = 0.25      # EC-048

# Scope of the file scans (EC-050, EC-054, EC-055): never published, or deliberately full of violations
SCAN_EXCLUDE = {".git", "build", "tests/fixtures", ad.DENYLIST_FILE}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
BINARY_EXT = IMAGE_EXT | {".gif", ".ico", ".pdf", ".heic", ".tif", ".tiff", ".woff", ".woff2", ".ttf", ".otf",
                          ".zip", ".gz", ".db", ".sqlite", ".mp4", ".mov"}


class ConfigError(Exception):
    """The validator itself cannot run (plan, schema or query files are inconsistent)."""


class Finding:
    def __init__(self, check_id, table, record_id, field, value, message, source):
        self.check_id, self.table, self.record_id = check_id, table, str(record_id)
        self.field, self.value, self.message = field, value, message
        self.sources = [source]
        self.severity = self.category = None

    def key(self):
        return self.check_id, self.table, self.record_id, self.field, self.value

    def line(self):
        what = f"{self.field}={self.value if self.value != '' else '(empty)'}" if self.field else self.value
        return " ".join(x for x in (f"[{self.check_id}]", self.severity, self.table, self.record_id, what, self.message) if x)


def blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def parse_date(s):
    m = DATE.match(s) if isinstance(s, str) else None
    try:
        return dt.date(*map(int, m.groups())) if m else None
    except ValueError:
        return None


def parse_datetime(s):
    """ISO 8601 date-time → naive datetime (converted to UTC when an offset is given); None if invalid."""
    m = DATETIME.match(s) if isinstance(s, str) else None
    if not m:
        return None
    try:
        t = dt.datetime(*(int(g or 0) for g in m.groups()[:6]))
    except ValueError:
        return None
    tz = m.group(7)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        t -= sign * dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
    return t


def to_number(v):
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    return v if ad.is_number(v) else None


def track_km(lines):
    return sum(ad.haversine_km(a[1], a[0], b[1], b[0]) for line in lines for a, b in zip(line, line[1:]))


# ---------- image metadata (EC-054) ----------

def _exif_has_gps(tiff):
    """True when the TIFF structure of an EXIF block has a GPS IFD pointer (tag 0x8825) in IFD0."""
    try:
        e = {b"II": "<", b"MM": ">"}[tiff[:2]]
        off = struct.unpack(e + "I", tiff[4:8])[0]
        n = struct.unpack(e + "H", tiff[off:off + 2])[0]
        return any(struct.unpack(e + "H", tiff[off + 2 + 12 * k:off + 4 + 12 * k])[0] == 0x8825 for k in range(n))
    except (KeyError, struct.error):
        return False


def _exif_kind(block):
    return "EXIF with GPS" if _exif_has_gps(block) else "EXIF"


def image_metadata(data, ext):
    """Embedded metadata blocks of a JPEG / PNG / WebP file, e.g. ["EXIF with GPS", "XMP"].
    ICC colour profiles (JPEG APP2, PNG iCCP, WebP ICCP) are allowed and not listed. ValueError if unreadable."""
    found = []
    if ext in (".jpg", ".jpeg"):
        if data[:2] != b"\xff\xd8":
            raise ValueError("not a JPEG file")
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                raise ValueError(f"corrupt JPEG segment at byte {i}")
            m = data[i + 1]
            if m == 0xFF:                           # fill byte
                i += 1
                continue
            if m in (0xD9, 0xDA):                   # end of image / start of scan: metadata comes before
                break
            if 0xD0 <= m <= 0xD7 or m == 0x01:      # markers without a length
                i += 2
                continue
            seg = data[i + 4:i + 2 + int.from_bytes(data[i + 2:i + 4], "big")]
            if m == 0xE1:                           # APP1: EXIF or XMP
                if seg.startswith(b"Exif\0\0"):
                    found.append(_exif_kind(seg[6:]))
                elif seg.startswith(b"http://ns.adobe.com/xap/1.0/"):
                    found.append("XMP with GPS" if b"GPS" in seg else "XMP")
                else:
                    found.append("APP1 metadata")
            elif m == 0xED:                         # APP13: Photoshop / IPTC
                found.append("IPTC")
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    elif ext == ".png":
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("not a PNG file")
        i = 8
        while i + 8 <= len(data):
            length, kind = struct.unpack(">I4s", data[i:i + 8])
            chunk = data[i + 8:i + 8 + length]
            if kind == b"eXIf":
                found.append(_exif_kind(chunk))
            elif kind in (b"tEXt", b"zTXt", b"iTXt"):
                key = chunk.split(b"\0", 1)[0]
                if key == b"XML:com.adobe.xmp":
                    found.append("XMP")
                elif key.startswith(b"Raw profile type"):
                    found.append(key.decode("latin-1"))
            if kind == b"IEND":
                break
            i += 12 + length
    elif ext == ".webp":
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError("not a WebP file")
        i = 12
        while i + 8 <= len(data):
            kind, length = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
            chunk = data[i + 8:i + 8 + length]
            if kind == b"EXIF":
                found.append(_exif_kind(chunk[6:] if chunk.startswith(b"Exif\0\0") else chunk))
            elif kind == b"XMP ":
                found.append("XMP")
            i += 8 + length + (length & 1)
    return found


# ---------- published files ----------

class GitIgnore:
    """The subset of .gitignore used here: comments, `dir/` (directories only), leading `/` or an inner `/`
    (anchored to the root), and shell globs. Ignored paths are never committed, so never published."""

    def __init__(self, path):
        self.rules = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "!")):
                    continue
                dir_only = line.endswith("/")
                pat = line.strip("/")
                self.rules.append((pat, dir_only, line.startswith("/") or "/" in pat))

    def ignored(self, rel, is_dir):
        rel = rel.as_posix()
        for pat, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            if fnmatch.fnmatch(rel if anchored else rel.rsplit("/", 1)[-1], pat):
                return True
        return False


def published_files(root):
    """Relative paths of the files under root that end up on the site (SCAN_EXCLUDE and .gitignore left out)."""
    ignore = GitIgnore(root / ".gitignore")
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames
                             if (here / d).as_posix() not in SCAN_EXCLUDE and not ignore.ignored(here / d, True))
        for f in sorted(filenames):
            rel = here / f
            if rel.as_posix() not in SCAN_EXCLUDE and not ignore.ignored(rel, False):
                out.append(rel)
    return out


# ---------- layer 1: Python checks ----------

class PythonChecks:
    def __init__(self, ds, today, denylist):
        self.ds, self.today, self.denylist = ds, today, denylist
        self.findings, self.skipped = [], []
        self.location_ids, self.photo_files, self.roll_ids = set(), {}, set()
        self.airports, self.route_modes, self.route_dates = {}, {}, {}

    def add(self, check_id, table, record_id, field, value, message):
        self.findings.append(Finding(check_id, table, record_id, field, ad.fmt_value(value), message, "python"))

    def run(self):
        for p in self.ds.problems:                                   # EC-001 / EC-002 / EC-003 (file level)
            self.add(p.check_id, p.path, p.record_id, p.field, p.value, p.message)
        self.reference_tables()
        self.locations()
        self.articles()
        self.film_rolls()
        self.flights()
        self.routes()
        flights_geo = self.geojson("flights_geo")
        routes_geo = self.geojson("routes_geo")
        self.flights_geojson(flights_geo)
        self.routes_geojson(routes_geo)
        self.files()
        return self.findings

    # ----- shared field checks -----

    def required(self, check_id, table, rid, row, fields):
        for f in fields:
            if blank(row.get(f)):
                self.add(check_id, table, rid, f, row.get(f), "Required field is missing")

    def pattern(self, table, rid, field, value, regex, what):
        """EC-011 for one field; True when the value is present and matches."""
        if blank(value):
            return False
        if not regex.match(str(value)):
            self.add("EC-011", table, rid, field, value, f"Not a valid {what}")
            return False
        return True

    def date(self, table, rid, field, value):
        """EC-009 and EC-014 for a date; returns the date when it is valid."""
        if blank(value):
            return None
        d = parse_date(value)
        if d is None:
            self.add("EC-009", table, rid, field, value, "Not a valid YYYY-MM-DD date")
        elif d > self.today:
            self.add("EC-014", table, rid, field, value, f"Date is in the future (today is {self.today})")
        return d

    def datetime(self, table, rid, field, value):
        """EC-010 and EC-014 for a date-time; returns it when valid."""
        if blank(value):
            return None
        t = parse_datetime(value)
        if t is None:
            self.add("EC-010", table, rid, field, value, "Not a valid ISO 8601 date-time (YYYY-MM-DD HH:MM)")
        elif t.date() > self.today:
            self.add("EC-014", table, rid, field, value, f"Time is in the future (today is {self.today})")
        return t

    def coordinate(self, table, rid, field, value, limit, number=None):
        """EC-012 for one latitude (limit 90) or longitude (limit 180); returns the number when valid.
        value is reported as written; number is its parsed form when value is CSV text."""
        if blank(value):
            return None
        name = "Latitude" if limit == 90 else "Longitude"
        v = number if number is not None else value if ad.is_number(value) else None
        if v is None:
            self.add("EC-012", table, rid, field, value, f"{name} is not a number")
        elif not -limit <= v <= limit:
            self.add("EC-012", table, rid, field, value, f"{name} out of range [-{limit}, {limit}]")
            v = None
        return v

    def asset(self, table, rid, field, rel):
        """EC-031: a referenced asset file exists."""
        if not blank(rel) and not self.ds.path(rel).is_file():
            self.add("EC-031", table, rid, field, rel, "Referenced file does not exist")

    def unique(self, seen, check_id, table, rid, field, value, what="id", where=None):
        """EC-021 / EC-022: report every repeat of a key after its first occurrence (where: how to name the first)."""
        if blank(value):
            return
        if value in seen:
            self.add(check_id, table, rid, field, value, f"Duplicate {what} (first used by {seen[value]})")
        else:
            seen[value] = where or rid

    # ----- data/cities.js: COUNTRIES, REGIONS, US_STATES, SWISS_CANTONS -----

    def reference_tables(self):
        if not self.ds.ok("cities"):
            return
        iso2s, isos = {}, {}
        for name, c in self.ds.countries.items():
            if not isinstance(c, dict):
                self.add("EC-001", ad.FILES["cities"], name, "", c, "COUNTRIES entry is not an object")
                continue
            self.required("EC-005", "countries", name, c, ("iso2", "iso"))
            if self.pattern("countries", name, "iso2", c.get("iso2"), ISO2, "ISO 3166-1 alpha-2 code (lower case)"):
                self.unique(iso2s, "EC-021", "countries", name, "iso2", c["iso2"], "country code")
            if self.pattern("countries", name, "iso", c.get("iso"), ISO_NUM, "ISO 3166-1 numeric code (3 digits, as text)"):
                self.unique(isos, "EC-021", "countries", name, "iso", c["iso"], "country code")
        names, slugs = {}, {}
        for n, r in enumerate(self.ds.regions, 1):
            if not isinstance(r, dict):
                self.add("EC-001", ad.FILES["cities"], f"REGIONS[{n}]", "", r, "REGIONS entry is not an object")
                continue
            rid = r.get("name") or f"REGIONS[{n}]"
            self.required("EC-005", "regions", rid, r, ("name", "slug"))
            self.unique(names, "EC-021", "regions", rid, "name", r.get("name"), "region name")
            if self.pattern("regions", rid, "slug", r.get("slug"), SLUG, "slug"):
                self.unique(slugs, "EC-021", "regions", rid, "slug", r["slug"], "region slug")
        for const, table in (("US_STATES", "us_states"), ("SWISS_CANTONS", "swiss_cantons")):
            for name, s in self.ds.js("cities", const).items():
                code = s.get("code") if isinstance(s, dict) else None
                if blank(code):
                    self.add("EC-005", table, name, "code", code, "Required field is missing")
                else:
                    self.pattern(table, name, "code", code, SUBDIVISION, "two-letter code")

    # ----- data/cities.js: CITIES and their photos -----

    def country_extents(self):
        """ISO2 (upper case) → [min lat, min lng, max lat, max lng] of the country's airports (EC-038 reference)."""
        ext = {}
        for a in self.ds.rows("airports"):
            lat, lng = to_number(a["lat"]), to_number(a["lng"])
            if lat is None or lng is None:
                continue
            b = ext.setdefault(a["country"], [lat, lng, lat, lng])
            b[0], b[1], b[2], b[3] = min(b[0], lat), min(b[1], lng), max(b[2], lat), max(b[3], lng)
        return ext

    def locations(self):
        ds = self.ds
        if not ds.ok("cities"):
            return
        T = "locations"
        countries, rolls_ok = ds.countries, ds.ok("rolls")
        region_names = {r.get("name") for r in ds.regions if isinstance(r, dict)}
        us_states, cantons = ds.js("cities", "US_STATES"), ds.js("cities", "SWISS_CANTONS")
        roll_ids = {r["roll"] for r in ds.rows("rolls")}
        extents = self.country_extents()
        ids, names = {}, {}
        for n, c in enumerate(ds.cities, 1):
            if not isinstance(c, dict):
                self.add("EC-001", ad.FILES["cities"], f"CITIES[{n}]", "", c, "CITIES entry is not an object")
                continue
            lid = c.get("id") if isinstance(c.get("id"), str) and c.get("id") else None
            rid = lid or f"CITIES[{n}]"
            for k in c:                                                               # EC-004
                if k not in LOCATION_FIELDS:
                    self.add("EC-004", T, rid, k, c[k], "Unknown field (typo?); the site ignores it")
            self.required("EC-005", T, rid, c, ("id", "en", "lat", "lng", "region"))   # EC-005
            clist = c.get("countries") if isinstance(c.get("countries"), list) else \
                ([c["country"]] if not blank(c.get("country")) else [])
            if not clist:
                self.add("EC-005", T, rid, "country", "", "Required field is missing (country or countries)")
            if lid:
                self.pattern(T, rid, "id", lid, SLUG, "slug (lower-case letters, digits, single hyphens)")  # EC-011
                self.unique(ids, "EC-021", T, rid, "id", lid, where=f"CITIES[{n}]")                        # EC-021
                self.location_ids.add(lid)
            if isinstance(c.get("en"), str):
                self.unique(names, "EC-022", T, rid, "en", c["en"], "English name")                         # EC-022
            lat = self.coordinate(T, rid, "lat", c.get("lat"), 90)                                         # EC-012
            lng = self.coordinate(T, rid, "lng", c.get("lng"), 180)
            hue = c.get("hue")                                                                             # EC-015
            if hue is not None and not (isinstance(hue, int) and not isinstance(hue, bool) and 0 <= hue <= 359):
                self.add("EC-015", T, rid, "hue", hue, "Hue is not an integer 0–359")
            size = c.get("size")                                                                           # EC-016
            if not blank(size) and size not in SIZES:
                self.add("EC-016", T, rid, "size", size, "Size is not big or small")
            region = c.get("region")                                                                       # EC-017
            if not blank(region) and region not in region_names:
                self.add("EC-017", T, rid, "region", region, "Region is not in REGIONS")
            seen_countries = {}
            for country in clist:
                self.unique(seen_countries, "EC-021", T, rid, "country", country, "country")               # EC-021
                if not blank(country) and country not in countries:                                        # EC-026
                    self.add("EC-026", T, rid, "country", country, "Country is not defined in COUNTRIES")
            # EC-020 / EC-031: subdivisions and their flags
            states = ([c["state"]] if "state" in c else []) + (c["states"] if isinstance(c.get("states"), list) else [])
            for s in states:
                if s not in us_states:
                    self.add("EC-020", T, rid, "state", s, "Not a key of US_STATES")
                elif "United States" not in clist:
                    self.add("EC-020", T, rid, "state", s, "US state on a location outside the United States")
                elif isinstance(us_states[s], dict) and us_states[s].get("code"):
                    self.asset(T, rid, "state", f"assets/flags/us/{us_states[s]['code']}.svg")
            if "canton" in c:
                s = c["canton"]
                if s not in cantons:
                    self.add("EC-020", T, rid, "canton", s, "Not a key of SWISS_CANTONS")
                elif "Switzerland" not in clist:
                    self.add("EC-020", T, rid, "canton", s, "Swiss canton on a location outside Switzerland")
                elif isinstance(cantons[s], dict) and cantons[s].get("code"):
                    self.asset(T, rid, "canton", f"assets/flags/ch/{cantons[s]['code']}.svg")
            self.asset(T, rid, "flag", c.get("flag"))                                                      # EC-031
            # EC-034: small ⇔ parent; never its own parent
            eff = "big" if blank(size) else size
            parent = c.get("parent")
            if eff == "small" and blank(parent):
                self.add("EC-034", T, rid, "parent", parent, "Small location has no parent")
            elif eff == "big" and not blank(parent):
                self.add("EC-034", T, rid, "parent", parent, "Big location must not have a parent")
            if not blank(parent) and parent == lid:
                self.add("EC-034", T, rid, "parent", parent, "Location is its own parent")
            # EC-038: coordinates inside the country's reference extent (only when EC-012 passed)
            boxes = [extents[countries[x]["iso2"].upper()] for x in clist
                     if isinstance(countries.get(x), dict) and isinstance(countries[x].get("iso2"), str)
                     and countries[x]["iso2"].upper() in extents]
            if lat is not None and lng is not None and boxes:
                m = EXTENT_MARGIN_DEG
                inside = lambda la, ln: any(b[0] - m <= la <= b[2] + m and b[1] - m <= ln <= b[3] + m for b in boxes)
                if not inside(lat, lng):
                    hint = ("latitude and longitude look swapped" if inside(lng, lat) else
                            "the latitude's sign looks wrong" if inside(-lat, lng) else
                            "the longitude's sign looks wrong" if inside(lat, -lng) else "")
                    self.add("EC-038", T, rid, "lat/lng", f"{lat},{lng}",
                             f"Outside the reference extent of {' / '.join(clist)}" + (f": {hint}" if hint else ""))
            self.photos(c, lid, rid, roll_ids if rolls_ok else None)
        for n, r in enumerate(ds.regions, 1):                                                              # EC-022
            if isinstance(r, dict) and r.get("slug") in self.location_ids:
                self.add("EC-022", "regions", r.get("name") or f"REGIONS[{n}]", "slug", r["slug"],
                         "Region slug equals a location id (both are URL hashes)")

    def photos(self, c, lid, rid, roll_ids):
        photos = c.get("photos", [])
        if not isinstance(photos, list):
            self.add("EC-001", ad.FILES["cities"], rid, "photos", photos, "photos is not an array")
            return
        T, seen = "photos", {}
        files = self.photo_files.setdefault(lid or rid, set())
        for j, p in enumerate(photos, 1):
            if isinstance(p, str):
                p = {"file": p}
            elif isinstance(p, dict):
                for k in p:                                                                       # EC-004
                    if k not in PHOTO_FIELDS:
                        self.add("EC-004", T, f"{rid}/{p.get('file') or '#' + str(j)}", k, p[k], "Unknown field (typo?)")
            else:
                self.add("EC-001", ad.FILES["cities"], rid, "photos", p, "Photo is neither a file name nor an object")
                continue
            file = p.get("file")
            prid = f"{rid}/{file if not blank(file) else '#' + str(j)}"
            self.required("EC-008", T, prid, p, ("file",))                                       # EC-008
            ok = self.pattern(T, prid, "file", file, PHOTO_FILE, "photo file name (NN.jpg)")      # EC-011
            if p.get("scan") is not None:
                self.pattern(T, prid, "scan", str(p["scan"]), SCAN, "scan number")
            self.unique(seen, "EC-021", T, prid, "file", file, "photo file")                       # EC-021
            if not blank(file):
                files.add(file)
                if lid and isinstance(file, str) and not self.ds.path(f"photos/{lid}/{file}").is_file():   # EC-025
                    self.add("EC-025", T, prid, "file", file, f"File photos/{lid}/{file} does not exist")
            roll = p.get("roll")                                                                  # EC-045
            if p.get("film") is True and blank(roll):
                self.add("EC-045", T, prid, "roll", roll, "Film photo has no roll")
            elif p.get("film") is not True and not blank(roll):
                self.add("EC-045", T, prid, "roll", roll, "Photo has a roll but is not marked film:true")
            if not blank(roll) and roll_ids is not None and roll not in roll_ids:                 # EC-027
                self.add("EC-027", T, prid, "roll", roll, "Roll is not defined in film/rolls.csv")

    # ----- data/articles.js -----

    def articles(self):
        if not self.ds.ok("articles"):
            return
        for lid, arts in self.ds.js("articles", "ARTICLES").items():
            if self.ds.ok("cities") and lid not in self.location_ids:                               # EC-032
                self.add("EC-032", "articles", lid, "location", lid, "Article location is not defined in data/cities.js")
            if not isinstance(arts, list):
                self.add("EC-001", ad.FILES["articles"], lid, "", arts, "Articles of a location are not an array")
                continue
            for a in arts:
                if not isinstance(a, dict):
                    self.add("EC-001", ad.FILES["articles"], lid, "", a, "Article is not an object")
                    continue
                rid = f"{lid}/{a.get('slug') or a.get('date') or a.get('title')}"
                self.date("articles", rid, "date", a.get("date"))                                   # EC-009 / EC-014
                cover = a.get("cover")
                if not blank(cover) and lid in self.location_ids and cover not in self.photo_files.get(lid, ()):  # EC-032
                    self.add("EC-032", "articles", rid, "cover", cover, "Cover is not one of the location's photos")

    # ----- film/rolls.csv -----

    def film_rolls(self):
        T, ids, stock_iso = "film_rolls", {}, {}
        for r in self.ds.rows("rolls"):
            rid = r["roll"] or f"line {r['_line']}"
            self.required("EC-008", T, rid, r, ("roll", "film_stock", "iso", "format"))       # EC-008
            self.pattern(T, rid, "roll", r["roll"], ROLL_ID, "roll id (stock + ISO + -NN)")   # EC-011
            self.unique(ids, "EC-021", T, rid, "roll", r["roll"], where=f"line {r['_line']}")   # EC-021
            iso = int(r["iso"]) if re.fullmatch(r"\d+", r["iso"]) else None                   # EC-015
            if not blank(r["iso"]) and (iso is None or not 1 <= iso <= 6400):
                self.add("EC-015", T, rid, "iso", r["iso"], "ISO is not a whole number 1–6400")
                iso = None
            gauge = r["format"].split()[0] if r["format"] else ""                            # EC-019
            if gauge and gauge not in GAUGES:
                self.add("EC-019", T, rid, "format", r["format"], f"Gauge {gauge!r} is not 135 or 120")
            d_from = self.date(T, rid, "date_from", r["date_from"])                           # EC-009 / EC-014
            d_to = self.date(T, rid, "date_to", r["date_to"])
            if d_from and d_to and d_from > d_to:                                             # EC-047
                self.add("EC-047", T, rid, "date_to", r["date_to"], f"Roll ends before it starts ({r['date_from']})")
            self.asset(T, rid, "box_image", r["box_image"])                                   # EC-031
            stock = r["film_stock"]                                                          # EC-046
            if iso is not None and not blank(stock):
                speeds = re.findall(r"(?<![\w.])(\d{2,4})(?![\w.])", stock)
                if speeds and str(iso) not in speeds:
                    self.add("EC-046", T, rid, "iso", r["iso"], f"ISO differs from the speed in the stock name {stock!r}")
                if stock in stock_iso and stock_iso[stock][0] != iso:
                    self.add("EC-046", T, rid, "iso", r["iso"],
                             f"ISO differs from roll {stock_iso[stock][1]} of the same stock (ISO {stock_iso[stock][0]})")
                stock_iso.setdefault(stock, (iso, rid))
            if r["roll"]:
                self.roll_ids.add(r["roll"])

    # ----- flights/flights.csv and the airports it uses -----

    def flights(self):
        T, ids = "flights", {}
        refs = {}
        for a in self.ds.rows("airports"):
            self.unique(refs, "EC-021", "airports", a["iata"], "iata", a["iata"], "airport code")  # EC-021
            self.airports.setdefault(a["iata"], a)
        used = set()
        # EC-040: the local departure time that tools/build_flights.py computed from the airport's time zone
        dep_local = {}
        for f in (self.ds.data.get("flights_geo") or {}).get("features", []):
            p = f.get("properties") if isinstance(f, dict) else None
            t = parse_datetime(p.get("dep_local")) if isinstance(p, dict) else None
            if t and isinstance(p.get("id"), str):
                dep_local[p["id"]] = t.date()
        for r in self.ds.rows("flights"):
            rid = r["id"] or f"line {r['_line']}"
            self.required("EC-006", T, rid, r, ("id", "date", "flight_no", "from_iata", "to_iata", "dep_utc", "arr_utc"))
            self.pattern(T, rid, "id", r["id"], FLIGHT_ID, "flight id (flight number + _ + 8 hex digits)")   # EC-011
            self.pattern(T, rid, "flight_no", r["flight_no"], FLIGHT_NO, "flight number")
            self.pattern(T, rid, "airline_iata", r["airline_iata"], AIRLINE, "airline code")
            for f in ("from_iata", "to_iata"):
                if self.pattern(T, rid, f, r[f], IATA, "IATA airport code"):
                    used.add(r[f])
                    if self.ds.ok("airports") and r[f] not in self.airports:                 # EC-028
                        self.add("EC-028", T, rid, f, r[f], "Airport is not in tools/airports.csv")
            self.unique(ids, "EC-021", T, rid, "id", r["id"], where=f"line {r['_line']}")     # EC-021
            if not blank(r["show"]) and r["show"] not in SHOW:                                 # EC-018
                self.add("EC-018", T, rid, "show", r["show"], "show is not yes, no or empty")
            d = self.date(T, rid, "date", r["date"])                                           # EC-009 / EC-014
            dep = self.datetime(T, rid, "dep_utc", r["dep_utc"])                               # EC-010 / EC-014
            arr = self.datetime(T, rid, "arr_utc", r["arr_utc"])
            if dep and arr and arr <= dep:                                                     # EC-039
                self.add("EC-039", T, rid, "arr_utc", r["arr_utc"], f"Arrives before it departs ({r['dep_utc']})")
            if not blank(r["from_iata"]) and r["from_iata"] == r["to_iata"]:
                self.add("EC-039", T, rid, "to_iata", r["to_iata"], "Lands at the airport it left")
            local = dep_local.get(r["id"])                                                     # EC-040
            if d and local and d != local:
                self.add("EC-040", T, rid, "date", r["date"],
                         f"Not the local departure date {local} (dep_local in {ad.FILES['flights_geo']})")
            if not blank(r["airline_iata"]) and not blank(r["flight_no"]) and r["airline_iata"] != r["flight_no"][:2]:
                self.add("EC-040", T, rid, "airline_iata", r["airline_iata"], f"Not the start of flight number {r['flight_no']}")
            if not blank(r["id"]) and not blank(r["flight_no"]) and not r["id"].startswith(r["flight_no"] + "_"):
                self.add("EC-040", T, rid, "id", r["id"], f"Does not start with flight number {r['flight_no']}_")
        for code in sorted(used & set(self.airports)):                                         # EC-012
            a = self.airports[code]
            for f, limit in (("lat", 90), ("lng", 180)):
                if blank(a[f]):
                    self.add("EC-012", "airports", code, f, "", "Reference position is missing")
                self.coordinate("airports", code, f, a[f], limit, to_number(a[f]))

    # ----- routes/routes.csv and routes/rail.csv -----

    def routes(self):
        T, ids = "routes", {}
        for r in self.ds.rows("routes"):
            rid = r["id"] or f"line {r['_line']}"
            self.required("EC-007", T, rid, r, ("id", "mode", "from", "to"))                   # EC-007
            self.pattern(T, rid, "id", r["id"], SLUG, "slug (lower-case letters, digits, single hyphens)")  # EC-011
            self.pattern(T, rid, "duration", r["duration"], DURATION, "duration (H:MM or minutes)")
            self.unique(ids, "EC-021", T, rid, "id", r["id"], where=f"line {r['_line']}")     # EC-021
            if not blank(r["mode"]) and r["mode"] not in MODES:                                # EC-018
                self.add("EC-018", T, rid, "mode", r["mode"], "Mode is not rail, drive or water")
            if not blank(r["show"]) and r["show"] not in SHOW:
                self.add("EC-018", T, rid, "show", r["show"], "show is not yes, no or empty")
            if blank(r["date"]):                                                               # EC-053
                self.add("EC-053", T, rid, "date", "", "Route has no date")
            self.route_dates[r["id"]] = self.date(T, rid, "date", r["date"])                   # EC-009 / EC-014
            if not blank(r["distance_km"]):                                                    # EC-015
                d = to_number(r["distance_km"])
                if d is None or not 0 < d <= 20000:
                    self.add("EC-015", T, rid, "distance_km", r["distance_km"], "Distance is not a number in (0, 20000] km")
            for stop in r["via"].split("|") if r["via"] else []:                              # EC-012
                m = re.search(r"@\s*([^,]*),\s*(.*)$", stop)
                if m:
                    for txt, limit in ((m.group(1), 90), (m.group(2), 180)):
                        v = to_number(txt)
                        if v is None or not -limit <= v <= limit:
                            name = "latitude" if limit == 90 else "longitude"
                            self.add("EC-012", T, rid, "via", stop.strip(),
                                     f"Stop {name} is not a number in [-{limit}, {limit}] (via stops are name@lat,lng)")
            if not blank(r["id"]):
                self.route_modes[r["id"]] = r["mode"]
        T, ids = "rail_details", {}
        for r in self.ds.rows("rail"):
            rid = r["id"] or f"line {r['_line']}"
            self.required("EC-007", T, rid, r, ("id",))                                        # EC-007
            self.pattern(T, rid, "id", r["id"], SLUG, "slug (lower-case letters, digits, single hyphens)")  # EC-011
            self.pattern(T, rid, "operator_code", r["operator_code"], OPERATOR, "operator code")
            self.unique(ids, "EC-021", T, rid, "id", r["id"], where=f"line {r['_line']}")     # EC-021
            if not blank(r["id"]) and self.ds.ok("routes"):                                    # EC-030
                if r["id"] not in self.route_modes:
                    self.add("EC-030", T, rid, "id", r["id"], "No route with this id in routes/routes.csv")
                elif self.route_modes[r["id"]] != "rail":
                    self.add("EC-030", T, rid, "id", r["id"], f"Route is a {self.route_modes[r['id']] or '(no mode)'} route, not rail")
            dep = self.datetime(T, rid, "dep_local", r["dep_local"])                           # EC-010 / EC-014
            arr = self.datetime(T, rid, "arr_local", r["arr_local"])
            if dep and arr and arr <= dep:                                                     # EC-047
                self.add("EC-047", T, rid, "arr_local", r["arr_local"], f"Arrives before it departs ({r['dep_local']})")
            route_date = self.route_dates.get(r["id"])
            if dep and route_date and dep.date() != route_date:
                self.add("EC-047", T, rid, "dep_local", r["dep_local"], f"Departure date differs from the route's date {route_date}")

    # ----- GeoJSON -----

    def position(self, p):
        return isinstance(p, list) and 2 <= len(p) <= 3 and all(ad.is_number(x) for x in p)

    def geojson(self, key):
        """EC-003 / EC-013 / EC-021 / EC-031 per feature; returns {id: (properties, lines or None if unusable)}."""
        doc, rel = self.ds.data.get(key), ad.FILES[key]
        if doc is None:
            return None
        feats, ids = {}, {}
        in_range = lambda p: -180 <= p[0] <= 180 and -90 <= p[1] <= 90
        for n, f in enumerate(doc["features"], 1):
            props = f.get("properties") if isinstance(f, dict) else None
            fid = props.get("id") if isinstance(props, dict) and not blank(props.get("id")) else None
            rid = fid or f"feature {n}"
            geom = f.get("geometry") if isinstance(f, dict) else None
            gtype = geom.get("type") if isinstance(geom, dict) else None
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            lines = [coords] if gtype == "LineString" else coords if gtype == "MultiLineString" else None
            problem = None                                                                     # EC-003
            if not isinstance(f, dict) or f.get("type") != "Feature":
                problem = ("type", f.get("type") if isinstance(f, dict) else f, "Not a GeoJSON Feature")
            elif fid is None:
                problem = ("properties.id", "", "Feature has no properties.id")
            elif gtype not in ("LineString", "MultiLineString"):
                problem = ("geometry.type", gtype, "Geometry is not a LineString or MultiLineString")
            elif not isinstance(lines, list) or not lines or not all(isinstance(l, list) for l in lines):
                problem = ("coordinates", "", "Coordinates are not an array of lines")
            elif any(len(l) < 2 for l in lines):
                problem = ("coordinates", min(len(l) for l in lines), "A line has fewer than 2 positions")
            elif not all(self.position(p) for l in lines for p in l):
                bad = next(p for l in lines for p in l if not self.position(p))
                problem = ("coordinates", bad, "A position is not 2–3 numbers")
            if problem:
                self.add("EC-003", rel, rid, *problem)
                if fid:
                    feats[fid] = (props, None)
                continue
            self.unique(ids, "EC-021", rel, rid, "id", fid, "feature id", f"feature {n}")    # EC-021
            positions = [p for l in lines for p in l]                                          # EC-013
            bad = [p for p in positions if not in_range(p)]
            usable = not bad
            if bad:
                swapped = all(in_range([p[1], p[0]]) for p in bad)
                self.add("EC-013", rel, rid, "coordinates", bad[0],
                         f"{len(bad)} of {len(positions)} positions out of range for [lon, lat]"
                         + ("; they look like [lat, lon]" if swapped else ""))
            for field in ("from_pos", "to_pos"):
                p = props.get(field)
                if p is not None and not (self.position(p) and in_range(p)):
                    swapped = self.position(p) and in_range([p[1], p[0]])
                    self.add("EC-013", rel, rid, field, p, "Not a [lon, lat] position in range"
                             + ("; it looks like [lat, lon]" if swapped else ""))
            for field in ("logo", "logo_webp"):                                                # EC-031
                self.asset(rel, rid, field, props.get(field))
            if isinstance(props.get("rail"), dict):
                for field in ("logo", "logo_webp"):
                    self.asset(rel, rid, f"rail.{field}", props["rail"].get(field))
            feats[fid] = (props, lines if usable else None)
        return feats

    def sync(self, feats, key, csv_key, fields, builder):
        """EC-033: the GeoJSON has exactly one feature per shown CSV row, with the same key fields."""
        if feats is None or not self.ds.ok(csv_key):
            return
        rel = ad.FILES[key]
        rows = {r["id"]: r for r in self.ds.rows(csv_key) if not blank(r["id"])}
        shown = {i: r for i, r in rows.items() if r["show"].strip().lower() not in HIDDEN}
        for i in shown:
            if i not in feats:
                self.add("EC-033", rel, i, "id", i, f"Row of {ad.FILES[csv_key]} has no feature; rerun {builder}")
        for i, (props, _) in feats.items():
            if i not in shown:
                self.add("EC-033", rel, i, "id", i, f"Feature has no shown row in {ad.FILES[csv_key]}; rerun {builder}")
                continue
            for f in fields:
                want, got = shown[i][f], props.get(f)
                if not blank(want) and want != ad.fmt_value(got):
                    self.add("EC-033", rel, i, f, got, f"Differs from {ad.FILES[csv_key]} ({want}); rerun {builder}")

    def ends(self, rel, fid, lines, targets, limit):
        """EC-044: the first / last track position lie within limit km of their targets [(label, lat, lng)]."""
        first, last = lines[0][0], lines[-1][-1]
        far = []
        for (label, lat, lng), p in zip(targets, (first, last)):
            if lat is None or lng is None:
                continue
            d = ad.haversine_km(p[1], p[0], lat, lng)
            if d > limit:
                far.append((label, d, ad.haversine_km(p[0], p[1], lat, lng) <= limit))
        if far and all(sw for _, _, sw in far) and len(far) == 2:
            self.add("EC-044", rel, fid, "coordinates", f"{first}…{last}",
                     "Track ends are far from both ends ("
                     + ", ".join(f"{label} {d:.0f} km" for label, d, _ in far)
                     + f"); the positions look like [lat, lon]")
        else:
            for label, d, _ in far:
                self.add("EC-044", rel, fid, "coordinates", first if label == targets[0][0] else last,
                         f"Track {'start' if label == targets[0][0] else 'end'} is {d:.0f} km from {label} (limit {limit} km)")

    def flights_geojson(self, feats):
        self.sync(feats, "flights_geo", "flights", ("flight_no", "from_iata", "to_iata", "dep_utc", "arr_utc"),
                  "tools/build_flights.py")
        rel = ad.FILES["flights_geo"]
        for fid, (props, lines) in (feats or {}).items():
            if lines is None:
                continue
            targets = []
            for field, pos in (("from_iata", "from_pos"), ("to_iata", "to_pos")):
                a = self.airports.get(props.get(field))
                lat, lng = (to_number(a["lat"]), to_number(a["lng"])) if a else (None, None)
                targets.append((props.get(field), lat, lng))
                p = props.get(pos)
                if lat is not None and lng is not None and self.position(p):
                    d = ad.haversine_km(p[1], p[0], lat, lng)
                    if d > FLIGHT_POS_KM:
                        self.add("EC-044", rel, fid, pos, p,
                                 f"{pos} is {d:.0f} km from airport {props.get(field)} (limit {FLIGHT_POS_KM} km)")
            self.ends(rel, fid, lines, targets, FLIGHT_END_KM)

    def routes_geojson(self, feats):
        self.sync(feats, "routes_geo", "routes", ("mode", "date", "from", "to"), "tools/build_routes.py")
        rel = ad.FILES["routes_geo"]
        rows = {r["id"]: r for r in self.ds.rows("routes")}
        for fid, (props, lines) in (feats or {}).items():
            if lines is None:
                continue
            targets = []
            for field in ("from_pos", "to_pos"):
                p = props.get(field)
                ok = self.position(p) and -180 <= p[0] <= 180 and -90 <= p[1] <= 90
                targets.append((field, p[1] if ok else None, p[0] if ok else None))
            self.ends(rel, fid, lines, targets, ROUTE_END_KM)
            stated = to_number(rows[fid]["distance_km"]) if fid in rows else None              # EC-048
            length = track_km(lines)
            if stated and length > 0 and abs(stated - length) / length > DISTANCE_TOLERANCE:
                self.add("EC-048", "routes", fid, "distance_km", rows[fid]["distance_km"],
                         f"Stated distance differs from the track length {length:.0f} km by more than 25%")

    # ----- files on disk: orphans, image metadata, denylist -----

    def files(self):
        ds = self.ds
        photos = ds.path("photos")
        if ds.ok("cities") and photos.is_dir():                                                # EC-050
            for d in sorted(photos.iterdir()):
                if d.name.startswith("."):
                    continue
                if not d.is_dir():
                    self.add("EC-050", "photos", d.name, "", "", "File outside a location folder")
                elif d.name not in self.location_ids:
                    self.add("EC-050", "photos", d.name + "/", "", "", "Folder does not belong to any location")
                else:
                    for f in sorted(d.iterdir()):
                        if not f.name.startswith(".") and f.name not in self.photo_files.get(d.name, ()):
                            self.add("EC-050", "photos", f"{d.name}/{f.name}", "", "", "File is not listed in data/cities.js")
        if not self.denylist:
            self.skipped.append(("EC-055", f"no privacy denylist ({ad.DENYLIST_FILE} not found and "
                                           f"${ad.DENYLIST_ENV} not set)"))
        for rel in published_files(ds.dir):
            ext = rel.suffix.lower()
            if ext in IMAGE_EXT:                                                               # EC-054
                try:
                    kinds = image_metadata(ds.path(rel).read_bytes(), ext)
                except ValueError as e:
                    kinds = [f"unreadable ({e}), so it cannot be checked"]
                if kinds:
                    self.add("EC-054", "images", self.denylist.redact(rel.as_posix()), "metadata", ", ".join(kinds),
                             "Image carries embedded metadata; strip it before publishing")
            if not self.denylist:
                continue
            shown = self.denylist.redact(rel.as_posix())                                       # EC-055
            if shown != rel.as_posix():
                self.add("EC-055", shown, "path", "", "***", "Privacy denylist term")
            if ext in BINARY_EXT:
                continue
            try:
                text = ds.path(rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if self.denylist.spans(line):
                    self.add("EC-055", shown, f"line {n}", "", "***", "Privacy denylist term")


# ---------- layer 3: SQL query checks ----------

def run_sql_checks(db_path, plan):
    findings = []
    conn = load_sqlite.connect(db_path)
    try:
        for path in sorted(CHECKS_DIR.glob("EC-*.sql")):
            cid = path.name[:6]
            if cid not in plan:
                raise ConfigError(f"{path.name}: {cid} is not in the validation plan")
            cur = conn.execute(path.read_text(encoding="utf-8"))
            cols = [d[0] for d in cur.description]
            if cols != QUERY_COLUMNS:
                raise ConfigError(f"{path.name}: returns columns {cols}, expected {QUERY_COLUMNS}")
            for row in cur:
                r = dict(zip(cols, row))
                if r["check_id"] != cid or r["severity"] != plan[cid].severity:
                    raise ConfigError(f"{path.name}: returns {r['check_id']} / {r['severity']}, but the plan says "
                                      f"{cid} / {plan[cid].severity}")
                findings.append(Finding(cid, r["table_name"], r["record_id"], r["field"] or "",
                                        ad.fmt_value(r["value"]), r["message"], "sql"))
    finally:
        conn.close()
    return findings


# ---------- run and report ----------

class Result:
    def __init__(self, data_dir, out_dir, plan, findings, load, skipped, denylist, counts):
        self.data_dir, self.out_dir, self.plan = data_dir, out_dir, plan
        self.findings, self.load, self.skipped, self.denylist, self.counts = findings, load, skipped, denylist, counts
        self.errors = [f for f in findings if f.severity == "error"]
        self.warnings = [f for f in findings if f.severity == "warning"]
        self.exit_code = 1 if self.errors else 0

    def ids(self, severity=None):
        return {f.check_id for f in self.findings if severity in (None, f.severity)}

    def not_loaded_note(self):
        n = sum(s["rejected"] + s["skipped"] for s in self.load.stats.values())
        return (f"{n} row{'s' * (n != 1)} could not be loaded into SQLite, so SQL query warnings (a place or roll "
                f"without photos, say) may only be side effects of those errors; fix the errors and run again") if n else ""

    def skipped_by_reason(self):
        groups = {}
        for cid, why in self.skipped:
            groups.setdefault(why, []).append(cid)
        return list(groups.items())

    def summary(self):
        return (f"{len(self.errors)} error{'s' * (len(self.errors) != 1)}, "
                f"{len(self.warnings)} warning{'s' * (len(self.warnings) != 1)} — "
                + ("FAILED" if self.errors else "PASSED"))

    @staticmethod
    def shown(path):
        """path relative to the working directory when it lies inside it, absolute otherwise."""
        try:
            rel = os.path.relpath(path)
        except ValueError:
            return str(path)
        return str(path) if rel.startswith("..") else rel

    def where(self):
        return self.shown(self.data_dir)

    def terminal(self):
        s = self.load.stats
        lines = [f"Travel Atlas data validation · {len(self.plan)} checks · data: {self.where()}",
                 f"  Python checks     {self.counts['python']} findings",
                 f"  SQLite load       {sum(t['loaded'] for t in s.values())} rows loaded, "
                 f"{sum(t['rejected'] for t in s.values())} rejected, {sum(t['skipped'] for t in s.values())} skipped",
                 f"  SQL query checks  {self.counts['queries']} queries, {self.counts['sql']} findings", ""]
        for title, group in (("Errors", self.errors), ("Warnings", self.warnings)):
            lines.append(f"{title} ({len(group)})")
            lines += [f.line() for f in group] or ["  none"]
            lines.append("")
        for why, ids in self.skipped_by_reason():
            lines.append(f"WARNING: {', '.join(ids)} skipped: {why}")
        if self.not_loaded_note():
            lines.append(f"NOTE: {self.not_loaded_note()}")
        lines.append(f"Report: {self.shown(self.out_dir / 'validation_report.md')}, "
                     f"{self.shown(self.out_dir / 'validation_report.csv')}")
        lines.append(self.summary())
        return self.denylist.redact("\n".join(lines))

    def markdown(self):
        cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ") if v not in (None, "") else ""
        commit = ""
        try:
            commit = subprocess.run(["git", "-C", str(self.data_dir), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        skipped = {cid for cid, _ in self.skipped}
        out = ["# Data validation report", "",
               f"**Result: {'FAILED' if self.errors else 'PASSED'}** — {len(self.errors)} errors, "
               f"{len(self.warnings)} warnings · {len(self.plan)} checks ({len(self.plan) - len(skipped)} run, "
               f"{len(skipped)} skipped)", "",
               "| | |", "|---|---|",
               f"| Run at | {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} |",
               f"| Data | `{self.where()}` |",
               f"| Commit | {commit or 'n/a'} |",
               f"| Validation plan | docs/validation_plan.md ({len(self.plan)} checks) |",
               f"| Python / SQLite | {platform.python_version()} / {sqlite3.sqlite_version} |", ""]
        for title, group in (("Errors", self.errors), ("Warnings", self.warnings)):
            out += [f"## {title} ({len(group)})", ""]
            if not group:
                out += ["None.", ""]
                continue
            out += ["| Check | Table | Record | Field | Value | Message | Detected by |", "|---|---|---|---|---|---|---|"]
            out += [f"| {f.check_id} | {cell(f.table)} | {cell(f.record_id)} | {cell(f.field)} | {cell(f.value)} | "
                    f"{cell(f.message)} | {', '.join(f.sources)} |" for f in group]
            out.append("")
        hits = {}
        for f in self.findings:
            hits[f.check_id] = hits.get(f.check_id, 0) + 1
        out += ["## Check summary", "", "| Check | Category | Severity | Result | Implemented in |", "|---|---|---|---|---|"]
        for c in self.plan.values():
            result = f"**{hits[c.id]}**" if c.id in hits else "skipped" if c.id in skipped else "pass"
            out.append(f"| {c.id} | {c.category} | {c.severity} | {result} | {c.implemented_in} |")
        out += ["", "## SQLite load", "", "| Table | Rows | Loaded | Rejected | Skipped |", "|---|---|---|---|---|"]
        for table, s in self.load.stats.items():
            out.append(f"| {table} | {s['rows']} | {s['loaded']} | {s['rejected']} | {s['skipped']} |")
        out += ["", "Rejected rows are listed above with `sqlite` under *Detected by*. Skipped rows come from, or point "
                    "to, a rejected row and are not reported again.", ""]
        if self.skipped or self.load.not_loaded or self.not_loaded_note():
            out += ["## Run notes", ""]
            out += [f"- {', '.join(ids)} skipped: {why}" for why, ids in self.skipped_by_reason()]
            if self.not_loaded_note():
                out.append(f"- {self.not_loaded_note()}")
            out += [f"- `{p}` was not loaded into SQLite (fails a structure check)" for p in sorted(set(self.load.not_loaded))]
            out.append("")
        return self.denylist.redact("\n".join(out))

    def csv_text(self):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["check_id", "severity", "category", "table_name", "record_id", "field", "value", "message", "detected_by"])
        for f in self.findings:
            w.writerow([f.check_id, f.severity, f.category, f.table, f.record_id, f.field, f.value, f.message,
                        "+".join(f.sources)])
        return self.denylist.redact(buf.getvalue())

    def write_reports(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "validation_report.md").write_text(self.markdown(), encoding="utf-8")
        (self.out_dir / "validation_report.csv").write_text(self.csv_text(), encoding="utf-8")


def merge(findings, plan, denylist):
    """One finding per discrepancy (check, table, record, field, value); severity and category from the plan."""
    merged = {}
    for f in findings:
        if f.check_id not in plan:
            raise ConfigError(f"{f.check_id} is used in code but not defined in the validation plan")
        for attr in ("table", "record_id", "field", "value", "message"):
            setattr(f, attr, denylist.redact(getattr(f, attr)))
        if f.key() in merged:
            m = merged[f.key()]
            m.sources += [s for s in f.sources if s not in m.sources]
        else:
            f.severity, f.category = plan[f.check_id].severity, plan[f.check_id].category
            merged[f.key()] = f
    order = {"error": 0, "warning": 1}
    return sorted(merged.values(), key=lambda f: (order[f.severity], f.check_id, f.table, f.record_id, f.field))


def validate(data_dir=ad.ROOT, out_dir=None, denylist_path=None, use_env=True, today=None):
    """Run the three layers on a site directory and return a Result (reports are not written)."""
    try:
        plan = ad.load_plan()
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read the validation plan: {e}")
    ds = ad.Dataset(data_dir)
    out_dir = Path(out_dir) if out_dir else ds.dir / "build"
    denylist = ad.Denylist.load(ds.dir, denylist_path, use_env)
    py = PythonChecks(ds, today or dt.date.today(), denylist)
    python_findings = py.run()
    try:
        load = load_sqlite.load(ds, out_dir / "atlas.db")
    except RuntimeError as e:
        raise ConfigError(f"SQLite load: {e}")
    sqlite_findings = [Finding(v.check_id, v.report_table, v.record_id, v.field, v.value, v.message, "sqlite")
                       for v in load.violations]
    skipped = list(py.skipped)
    if ds.problems:
        # cross-record checks need every file: on a partial database "roll has no photos" may only mean that
        # data/cities.js did not load, so the queries wait until the structure errors are fixed
        sql_findings = []
        skipped += [(p.name[:6], "SQL query not run: a data file failed a structure check (EC-001 / EC-002 / EC-003)")
                    for p in sorted(CHECKS_DIR.glob("EC-*.sql"))]
    else:
        try:
            sql_findings = run_sql_checks(out_dir / "atlas.db", plan)
        except sqlite3.Error as e:
            raise ConfigError(f"SQL check failed to run: {e}")
    findings = merge(python_findings + sqlite_findings + sql_findings, plan, denylist)
    counts = {"python": len(python_findings), "sql": len(sql_findings), "queries": len(list(CHECKS_DIR.glob("EC-*.sql")))}
    return Result(ds.dir, out_dir, plan, findings, load, skipped, denylist, counts)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate the Travel Atlas data files (docs/validation_plan.md).")
    ap.add_argument("--data-dir", default=str(ad.ROOT), help="site directory to check (default: this repository)")
    ap.add_argument("--out-dir", help="where reports and atlas.db go (default: <data-dir>/build)")
    ap.add_argument("--denylist", help=f"privacy denylist file (default: <data-dir>/{ad.DENYLIST_FILE})")
    args = ap.parse_args(argv)
    try:
        result = validate(args.data_dir, args.out_dir, args.denylist)
    except ConfigError as e:
        print(f"Validator error: {e}", file=sys.stderr)
        return 2
    result.write_reports()
    print(result.terminal())
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
