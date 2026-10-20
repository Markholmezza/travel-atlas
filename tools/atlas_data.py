#!/usr/bin/env python3
"""Shared readers for the data validation tools (tools/validate_data.py, tools/load_sqlite.py, tools/test_validation.py).

  - parse_js_consts: data/cities.js and data/articles.js are JavaScript, not JSON (unquoted keys, comments,
    trailing commas); a small literal parser reads them and reports syntax errors with line and column
  - Dataset: every data file of a site directory, parsed once; files that fail EC-001 / EC-002 (or the top-level
    shape of EC-003) are recorded as problems and left out, so later checks and the SQLite load skip them
  - load_plan: the check table in docs/validation_plan.md, the single list of rules (id → category, severity …)
  - Denylist: the privacy denylist (EC-055) and the matcher that redacts it from every output

Standard library only (Python 3.8+), so it runs unchanged on GitHub Actions and in the Cloudflare Pages build.
"""

import csv
import json
import math
import os
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "validation_plan.md"

# Data files, relative to the site (data) directory
FILES = {
    "cities": "data/cities.js",
    "articles": "data/articles.js",
    "changelog": "data/changelog.json",
    "changelog_text": "data/changelog-text.csv",
    "us_states_geo": "data/geo/us-states-10m.json",
    "rolls": "film/rolls.csv",
    "flights": "flights/flights.csv",
    "flights_geo": "flights/flights.geojson",
    "routes": "routes/routes.csv",
    "rail": "routes/rail.csv",
    "routes_geo": "routes/routes.geojson",
    "airports": "tools/airports.csv",
}

# EC-002: required header columns of each CSV file (validation plan, Appendix A.2)
CSV_COLUMNS = {
    "rolls": ["roll", "film_stock", "iso", "format", "camera", "lens", "date_from", "date_to", "lab", "note", "box_image"],
    "flights": ["id", "date", "flight_no", "airline", "airline_iata", "from_iata", "from_city", "to_iata", "to_city",
                "dep_utc", "arr_utc", "aircraft", "seat", "note", "show"],
    "routes": ["id", "mode", "date", "from", "to", "number", "operator", "distance_km", "duration", "note", "show", "via"],
    "rail": ["id", "operator_code", "train_number", "train_name", "dep_station", "arr_station", "dep_local", "arr_local",
             "rolling_stock", "class", "coach", "seat_or_berth", "note"],
    "airports": ["iata", "name", "municipality", "country", "lat", "lng", "size"],
    "changelog_text": ["commit", "text"],
}

# EC-001: constants each JavaScript data file must define, with their expected type
JS_CONSTS = {
    "cities": {"CITIES": list, "COUNTRIES": dict, "REGIONS": list, "US_STATES": dict, "SWISS_CANTONS": dict},
    "articles": {"ARTICLES": dict},
}
JSON_TYPES = {"changelog": list, "us_states_geo": dict}

DENYLIST_FILE = "tools/privacy_denylist.txt"
DENYLIST_ENV = "ATLAS_PRIVACY_DENYLIST"


# ---------- JavaScript literals ----------

class JSParseError(ValueError):
    def __init__(self, message, text, pos):
        self.reason = message
        self.line = text.count("\n", 0, pos) + 1
        self.col = pos - (text.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{message} at line {self.line}, column {self.col}")


_CONST = re.compile(r"const\s+([A-Za-z_$][\w$]*)\s*=")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class _Parser:
    """Object / array / string / number / true / false / null literals, as written in the data files."""

    def __init__(self, text):
        self.s, self.i = text, 0

    def error(self, message, pos=None):
        return JSParseError(message, self.s, self.i if pos is None else pos)

    def skip(self):
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                if end < 0:
                    raise self.error("unterminated comment")
                self.i = end + 2
            else:
                break

    def value(self):
        self.skip()
        if self.i >= len(self.s):
            raise self.error("unexpected end of file")
        c = self.s[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "\"'":
            return self.string()
        m = _NUMBER.match(self.s, self.i)
        if m:
            self.i = m.end()
            t = m.group()
            return float(t) if any(x in t for x in ".eE") else int(t)
        m = _IDENT.match(self.s, self.i)
        if m and m.group() in ("true", "false", "null"):
            self.i = m.end()
            return {"true": True, "false": False, "null": None}[m.group()]
        raise self.error(f"unexpected {c!r}")

    def string(self):
        q, s = self.s[self.i], self.s
        start, self.i, out = self.i, self.i + 1, []
        while True:
            if self.i >= len(s) or s[self.i] == "\n":
                raise self.error("unterminated string", start)
            c = s[self.i]
            if c == q:
                self.i += 1
                return "".join(out)
            if c == "\\":
                e = s[self.i + 1:self.i + 2]
                if e == "u":
                    out.append(chr(int(s[self.i + 2:self.i + 6], 16)))
                    self.i += 6
                    continue
                if e == "\n":
                    self.i += 2
                    continue
                out.append(_ESCAPES.get(e, e))
                self.i += 2
                continue
            out.append(c)
            self.i += 1

    def arr(self):
        self.i += 1
        out = []
        while True:
            self.skip()
            if self.s.startswith("]", self.i):
                self.i += 1
                return out
            out.append(self.value())
            self.skip()
            if self.s.startswith(",", self.i):
                self.i += 1
            elif not self.s.startswith("]", self.i):
                raise self.error("expected ',' or ']'")

    def obj(self):
        self.i += 1
        out = {}
        while True:
            self.skip()
            if self.s.startswith("}", self.i):
                self.i += 1
                return out
            pos = self.i
            if self.s[self.i:self.i + 1] in ("\"", "'"):
                key = self.string()
            else:
                m = _IDENT.match(self.s, self.i) or _NUMBER.match(self.s, self.i)
                if not m:
                    raise self.error("expected a property name")
                key, self.i = m.group(), m.end()
            if key in out:      # JavaScript keeps the last value silently; for data that is a lost value
                raise self.error(f"duplicate key {key!r}", pos)
            self.skip()
            if not self.s.startswith(":", self.i):
                raise self.error("expected ':'")
            self.i += 1
            out[key] = self.value()
            self.skip()
            if self.s.startswith(",", self.i):
                self.i += 1
            elif not self.s.startswith("}", self.i):
                raise self.error("expected ',' or '}'")


def parse_js_consts(text):
    """{name: value} for every `const NAME = <literal>;` in a data file."""
    p, out = _Parser(text), {}
    while True:
        p.skip()
        if p.i >= len(text):
            return out
        m = _CONST.match(text, p.i)
        if not m:
            raise p.error("expected `const NAME = …`")
        p.i = m.end()
        out[m.group(1)] = p.value()
        p.skip()
        if text.startswith(";", p.i):
            p.i += 1


# ---------- values ----------

def fmt_value(v):
    """One text form for raw values, shared by the Python checks and the SQLite loader so findings match."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km; None if any coordinate is missing (also registered as an SQL function)."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742.0 * math.asin(min(1.0, math.sqrt(a)))


# ---------- dataset ----------

class Problem:
    """A file-level finding (EC-001 / EC-002 / EC-003) raised while reading."""

    def __init__(self, check_id, path, record_id, field, value, message):
        self.check_id, self.path, self.record_id, self.field, self.value, self.message = \
            check_id, path, record_id, field, value, message


class Dataset:
    """All data files of one site directory. data[key] is None when a file is missing or unusable."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir).resolve()
        self.problems = []
        self.data = {}
        self.columns = {}
        for key, rel in FILES.items():
            self.data[key] = self._read(key, rel)

    def path(self, rel):
        return self.dir / rel

    def ok(self, *keys):
        return all(self.data.get(k) is not None for k in keys)

    def _problem(self, check_id, rel, message, record_id="", field="", value=""):
        self.problems.append(Problem(check_id, rel, record_id, field, value, message))

    def _read(self, key, rel):
        p = self.dir / rel
        if not p.is_file():
            self._problem("EC-001", rel, "File is missing")                                     # EC-001
            return None
        try:
            text = p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as e:
            self._problem("EC-001", rel, f"Not valid UTF-8 (byte {e.start})")                   # EC-001
            return None
        if key in JS_CONSTS:
            return self._read_js(key, rel, text)
        if key in CSV_COLUMNS:
            return self._read_csv(key, rel, text)
        return self._read_json(key, rel, text)

    def _read_js(self, key, rel, text):
        try:
            consts = parse_js_consts(text)
        except JSParseError as e:
            self._problem("EC-001", rel, f"JavaScript syntax error: {e.reason} (column {e.col})", f"line {e.line}")  # EC-001
            return None
        except ValueError as e:
            self._problem("EC-001", rel, f"JavaScript syntax error: {e}")                       # EC-001
            return None
        for name, typ in JS_CONSTS[key].items():
            if not isinstance(consts.get(name), typ):
                what = "is missing" if name not in consts else f"is not {'an array' if typ is list else 'an object'}"
                self._problem("EC-001", rel, f"const {name} {what}", name)                      # EC-001
                return None
        return consts

    def _read_csv(self, key, rel, text):
        reader = csv.reader(text.splitlines())
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            self._problem("EC-001", rel, "CSV file is empty")                                   # EC-001
            return None
        except csv.Error as e:
            self._problem("EC-001", rel, f"CSV syntax error: {e}", "line 1")                    # EC-001
            return None
        missing = [c for c in CSV_COLUMNS[key] if c not in header]
        if missing:
            for c in missing:
                self._problem("EC-002", rel, "Required column missing from the header", "header", "column", c)  # EC-002
            return None
        self.columns[key] = header
        rows = []
        try:
            for cells in reader:
                if not any(c.strip() for c in cells):
                    continue
                row = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header)}
                row["_line"] = reader.line_num
                rows.append(row)
        except csv.Error as e:
            self._problem("EC-001", rel, f"CSV syntax error: {e}", f"line {reader.line_num}")  # EC-001
            return None
        return rows

    def _read_json(self, key, rel, text):
        try:
            doc = json.loads(text)
        except ValueError as e:
            line = f"line {e.lineno}" if hasattr(e, "lineno") else ""
            self._problem("EC-001", rel, f"JSON syntax error: {e.msg if hasattr(e, 'msg') else e}", line)  # EC-001
            return None
        if key in ("flights_geo", "routes_geo"):
            # EC-003: the top level must be a FeatureCollection; feature-level structure is checked in validate_data
            if not (isinstance(doc, dict) and doc.get("type") == "FeatureCollection" and isinstance(doc.get("features"), list)):
                self._problem("EC-003", rel, "Not a GeoJSON FeatureCollection with a features array", "", "type",
                              fmt_value(doc.get("type") if isinstance(doc, dict) else type(doc).__name__))
                return None
        elif not isinstance(doc, JSON_TYPES[key]):
            self._problem("EC-001", rel, f"Top level is not {'an array' if JSON_TYPES[key] is list else 'an object'}")  # EC-001
            return None
        return doc

    # Convenient views (empty when the file is unusable)
    def js(self, key, name):
        d = self.data.get(key)
        return d[name] if d else JS_CONSTS[key][name]()

    @property
    def cities(self):
        return self.js("cities", "CITIES")

    @property
    def countries(self):
        return self.js("cities", "COUNTRIES")

    @property
    def regions(self):
        return self.js("cities", "REGIONS")

    def rows(self, key):
        return self.data.get(key) or []


# ---------- validation plan ----------

class Check:
    def __init__(self, check_id, category, description, severity, rationale, implemented_in):
        self.id, self.category, self.description = check_id, category, description
        self.severity, self.rationale, self.implemented_in = severity, rationale, implemented_in


def load_plan(path=PLAN):
    """{check id: Check} from the table in docs/validation_plan.md, the single list of rules."""
    checks, section = {}, None
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
        if section != "Checks" or not line.startswith("| EC-"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
        if len(cells) != 6:
            raise ValueError(f"{path}:{n}: expected 6 columns, found {len(cells)}")
        cid, category, description, severity, rationale, implemented = cells
        if not re.fullmatch(r"EC-\d{3}", cid) or cid in checks or severity not in ("error", "warning"):
            raise ValueError(f"{path}:{n}: bad or duplicate check id, or severity not error/warning")
        checks[cid] = Check(cid, category, description, severity, rationale, implemented)
    if not checks:
        raise ValueError(f"{path}: no checks found")
    return checks


# ---------- privacy denylist (EC-055) ----------

def _fold(s):
    """s lower-cased and without accents, plus the index in s of each folded character."""
    out, idx = [], []
    for i, ch in enumerate(s):
        for d in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(d):
                continue
            for f in d.casefold():
                out.append(f)
                idx.append(i)
    return "".join(out), idx


class Denylist:
    """Terms that must never be published. Matching ignores case and accents; any run of spaces, hyphens,
    underscores or dots between words matches (so "Jane Doe" also finds jane-doe.jpg and JANE_DOE)."""

    def __init__(self, terms, sources):
        self.terms, self.sources = terms, sources
        words = [re.findall(r"[^\W_]+", _fold(t)[0]) for t in terms]
        alts = [r"[\W_]+".join(map(re.escape, w)) for w in words if w]
        self.pattern = re.compile(r"(?<![^\W_])(?:" + "|".join(alts) + r")(?![^\W_])") if alts else None

    def __bool__(self):
        return self.pattern is not None

    def spans(self, s):
        if not self.pattern or not s:
            return []
        folded, idx = _fold(s)
        return [(idx[m.start()], idx[m.end() - 1] + 1) for m in self.pattern.finditer(folded)]

    def redact(self, s):
        """s with every match replaced by ***."""
        if not isinstance(s, str):
            return s
        for a, b in reversed(self.spans(s)):
            s = s[:a] + "***" + s[b:]
        return s

    @classmethod
    def load(cls, data_dir, path=None, use_env=True):
        """Terms from the denylist file (default <data dir>/tools/privacy_denylist.txt) and $ATLAS_PRIVACY_DENYLIST."""
        terms, sources = [], []
        p = Path(path) if path else Path(data_dir) / DENYLIST_FILE
        if p.is_file():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    terms.append(line)
            sources.append(DENYLIST_FILE if not path else str(path))
        env = os.environ.get(DENYLIST_ENV, "") if use_env else ""
        if env.strip():
            terms += [t.strip() for t in re.split(r"[\n,]", env) if t.strip()]
            sources.append("$" + DENYLIST_ENV)
        return cls(terms, sources)
