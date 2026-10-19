#!/usr/bin/env python3
"""Flights: Flightradar24 exports in flights/raw/ → flight table flights/flights.csv → map data flights/flights.geojson.

Usage:
    python3 tools/build_flights.py

  1. Reads flights/raw/: one file per flight, e.g. AM58_3dbd5d3f.csv; the CSV is used when there is one, otherwise the KML
     with the same name (FR24 names the KML AM58-3dbd5d3f.kml; underscore and hyphen count as the same name)
  2. Flight table flights/flights.csv (editable in Excel / Numbers):
     - each new flight gets a row, with whatever can be inferred filled in:
         flight number: the start of the file name (the number FR24 shows); from the callsign when the name doesn't say (AMX058 → AM58)
         airline: first two characters of the flight number → the AIRLINES table below
         airports: the nearest airport within 30 km of the first / last point (tools/airports.csv, from OurAirports);
                   widened to 200 km when FR24 missed the take-off / landing (the end point is still above 3,000 ft)
         dep_utc / arr_utc: UTC time of the first / last point
         date: local date at the departure airport (time zone from the airportsdata package; UTC date if unknown)
     - cells that already have content are never touched; only empty cells are filled. What the script filled in is recorded in
       tools/.flight-autofill.json so a date you changed by hand can be told apart: yours wins, the rest show the departure's local date
     - flights with show = no are not drawn
  3. Airline logo inbox airline_logos_inbox/: a folder "AM Aeroméxico" is created for each airline in the table (only the code
     before the first space matters); drop a logo in it (png / jpg / webp / svg; the newest wins):
       bitmap → 96px tall (shown at 48px on the card, 2×), at most 320px wide, saved as PNG (quantised to 128 colours);
                a WebP copy is kept too when it is smaller than the PNG, and the page prefers it
       SVG → scripts, external links, comments, editor metadata and hidden layers removed; coordinates rounded to 2 decimals
  4. Writes flights/flights.geojson: tracks simplified with Douglas–Peucker (about 1.5 km tolerance), split where they cross 180°

flights/raw/ and airline_logos_inbox/ are in .gitignore; commit flights.csv, flights.geojson and assets/airlines/.
Needs airportsdata (pip3 install airportsdata, for local times); bitmap logos need Pillow (pip3 install Pillow).
"""

import csv
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
RAW = ROOT / "flights" / "raw"
TABLE = ROOT / "flights" / "flights.csv"
OUT = ROOT / "flights" / "flights.geojson"
AIRPORTS_CSV = TOOLS / "airports.csv"
LOGO_INBOX = ROOT / "airline_logos_inbox"
LOGO_DIR = ROOT / "assets" / "airlines"
LOGO_MANIFEST = TOOLS / ".logo-manifest.json"
AUTOFILL = TOOLS / ".flight-autofill.json"

COLUMNS = ["id", "date", "flight_no", "airline", "airline_iata", "from_iata", "from_city", "to_iata", "to_city",
           "dep_utc", "arr_utc", "aircraft", "seat", "note", "show"]
TIME_FMT = "%Y-%m-%d %H:%M"

TOLERANCE_KM = 1.5
AIRPORT_RADIUS_KM = 30
AIRBORNE_FT = 3000             # an end point above this altitude means the track is incomplete (take-off or landing missed)
AIRBORNE_RADIUS_KM = 200
LOGO_HEIGHT = 96               # shown at 48px on the card, stored at 2×
LOGO_MAX_WIDTH = 320
PNG_COLORS = 128
QUANT_MAX_RMS = 2.5            # if 128-colour quantisation errs more than this (common with gradients), use 256 colours
WEBP_QUALITY = 85
LOGO_WARN_KB = 30
LOGO_VERSION = 4               # bump when the processing changes and every logo is regenerated
R = 6371.0

# Airlines: ICAO callsign prefix → (IATA, name). Add a line for a missing airline, or fill it in flights.csv directly.
AIRLINES = {
    "AMX": ("AM", "Aeroméxico"),        "ARG": ("AR", "Aerolíneas Argentinas"), "AVA": ("AV", "Avianca"),
    "DAL": ("DL", "Delta Air Lines"),   "LAN": ("LA", "LATAM Airlines"),       "LPE": ("LP", "LATAM Airlines Perú"),
    "PGT": ("PC", "Pegasus Airlines"),  "FIA": ("5F", "FlyOne"),               "QTR": ("QR", "Qatar Airways"),
    "UAL": ("UA", "United Airlines"),   "AAL": ("AA", "American Airlines"),    "SWA": ("WN", "Southwest Airlines"),
    "JBU": ("B6", "JetBlue"),           "ASA": ("AS", "Alaska Airlines"),      "ACA": ("AC", "Air Canada"),
    "VOI": ("Y4", "Volaris"),           "VIV": ("VB", "Viva Aerobus"),         "CMP": ("CM", "Copa Airlines"),
    "JAT": ("JA", "JetSMART"),          "FBZ": ("FO", "Flybondi"),             "GLO": ("G3", "Gol"),
    "AZU": ("AD", "Azul"),              "TAM": ("JJ", "LATAM Airlines Brasil"), "AEA": ("UX", "Air Europa"),
    "IBE": ("IB", "Iberia"),            "AFR": ("AF", "Air France"),           "KLM": ("KL", "KLM"),
    "BAW": ("BA", "British Airways"),   "DLH": ("LH", "Lufthansa"),            "SWR": ("LX", "Swiss"),
    "AUA": ("OS", "Austrian Airlines"), "ITY": ("AZ", "ITA Airways"),          "TAP": ("TP", "TAP Air Portugal"),
    "THY": ("TK", "Turkish Airlines"),  "AEE": ("A3", "Aegean Airlines"),      "CTN": ("OU", "Croatia Airlines"),
    "ROT": ("RO", "TAROM"),             "RYR": ("FR", "Ryanair"),              "EZY": ("U2", "easyJet"),
    "EZS": ("DS", "easyJet Switzerland"), "EWG": ("EW", "Eurowings"),
    "ASL": ("JU", "Air Serbia"),        "WZZ": ("W6", "Wizz Air"),             "UAE": ("EK", "Emirates"),
    "ETD": ("EY", "Etihad Airways"),    "CCA": ("CA", "Air China"),            "CES": ("MU", "China Eastern Airlines"),
    "CSN": ("CZ", "China Southern Airlines"), "CPA": ("CX", "Cathay Pacific"), "JAL": ("JL", "Japan Airlines"),
    "ANA": ("NH", "ANA"),               "KAL": ("KE", "Korean Air"),           "SIA": ("SQ", "Singapore Airlines"),
}
AIRLINE_NAMES = {iata: name for iata, name in AIRLINES.values()}

AIRLINE_NAMES["LA"] = "LATAM Airlines"   # flights of subsidiaries such as LP / JJ are mostly sold under LA numbers

# Display name of the city an airport serves, where OurAirports' municipality would be less familiar; others use OurAirports
CITY_NAMES = {
    "EZE": "Buenos Aires", "AEP": "Buenos Aires", "MDZ": "Mendoza", "LIM": "Lima", "BOG": "Bogotá",
    "CTG": "Cartagena", "AXM": "Armenia", "PEI": "Pereira", "MEX": "Mexico City", "MTY": "Monterrey", "CUN": "Cancún",
    "GDL": "Guadalajara", "ATL": "Atlanta", "IAD": "Washington", "DCA": "Washington", "JFK": "New York", "EWR": "New York",
    "LGA": "New York", "SFO": "San Francisco", "LAS": "Las Vegas", "LAX": "Los Angeles", "SEA": "Seattle", "ORD": "Chicago",
    "MIA": "Miami", "IAH": "Houston", "DFW": "Dallas", "YYZ": "Toronto", "YVR": "Vancouver", "PTY": "Panama City",
    "SCL": "Santiago", "GRU": "São Paulo", "ZRH": "Zurich", "GVA": "Geneva", "BER": "Berlin", "MUC": "Munich",
    "FRA": "Frankfurt", "TRS": "Trieste", "VCE": "Venice", "PUY": "Pula", "LJU": "Ljubljana",
    "SJJ": "Sarajevo", "BEG": "Belgrade", "RJK": "Rijeka", "DBV": "Dubrovnik", "SPU": "Split",
    "ZAG": "Zagreb", "SOF": "Sofia", "OTP": "Bucharest", "RMO": "Chișinău", "ATH": "Athens", "IST": "Istanbul",
    "SAW": "Istanbul", "VIE": "Vienna", "LHR": "London", "CDG": "Paris", "AMS": "Amsterdam", "MAD": "Madrid",
    "FCO": "Rome", "DOH": "Doha", "DXB": "Dubai", "PEK": "Beijing", "PKX": "Beijing", "PVG": "Shanghai", "HKG": "Hong Kong",
    "NRT": "Narita", "HND": "Tokyo", "ICN": "Seoul",
}


# ---------- tracks ----------

def flight_id(path):
    """AM58_3dbd5d3f.csv and AM58-3dbd5d3f.kml are both AM58_3dbd5d3f."""
    return re.sub(r"-", "_", path.stem)


def read_csv(path):
    pts, callsign = [], ""
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            lat, lng = (float(v) for v in row["Position"].split(","))
            pts.append((int(row["Timestamp"]), lat, lng, float(row.get("Altitude") or 0)))  # feet
            callsign = callsign or row.get("Callsign", "").strip()
    return pts, callsign


def read_kml(path):
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    root = ET.parse(path).getroot()
    pts = []
    for pm in root.iterfind(".//k:Placemark", ns):
        when = pm.find("k:TimeStamp/k:when", ns)
        coord = pm.find("k:Point/k:coordinates", ns)
        if when is None or coord is None:
            continue
        vals = [float(v) for v in coord.text.strip().split(",")]
        lng, lat, alt_m = vals[0], vals[1], (vals[2] if len(vals) > 2 else 0)
        t = datetime.fromisoformat(when.text.strip().replace("Z", "+00:00"))
        pts.append((int(t.timestamp()), lat, lng, alt_m * 3.28084))  # KML uses metres; convert to feet like the CSV
    name = root.findtext("k:Document/k:name", "", ns)  # e.g. "AM58/AMX058"
    callsign = name.split("/", 1)[1].strip() if "/" in name else ""
    return pts, callsign


def raw_sources():
    sources = {}
    for p in sorted(RAW.iterdir()):
        ext = p.suffix.lower()
        if ext not in (".csv", ".kml") or p.name.startswith("."):
            continue
        fid = flight_id(p)
        if ext == ".csv" or fid not in sources:   # CSV first
            sources[fid] = p
    return sources


# ---------- geometry ----------

def haversine(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def unwrap(pts):
    """Make longitudes continuous (no jump at 180°); they are split at ±180 afterwards."""
    out, prev = [], None
    for lat, lng in pts:
        if prev is not None:
            while lng - prev > 180:
                lng -= 360
            while lng - prev < -180:
                lng += 360
        out.append((lat, lng))
        prev = lng
    return out


def mercator(lat, lng):
    lat = max(min(lat, 85), -85)
    return R * math.radians(lng), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def simplify(pts, tol_km):
    """Douglas–Peucker on the Mercator plane (where Leaflet draws straight lines), scaled by cos(latitude) back to ground distance."""
    if len(pts) < 3:
        return pts
    xy = [mercator(lat, lng) for lat, lng in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        worst, idx = 0.0, None
        for i in range(a + 1, b):
            px, py = xy[i]
            t = 0 if seg2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / seg2))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy) * math.cos(math.radians(pts[i][0]))
            if d > worst:
                worst, idx = d, i
        if idx is not None and worst > tol_km:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def split_antimeridian(pts):
    """A polyline with continuous longitudes → polylines that each stay within [-180, 180] (interpolated where they cross)."""
    def shift(lng):
        return (lng + 180) % 360 - 180

    lines, cur = [], []
    for i, (lat, lng) in enumerate(pts):
        if i:
            plat, plng = pts[i - 1]
            k_prev, k_cur = math.floor((plng + 180) / 360), math.floor((lng + 180) / 360)
            if k_prev != k_cur:
                edge = (max(k_prev, k_cur)) * 360 - 180   # the ±180 line crossed (in continuous longitudes)
                f = (edge - plng) / (lng - plng)
                clat = plat + f * (lat - plat)
                side = 180 if lng > plng else -180
                cur.append([side, clat])
                lines.append(cur)
                cur = [[-side, clat]]
        cur.append([shift(lng), lat])
    lines.append(cur)
    return [[[round(x, 4), round(y, 4)] for x, y in ln] for ln in lines if len(ln) >= 2]


# ---------- time zones ----------

_TZ = None


def airport_tz(code):
    """IATA → ZoneInfo; None when airportsdata isn't installed or the airport is unknown."""
    global _TZ
    if _TZ is None:
        try:
            import airportsdata
            _TZ = {k: v["tz"] for k, v in airportsdata.load("IATA").items() if v.get("tz")}
        except ImportError:
            _TZ = {}
    from zoneinfo import ZoneInfo
    name = _TZ.get(code.strip().upper()) if code else None
    return ZoneInfo(name) if name else None


def local(utc_dt, code):
    """UTC time → (airport local time, whether it was actually converted)."""
    tz = airport_tz(code)
    aware = utc_dt.replace(tzinfo=timezone.utc)
    return (aware.astimezone(tz), True) if tz else (aware, False)


# ---------- inferring flight details ----------

def load_airports():
    with open(AIRPORTS_CSV, newline="", encoding="utf-8") as f:
        return {r["iata"]: (r["municipality"], float(r["lat"]), float(r["lng"])) for r in csv.DictReader(f)}


def nearest_airport(pt, airports):
    _, lat, lng, alt = pt
    radius = AIRBORNE_RADIUS_KM if alt > AIRBORNE_FT else AIRPORT_RADIUS_KM
    code, (_, alat, alng) = min(airports.items(), key=lambda kv: haversine(lat, lng, kv[1][1], kv[1][2]))
    return code if haversine(lat, lng, alat, alng) <= radius else ""


def infer(fid, pts, callsign, airports):
    """Columns that can be inferred from the track, file name and callsign; the rest stay empty."""
    flight_no = ""
    m = re.match(r"^([A-Z0-9]{2})(\d{1,4})([A-Z]?)$", fid.split("_")[0].upper())
    if m and re.search(r"[A-Z]", m.group(1)):
        flight_no = m.group(1) + str(int(m.group(2))) + m.group(3)
    else:
        m = re.match(r"^([A-Z]{3})(\d{1,4})$", callsign.upper())
        if m and m.group(1) in AIRLINES:
            flight_no = AIRLINES[m.group(1)][0] + str(int(m.group(2)))
    airline_iata = flight_no[:2]
    if not airline_iata and callsign[:3].upper() in AIRLINES:
        airline_iata = AIRLINES[callsign[:3].upper()][0]

    first, last = pts[0], pts[-1]
    dep, arr = nearest_airport(first, airports), nearest_airport(last, airports)
    city = lambda code: CITY_NAMES.get(code) or (airports[code][0] if code else "")
    t = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime(TIME_FMT)
    dep_local, _ = local(datetime.fromtimestamp(first[0], timezone.utc).replace(tzinfo=None), dep)
    return {
        "id": fid,
        "date": dep_local.strftime("%Y-%m-%d"),
        "flight_no": flight_no,
        "airline": AIRLINE_NAMES.get(airline_iata, ""),
        "airline_iata": airline_iata,
        "from_iata": dep, "from_city": city(dep),
        "to_iata": arr, "to_city": city(arr),
        "dep_utc": t(first[0]), "arr_utc": t(last[0]),
        "show": "yes",
    }


# ---------- flight table ----------

def read_table():
    if not TABLE.exists():
        return [], list(COLUMNS)
    with open(TABLE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or COLUMNS)
        rows = [{k: (v or "") for k, v in r.items() if k is not None} for r in reader]
    for c in COLUMNS:                     # columns you added yourself are kept
        if c not in fields:
            fields.append(c)
    return rows, fields


def write_table(rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    TABLE.write_text(buf.getvalue(), encoding="utf-8-sig")   # with a BOM so Excel reads it as UTF-8


def excel_fix(value, recorded=""):
    """Excel / Numbers save 2026-08-16 as 8/16/26, 2026/8/16 9:58, 46250 and the like; restore the original format (same value).
    When month and day are ambiguous, prefer the reading that matches what the script filled in; otherwise US month/day."""
    v = value.strip()
    if re.fullmatch(r"\d{5}(\.\d+)?", v) and 20000 < float(v) < 80000:   # Excel date serial: 46250 = 2026-08-16
        dt = datetime(1899, 12, 30) + timedelta(days=float(v))
        dt = (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)   # fractional times carry rounding error; round to the minute
        return dt.strftime(TIME_FMT if "." in v else "%Y-%m-%d")
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::\d{2})?)?$", v)
    if m:
        parts = [(int(m[1]), int(m[2]), int(m[3]))]
    else:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})(?:\s+(\d{1,2}):(\d{2})(?::\d{2})?)?$", v)
        if not m:
            return value
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        parts = [(y, int(m[1]), int(m[2])), (y, int(m[2]), int(m[1]))]
    out = []
    for y, mo, d in parts:
        try:
            dt = datetime(y, mo, d, int(m[4] or 0), int(m[5] or 0))
        except ValueError:
            continue
        s_ = dt.strftime(TIME_FMT if m[4] else "%Y-%m-%d")
        if s_ not in out:
            out.append(s_)
    if not out:
        return value
    return recorded if recorded in out else out[0]


def parse_time(s):
    s = s.strip().replace("T", " ").rstrip("Zz").strip()
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


# ---------- airline logos ----------

SVG_NS, XLINK_NS = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NUMBER = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
ROUND_ATTRS = {"d", "points", "transform", "viewbox", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r",
               "rx", "ry", "fx", "fy", "width", "height", "stroke-width", "font-size", "gradienttransform"}
KEEP_HIDDEN = {"defs", "lineargradient", "radialgradient", "pattern", "clippath", "mask", "filter",
               "symbol", "marker", "style"}   # never displayed directly anyway, so display:none must not remove them


def fmt_number(m):
    v = round(float(m.group(0)), 2)
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def round_numbers(name, value):
    if name == "d":
        if re.search(r"[aA]", value):      # arc flags may be run together (0110), which isn't safe to touch; keep as is
            return value
        tokens = re.findall(r"[A-Za-z]|" + NUMBER.pattern, value)
        return " ".join(fmt_number(re.match(NUMBER, t)) if not t.isalpha() else t for t in tokens)
    return NUMBER.sub(fmt_number, value)


def clean_svg(src, dst):
    """Sanitise and slim down: drop scripts / event attributes / external links, comments, metadata, editor data and hidden layers;
    round coordinates to 2 decimals; strip extra whitespace."""
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    root = ET.parse(src).getroot()          # ElementTree already drops comments and the DOCTYPE while parsing
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("not an SVG file")
    external_url = re.compile(r"url\(\s*['\"]?(?!#)[^)]*\)", re.I)
    ns = lambda tag: tag[1:].split("}")[0] if tag.startswith("{") else SVG_NS
    local = lambda tag: tag.split("}")[-1].lower()
    hidden = lambda el: (el.get("display", "").strip() == "none" or el.get("visibility", "").strip() == "hidden"
                         or re.search(r"(display\s*:\s*none|visibility\s*:\s*hidden)", el.get("style", ""), re.I))

    def drop(el):
        name = local(el.tag)
        if ns(el.tag) != SVG_NS:                                   # Inkscape / Illustrator and other editor elements
            return True
        if name in ("script", "foreignobject", "metadata"):
            return True
        if name == "desc" and re.search(r"created with|generator", el.text or "", re.I):
            return True
        return name not in KEEP_HIDDEN and bool(hidden(el))         # hidden layers

    def walk(parent):
        for child in list(parent):
            if not isinstance(child.tag, str) or drop(child):
                parent.remove(child)
            else:
                walk(child)
    walk(root)

    for el in root.iter():
        for attr in list(el.attrib):
            attr_ns = attr[1:].split("}")[0] if attr.startswith("{") else ""
            name, value = local(attr), el.attrib[attr].strip()
            if attr_ns not in ("", XLINK_NS, XML_NS) or name.startswith("on") or name == "data-name":
                del el.attrib[attr]
            elif name == "href" and not (value.startswith("#") or value.startswith("data:image/")):
                del el.attrib[attr]
            elif external_url.search(value):
                el.attrib[attr] = external_url.sub("none", value)
            elif name in ROUND_ATTRS:
                el.attrib[attr] = round_numbers(name, value)
        if local(el.tag) == "style" and el.text:
            el.text = external_url.sub("none", re.sub(r"@import[^;]*;?", "", el.text)).strip()
        if local(el.tag) not in ("text", "tspan", "textpath", "style", "title"):
            if el.text and not el.text.strip():
                el.text = None
        if el.tail and not el.tail.strip():
            el.tail = None
    ET.ElementTree(root).write(dst, encoding="utf-8", xml_declaration=False)
    return "SVG"


def white_to_alpha(im):
    """Opaque images with white corners (JPGs, say): turn the white background transparent.
    Uses colour-to-alpha: alpha = 255 − the darkest of the three channels, with colours scaled back accordingly,
    so anti-aliased edges become semi-transparent originals instead of leaving a white fringe."""
    from PIL import Image, ImageChops, ImageMath
    if im.getchannel("A").getextrema()[0] < 250:
        return im                                           # already has transparency
    w, h = im.size
    if not all(min(im.getpixel(p)[:3]) >= 245 for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))):
        return im                                           # background isn't white
    r, g, b, _ = im.split()
    a = ImageChops.invert(ImageChops.darker(ImageChops.darker(r, g), b))
    fix = lambda c: ImageMath.unsafe_eval("convert(255 - (255 - c) * 255 / max(a, 1), 'L')", c=c, a=a)
    return Image.merge("RGBA", (fix(r), fix(g), fix(b), a))


def process_raster(src, code, out_dir=None):
    """Trim transparent and white borders; 96px tall (never upscaled), at most 320px wide; save PNG (quantised) and WebP, both with alpha."""
    try:
        from PIL import Image, ImageChops, ImageOps, ImageStat
    except ImportError:
        raise RuntimeError("bitmap logos need Pillow: pip3 install Pillow")
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
    im = white_to_alpha(im)
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 250:                                  # has transparency: trim transparent borders first
        box = alpha.point(lambda a: 255 if a > 8 else 0).getbbox()
        if box:
            im = im.crop(box)
    w, h = im.size
    corners = [im.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    if all(a >= 250 and min(r, g, b) >= 240 for r, g, b, a in corners):   # white corners: trim white borders as well
        r, g, b, a = im.split()
        darkest = ImageChops.darker(ImageChops.darker(r, g), b)
        content = ImageChops.multiply(darkest.point(lambda v: 255 if v < 240 else 0),
                                      a.point(lambda v: 255 if v > 8 else 0))
        box = content.getbbox()
        if box:
            im = im.crop(box)
    scale = min(1, LOGO_HEIGHT / im.height, LOGO_MAX_WIDTH / im.width)
    if scale < 1:
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)

    def error(q):
        """Difference before / after quantising: composited on white and on black, plus alpha; the largest RMS error."""
        q = q.convert("RGBA")
        errs = [max(ImageStat.Stat(ImageChops.difference(im.getchannel("A"), q.getchannel("A"))).rms)]
        for bg in ((255, 255, 255, 255), (0, 0, 0, 255)):
            base = Image.new("RGBA", im.size, bg)
            a, b = Image.alpha_composite(base, im).convert("RGB"), Image.alpha_composite(base, q).convert("RGB")
            errs.append(max(ImageStat.Stat(ImageChops.difference(a, b)).rms))
        return max(errs)

    colors = PNG_COLORS
    q = im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
    if error(q) > QUANT_MAX_RMS:                                     # gradients and edges suffer: fall back to 256 colours
        colors = 256
        q = im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
    out_dir = out_dir or LOGO_DIR
    png, webp = out_dir / f"{code}.png", out_dir / f"{code}.webp"
    q.save(png, "PNG", optimize=True)
    im.save(webp, "WEBP", quality=WEBP_QUALITY, method=6)
    # the PNG is always kept as the fallback; the WebP only when it is smaller (quantised flat-colour PNGs often win)
    if webp.stat().st_size >= png.stat().st_size:
        webp.unlink()
        return f"{im.width}×{im.height}, PNG {colors} colours, WebP not smaller so not kept"
    return f"{im.width}×{im.height}, PNG {colors} colours"


def image_kind(path):
    """Tell the format from the content, not the name: a bitmap if Pillow opens it, otherwise try SVG; None if neither."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return "raster"
    except ImportError:
        pass                                   # no Pillow: bitmaps fail later with a hint to install it
    except Exception:
        pass
    try:
        if ET.parse(path).getroot().tag.split("}")[-1] == "svg":
            return "svg"
    except Exception:
        pass
    try:                                       # without Pillow, recognise common bitmaps by their header and report "needs Pillow" later
        head = path.read_bytes()[:12]
        if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            return "raster"
    except OSError:
        pass
    return None


def image_format(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.format or "bitmap"
    except Exception:
        return "bitmap"


def logo_files(code, out_dir=None):
    out_dir = out_dir or LOGO_DIR
    return [out_dir / f"{code}{ext}" for ext in (".svg", ".png", ".webp") if (out_dir / f"{code}{ext}").exists()]


def logo_props(code, out_dir=None):
    """Logo paths for the card: an SVG, or a PNG (with a preferred WebP)."""
    rel = lambda p: p.relative_to(ROOT).as_posix()
    out_dir = out_dir or LOGO_DIR
    svg, png, webp = (out_dir / f"{code}{e}" for e in (".svg", ".png", ".webp"))
    if code and svg.exists():
        return {"logo": rel(svg), "logo_webp": ""}
    if code and png.exists():
        return {"logo": rel(png), "logo_webp": rel(webp) if webp.exists() else ""}
    return {"logo": "", "logo_webp": ""}


def update_logos(rows):
    """Airline logo inbox; returns information for the summary."""
    names = {}
    for r in rows:
        code = r.get("airline_iata", "").strip().upper()
        if code and code not in names:
            names[code] = r.get("airline", "").strip() or AIRLINE_NAMES.get(code, "")
    return sync_logo_box(names, LOGO_INBOX, LOGO_DIR, LOGO_MANIFEST)


def sync_logo_box(names, inbox, out_dir, manifest_path):
    """Logo inbox (shared by airlines and rail operators): names is {code: display name}.
    Creates a folder "<code> <name>" for each (only the code before the first space matters; existing folders are left alone),
    takes the newest recognisable image in each folder and saves it as out_dir/<code>.png (+ .webp) or .svg;
    only changed ones are processed (tracked in manifest_path)."""
    inbox.mkdir(exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    folders, unknown, duplicate, created = {}, [], [], []
    for d in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
        code = d.name.split(" ", 1)[0].upper()
        if code not in names:
            unknown.append(d.name)
        elif code in folders:
            duplicate.append(d.name)
        else:
            folders[code] = d
    for code, name in names.items():
        if code not in folders:
            folders[code] = inbox / (f"{code} {name}" if name else code)
            folders[code].mkdir()
            created.append(folders[code].name)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    updated, kept, missing, failed, big, unreadable = [], [], [], [], [], []
    for code in names:
        # ignore extensions: newest first by modification time, take the first recognisable image (bitmap or SVG)
        files = sorted((p for p in folders[code].iterdir() if p.is_file() and not p.name.startswith(".")),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
        src = kind = None
        for f in files:
            kind = image_kind(f)
            if kind:
                src = f
                break
            unreadable.append(f"{folders[code].name}/{f.name}")
        if not src:
            if files:
                failed.append((code, "no recognisable image in the folder (neither a readable bitmap nor an SVG)"))
            (kept if logo_files(code, out_dir) else missing).append(code)
            continue
        snap = [src.name, src.stat().st_size, src.stat().st_mtime_ns, LOGO_VERSION]
        if manifest.get(code) == snap and logo_files(code, out_dir):
            continue
        is_svg = kind == "svg"
        try:
            detail = clean_svg(src, out_dir / f"{code}.svg") if is_svg else process_raster(src, code, out_dir)
        except Exception as e:
            failed.append((code, f"{src.name}: {e}"))
            continue
        for stale in ((".png", ".webp") if is_svg else (".svg",)):    # format changed: remove the old format's files
            (out_dir / f"{code}{stale}").unlink(missing_ok=True)
        manifest[code] = snap
        outs = logo_files(code, out_dir)
        sizes = ", ".join(f"{p.suffix[1:].upper()} {p.stat().st_size / 1024:.1f} KB" for p in outs)
        fmt = "SVG" if is_svg else image_format(src)
        updated.append(f"{code}: {src.name} (actually {fmt}) {src.stat().st_size / 1024:.1f} KB → {sizes} ({detail})")
        big += [f"{p.name} {p.stat().st_size / 1024:.1f} KB" for p in outs if p.stat().st_size > LOGO_WARN_KB * 1024]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {"created": created, "updated": updated, "kept": kept, "missing": missing, "failed": failed,
            "big": big, "unreadable": unreadable, "unknown": unknown, "duplicate": duplicate, "names": names}


# ---------- main ----------

def main():
    if not RAW.is_dir():
        sys.exit(f"{RAW.relative_to(ROOT)}/ not found: create it and put the Flightradar24 CSV / KML exports in it.")
    airports = load_airports()
    rows, fields = read_table()
    by_id = {r["id"]: r for r in rows}
    autofill = json.loads(AUTOFILL.read_text(encoding="utf-8")) if AUTOFILL.exists() else {}
    excel_fixed = 0
    for row in rows:
        for col in ("date", "dep_utc", "arr_utc"):
            fixed = excel_fix(row.get(col, ""), autofill.get(row["id"], {}).get(col, ""))
            if fixed != row.get(col, ""):
                row[col] = fixed
                excel_fixed += 1

    # 1. read the tracks and complete the flight table (empty cells only)
    tracks, problems, added, filled = {}, [], [], {}
    for fid, path in raw_sources().items():
        try:
            pts, callsign = read_csv(path) if path.suffix.lower() == ".csv" else read_kml(path)
        except Exception as e:
            problems.append(f"{path.name}: unreadable ({type(e).__name__}: {e})")
            continue
        pts = sorted(set(pts))
        if len(pts) < 2:
            problems.append(f"{path.name}: fewer than 2 track points, skipped")
            continue
        tracks[fid] = pts
        auto = infer(fid, pts, callsign, airports)
        if fid not in by_id:
            row = {c: "" for c in fields}
            row.update(auto)
            rows.append(row)
            by_id[fid] = row
            added.append(fid)
            autofill[fid] = {c: v for c, v in auto.items() if v}
        else:
            row = by_id[fid]
            if fid not in autofill:
                # rows filled by an older version have no record yet: cells equal to what would be filled now count as auto-filled
                # (older versions used the UTC date)
                legacy = dict(auto, date=datetime.fromtimestamp(pts[0][0], timezone.utc).strftime("%Y-%m-%d"))
                autofill[fid] = {c: v for c, v in legacy.items() if v and row.get(c, "").strip() == v}
            for col, val in auto.items():
                if val and not row.get(col, "").strip():
                    row[col] = val
                    autofill[fid][col] = val
                    filled.setdefault(fid, []).append(col)
    if added or filled or excel_fixed or not TABLE.exists():
        write_table(rows, fields)
    AUTOFILL.write_text(json.dumps(autofill, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # 2. airline logos
    logos = update_logos(rows)

    # 3. map data
    features, hidden, no_tz = [], [], []
    for row in rows:
        fid = row["id"]
        if fid not in tracks:
            continue
        if row.get("show", "").strip().lower() in ("no", "n", "0", "false"):
            hidden.append(fid)
            continue
        pts = tracks[fid]
        pos = lambda code: airports.get(code.strip().upper(), (None, None, None))[1:] if code.strip() else (None, None)
        dep_pos, arr_pos = pos(row["from_iata"]), pos(row["to_iata"])
        track = [(lat, lng) for _, lat, lng, _ in pts]
        if dep_pos[0] is not None and pts[0][3] > AIRBORNE_FT:    # take-off missed: straight line from the airport
            track.insert(0, dep_pos)
        if arr_pos[0] is not None and pts[-1][3] > AIRBORNE_FT:   # landing missed: straight line to the airport
            track.append(arr_pos)
        parts = split_antimeridian(simplify(unwrap(track), TOLERANCE_KM))
        dep_t, arr_t = parse_time(row["dep_utc"]), parse_time(row["arr_utc"])
        props = {c: row.get(c, "").strip() for c in COLUMNS if c != "show"}
        # local times: departure in the departure airport's zone, arrival in the arrival airport's; UTC (flagged in the summary) if unknown
        dep_l = local(dep_t, row["from_iata"]) if dep_t else None
        arr_l = local(arr_t, row["to_iata"]) if arr_t else None
        for which, l, code in (("departure", dep_l, row["from_iata"]), ("arrival", arr_l, row["to_iata"])):
            if l and not l[1]:
                no_tz.append(f"{fid} {which} airport {code.strip() or '(empty)'}")
        manual_date = props["date"] and props["date"] != autofill.get(fid, {}).get("date")
        if dep_l and not manual_date:
            props["date"] = dep_l[0].strftime("%Y-%m-%d")      # the map shows the departure's local date; the CSV is left alone
        props.update({
            "dep_local": dep_l[0].strftime("%Y-%m-%d %H:%M") if dep_l else "",
            "arr_local": arr_l[0].strftime("%Y-%m-%d %H:%M") if arr_l else "",
            "dep_tz": bool(dep_l and dep_l[1]), "arr_tz": bool(arr_l and arr_l[1]),
            "minutes": round((arr_t - dep_t).total_seconds() / 60) if dep_t and arr_t and arr_t > dep_t else None,
            **logo_props(row["airline_iata"].strip().upper()),
            "from_pos": [dep_pos[1], dep_pos[0]] if dep_pos[0] is not None else None,   # [lng, lat]
            "to_pos": [arr_pos[1], arr_pos[0]] if arr_pos[0] is not None else None,
        })
        geom = ({"type": "LineString", "coordinates": parts[0]} if len(parts) == 1
                else {"type": "MultiLineString", "coordinates": parts})
        features.append({"type": "Feature", "properties": props, "geometry": geom})
    features.sort(key=lambda f: (f["properties"]["dep_utc"], f["properties"]["id"]))
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    # 4. summary
    print("========== Flight table flights/flights.csv ==========")
    print(f"Added {len(added)} rows" + (": " + ", ".join(added) if added else ""))
    if excel_fixed:
        print(f"⚠ Excel / Numbers changed the date format in flights.csv; restored {excel_fixed} cells to the 2026-01-01 form (values unchanged)")
    for fid, cols in filled.items():
        print(f"Filled empty cells in {fid}: {', '.join(cols)}")
    blank = {}
    for row in rows:
        if row["id"] in tracks:
            for c in COLUMNS:
                if c not in ("note", "seat") and not row.get(c, "").strip():
                    blank.setdefault(c, []).append(row["id"])
    if blank:
        print("Columns still empty that you can fill in by hand:")
        for c, ids in blank.items():
            print(f"    {c}: {len(ids)} flights" + (f" ({', '.join(ids)})" if len(ids) <= 6 else ""))
    orphan = [r["id"] for r in rows if r["id"] not in tracks]
    if orphan:
        print("In the table but no track file in flights/raw/ (not drawn): " + ", ".join(orphan))

    print("\n========== Airline logos airline_logos_inbox/ ==========")
    if logos["created"]:
        print("✦ New airlines; folders created, drop their logos in:")
        for name in logos["created"]:
            hint = "" if " " in name else " (unknown airline: fill in the airline column of flights.csv, or add a line to AIRLINES in this script)"
            print(f"    airline_logos_inbox/{name}/{hint}")
    print("Updated logos: " + ("none" if not logos["updated"] else ""))
    for line in logos["updated"]:
        print("    " + line)
    if logos["big"]:
        print(f"⚠ Over {LOGO_WARN_KB} KB; consider a simpler image: " + ", ".join(logos["big"]))
    if logos["kept"]:
        print("Inbox empty but a logo already exists, left as is: " + ", ".join(logos["kept"]))
    if logos["missing"]:
        print("⚠ No logo yet (cards show a code badge); find one and put it in the folder:")
        for code in logos["missing"]:
            print(f"    {code} {logos['names'][code]}")
    for code, why in logos["failed"]:
        print(f"✗ {code}: logo processing failed: {why}")
    if logos["unreadable"]:
        print("⚠ Neither a readable bitmap nor an SVG, skipped:")
        for f in logos["unreadable"]:
            print(f"    airline_logos_inbox/{f}")
    for name in logos["unknown"]:
        print(f"⚠ airline_logos_inbox/{name} matches no airline_iata in the table, not processed")
    for name in logos["duplicate"]:
        print(f"⚠ airline_logos_inbox/{name} is the same airline as another folder, not processed")

    print(f"\nWrote {OUT.relative_to(ROOT)}: {len(features)} flights, {OUT.stat().st_size / 1024:.1f} KB"
          + (f"; not drawn (show=no): {', '.join(hidden)}" if hidden else ""))
    if no_tz:
        print("⚠ Airport time zone unknown, so dates and times are shown in UTC: " + ", ".join(no_tz)
              + (" (airportsdata isn't installed: pip3 install airportsdata)" if not _TZ else ""))
    for p in problems:
        print("⚠ " + p)


if __name__ == "__main__":
    main()
