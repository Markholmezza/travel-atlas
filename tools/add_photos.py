#!/usr/bin/env python3
"""Photo inbox: bring the photos in originals/ into the site.

Usage (no arguments):
    python3 tools/add_photos.py

  1. Reads every place in data/cities.js and creates a folder "<city id> <name>" for it in originals/,
     e.g. "buenos-aires Buenos Aires", with an empty film/ inside. Existing folders are left alone; only the id before the first space counts.
  2. Put the original photos in the right folder and run it again.
  3. Only places whose originals changed (added, removed, replaced) are processed: sorted by capture time,
     long edge 2000px, JPEG quality 82, EXIF removed, colour profile kept,
     photos/<id>/01.jpg, 02.jpg… regenerated as a whole, stale files removed, and the photos list in data/cities.js updated.

  Film: photos in originals/<place>/film/<roll>/ are marked as film automatically (film:true, roll:<roll>);
  photos placed directly in film/ (not in a roll folder) are skipped and listed in the summary.
  They are ordered by file name (scan frame number), quality 85, never cropped; they come after the place's regular photos.
  One roll can be spread over several places' folders: the same roll name means the same roll. A new roll gets a line in
  film/rolls.csv (roll name only). Film photos also record the original file name (scan:"000030340009"), which orders frames in a roll.
  Photos marked as film by hand in data/cities.js (originals not in film/) keep their marks when reprocessed.

  originals/ is in .gitignore and never committed. The last known state of the originals is kept in tools/.photo-manifest.json.

Needs Pillow: pip3 install Pillow (not needed when only folders are created)
iPhone HEIC photos also need: pip3 install pillow-heif
"""

import csv
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CITIES_JS = ROOT / "data" / "cities.js"
PHOTOS = ROOT / "photos"
INBOX = ROOT / "originals"
MANIFEST = Path(__file__).resolve().parent / ".photo-manifest.json"
ROLLS_CSV = ROOT / "film" / "rolls.csv"
FILM_DIR = "film"

LONG_EDGE = 2000
QUALITY = 82
FILM_QUALITY = 85
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
HEIC_EXTS = {".heic", ".heif"}

EXIF_IFD = 0x8769
DATETIME_ORIGINAL = 36867
DATETIME = 306

Image = ImageOps = None
HEIC = False


def load_pillow():
    global Image, ImageOps, HEIC
    try:
        from PIL import Image, ImageOps
    except ImportError:
        sys.exit("Some places need processing but Pillow is missing; run: pip3 install Pillow")
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        HEIC = True
    except ImportError:
        HEIC = False


# ---------- data/cities.js ----------

def read_cities(html):
    """[(id, English name)] in data/cities.js order."""
    return re.findall(r'\{id:"([^"]+)",\s*en:"([^"]+)"', html)


def city_span(html, city_id):
    start = html.find('{id:"%s"' % city_id)
    ends = [i for i in (html.find("\n  {id:", start + 1), html.find("\n];", start + 1)) if i != -1]
    return start, min(ends)


PHOTO_ITEM = re.compile(r'\{[^}]*\}|"[^"]*"')


def read_marks(html, city_id):
    """A place's current photo list → [(file name, roll or None)]. Accepts "01.jpg" and {file:"01.jpg", film:true, roll:"…"}."""
    start, end = city_span(html, city_id)
    m = re.search(r"photos:\[([^\]]*)\]", html[start:end])
    out = []
    for item in PHOTO_ITEM.findall(m.group(1) if m else ""):
        if item.startswith("{"):
            f = re.search(r'file:\s*"([^"]+)"', item)
            film = re.search(r"film:\s*true", item)
            roll = re.search(r'roll:\s*"([^"]*)"', item)
            if f:
                out.append((f.group(1), (roll.group(1) if roll else "") if film else None))
        else:
            out.append((item.strip('"'), None))
    return out


def scan_name(key):
    """Original file name (without extension), stored as a film photo's scan; the page orders frames by it (it carries the lab's frame number)."""
    return re.sub(r'["\\]', "", Path(key).stem)


def photo_entry(name, roll, scan=None):
    if roll is None:
        return '"%s"' % name
    return '{file:"%s", film:true, roll:"%s"%s}' % (name, roll, ', scan:"%s"' % scan if scan else "")


def update_cities(html, city_id, entries):
    """entries: [(file name, roll or None, original name or None)]"""
    start, end = city_span(html, city_id)
    entry = html[start:end]
    listing = "photos:[" + ",".join(photo_entry(*e) for e in entries) + "]"
    if re.search(r"photos:\[[^\]]*\]", entry):
        entry = re.sub(r"photos:\[[^\]]*\]", lambda _: listing, entry, count=1)
    else:
        close = entry.rfind("}")
        entry = entry[:close] + ",\n   " + listing + entry[close:]
    return html[:start] + entry + html[end:]


# ---------- photos ----------

def natural(name):
    """Natural sort for file names: 2.jpg before 10.jpg (scan frame numbers)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def is_photo(p):
    return p.is_file() and not p.name.startswith(".") and p.suffix.lower() in EXTS


def originals(folder):
    """A place's originals: [(path, key, roll or None)]. Regular photos are keyed by file name; photos in film/<roll>/ by
    "film/<roll>/<file name>", the roll being the folder name."""
    out = [(p, p.name, None) for p in sorted(folder.iterdir()) if is_photo(p)]
    film = folder / FILM_DIR
    if film.is_dir():
        for roll in sorted(d for d in film.iterdir() if d.is_dir() and not d.name.startswith(".")):
            out += [(p, f"{FILM_DIR}/{roll.name}/{p.name}", roll.name) for p in sorted(roll.iterdir(), key=lambda p: natural(p.name)) if is_photo(p)]
    return out


def snapshot(items):
    """key → [size, modification time], compared with the previous run."""
    return {key: [p.stat().st_size, p.stat().st_mtime_ns] for p, key, _ in items}


def taken_at(path):
    """Capture time; the file's modification time when it can't be read."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            raw = exif.get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL) or exif.get(DATETIME)
        if raw:
            return datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def output_names(city_id):
    folder = PHOTOS / city_id
    return sorted((p.name for p in folder.glob("*.jpg") if p.stem.isdigit()), key=lambda s: int(s[:-4])) if folder.is_dir() else []


def expected_names(n):
    return [f"{i:02d}.jpg" for i in range(1, n + 1)]


def carry_marks(html, cid, items, manifest):
    """Photos marked as film by hand (the film mark is in data/cities.js, the original sits in the regular folder): using the
    "output file → original" record, carry the mark over to the original so it survives renumbering. Returns {original key: roll};
    raises when they can't be matched up — better not to process than to lose a mark."""
    marked = {n: r for n, r in read_marks(html, cid) if r is not None}
    if not marked:
        return {}
    outputs = manifest.get("_map", {}).get(cid)
    if outputs is None:
        # older records have no mapping: if the originals are unchanged, the previous order is the current sort (capture time, name)
        old = manifest.get(cid, {})
        regular = [(p, k) for p, k, r in items if r is None]
        names = [n for n, _ in read_marks(html, cid)]
        if set(old) != {k for _, k in regular} or len(names) != len(regular):
            raise RuntimeError("there are photos marked as film by hand that can't be matched to originals (originals were added or removed); "
                               "move their originals into film/<roll>/ first")
        outputs = {n: k for n, (_, k, _) in zip(names, ordered(items))}
    result, keys = {}, {k for _, k, _ in items}
    in_film = {p.name for p, _, r in items if r is not None}
    for name, roll in marked.items():
        key = outputs.get(name)
        if key is not None and key.startswith(FILM_DIR + "/"):
            continue        # already in film/<roll>/: marked by its folder, not by hand; if the original is gone the photo goes too
        if key in keys:
            result[key] = roll
        elif key is not None and Path(key).name in in_film:
            continue        # the original has moved into film/<roll>/: the folder marks it now, no need to carry the manual mark
        else:
            raise RuntimeError(f"{name} is marked as film by hand but its original is missing; put the original in film/{roll}/ first")
    return result


def ordered(items):
    """Output order: regular photos (including ones marked as film by hand) by capture time, then those in film/ (by roll, then file name)."""
    plain = sorted((i for i in items if i[2] is None), key=lambda i: (taken_at(i[0]), i[0].name))
    film = sorted((i for i in items if i[2] is not None), key=lambda i: (natural(i[2]), natural(i[0].name)))
    return plain + film


def rebuild(city_id, items, carried):
    """Regenerate photos/<id>/ as a whole: write to a temporary folder first and swap in once everything succeeded.
    Returns (entries [(file name, roll or None, original name or None)], {output file name: original key})."""
    ordered_items = [(p, k, r if r is not None else carried.get(k)) for p, k, r in ordered(items)]
    out = PHOTOS / city_id
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PHOTOS) as tmp:
        entries, origin = [], {}
        for i, (p, key, roll) in enumerate(ordered_items, 1):
            name = f"{i:02d}.jpg"
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im)  # rotate upright first, or the photo ends up sideways once EXIF is dropped
                icc = im.info.get("icc_profile")
                im = im.convert("RGB")
                im.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)   # only scales down, never crops, keeps the aspect ratio
                im.save(Path(tmp) / name, "JPEG", quality=FILM_QUALITY if roll is not None else QUALITY, optimize=True, progressive=True,
                        **({"icc_profile": icc} if icc else {}))
            entries.append((name, roll, scan_name(key) if roll is not None else None))
            origin[name] = key
        # Not delete-then-create: a sync client such as iCloud can apply the deletion of the old 01.jpg late and remove the new file
        # of the same name with it. Unchanged files stay put, changed ones are overwritten in place with os.replace, and only
        # files that are no longer needed are deleted at the end
        names = {e[0] for e in entries}
        for name in sorted(names):
            new, dest = Path(tmp) / name, out / name
            if dest.exists() and dest.read_bytes() == new.read_bytes():
                continue
            os.replace(new, dest)
        for old in out.glob("*.jpg"):
            if old.name not in names:
                old.unlink()
    # in a folder synced by iCloud, freshly written files occasionally go missing: check again after writing
    if output_names(city_id) != [e[0] for e in entries]:
        raise RuntimeError(f"photos/{city_id}/ is incomplete after writing, possibly touched by a sync client; the next run redoes it")
    return entries, origin


# ---------- roll table film/rolls.csv ----------

def add_rolls(html):
    """Rolls that appear in data/cities.js but not yet in rolls.csv get a line (roll name only, the rest is up to you).
    Existing lines are never touched."""
    seen = []
    for m in re.finditer(r'roll:\s*"([^"]+)"', html[html.find("const CITIES"):]):   # skip the example in the header comment
        if m.group(1) not in seen:
            seen.append(m.group(1))
    raw = ROLLS_CSV.read_bytes().decode("utf-8-sig") if ROLLS_CSV.exists() else ""
    rows = list(csv.reader(raw.splitlines()))
    header = rows[0] if rows else ["roll", "film_stock", "iso", "format", "camera", "lens", "date_from", "date_to", "lab", "note"]
    have = {r[0].strip() for r in rows[1:] if r}
    new = [r for r in seen if r not in have]
    if not new:
        return []
    ROLLS_CSV.parent.mkdir(exist_ok=True)
    text = raw if rows else ",".join(header) + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    for r in new:
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow([r] + [""] * (len(header) - 1))   # quoted automatically if the name has commas or quotes
        text += buf.getvalue()
    ROLLS_CSV.write_bytes((("﻿" if ROLLS_CSV.exists() and ROLLS_CSV.read_bytes().startswith(b"\xef\xbb\xbf") else "") + text).encode("utf-8"))
    return new


# ---------- main ----------

def main():
    html = CITIES_JS.read_text(encoding="utf-8")
    cities = read_cities(html)
    known = {cid for cid, _ in cities}

    # 1. folders: existing ones are recognised by the id before the first space
    INBOX.mkdir(exist_ok=True)
    folders, unknown, duplicate = {}, [], []
    for d in sorted(p for p in INBOX.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cid = d.name.split(" ", 1)[0]
        if cid not in known:
            unknown.append(d.name)
        elif cid in folders:
            duplicate.append(d.name)
        else:
            folders[cid] = d
    created, film_created, loose = [], 0, []
    for cid, name in cities:
        if cid not in folders:
            folders[cid] = INBOX / f"{cid} {name}"
            folders[cid].mkdir()
            created.append(folders[cid].name)
        # every place folder has a film/ for film/<roll>/; existing ones are left alone
        film = folders[cid] / FILM_DIR
        if not film.exists():
            film.mkdir()
            film_created += 1
        elif film.is_dir():
            stray = [p.name for p in sorted(film.iterdir()) if is_photo(p)]
            if stray:
                loose.append((folders[cid].name, stray))

    # 2. compare with the last run to find the places to process
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    todo, unchanged, empty, protected, failed, seed = [], [], [], [], [], []
    for cid, _ in cities:
        items = originals(folders[cid])
        if not items:
            existing = output_names(cid)
            (protected if existing else empty).append((cid, len(existing)))
            continue
        snap = snapshot(items)
        if manifest.get(cid) == snap and output_names(cid) == expected_names(len(items)):
            unchanged.append(cid)
            if cid not in manifest.get("_map", {}):
                seed.append((cid, items))
        else:
            todo.append((cid, items, snap))

    # unchanged places also get an "output file → original" record (_map), so hand-marked film photos follow their originals
    # when new originals arrive and everything is renumbered
    if seed:
        load_pillow()
        for cid, items in seed:
            manifest.setdefault("_map", {})[cid] = {f"{i:02d}.jpg": k for i, (_, k, _) in enumerate(ordered(items), 1)}

    # unchanged places: add scan (the original name the page orders frames by) to film photos. Only that field in data/cities.js changes
    for cid in unchanged:
        origin = manifest.get("_map", {}).get(cid, {})
        entries = [(n, r, scan_name(origin[n]) if r is not None and n in origin else None) for n, r in read_marks(html, cid)]
        if any(r is not None for _, r, _ in entries):
            html = update_cities(html, cid, entries)

    # 3. regenerate
    updated, film_counts = [], {}
    if todo:
        load_pillow()
        for cid, items, snap in todo:
            heic = [p.name for p, _, _ in items if p.suffix.lower() in HEIC_EXTS]
            if heic and not HEIC:
                failed.append((cid, f"{len(heic)} HEIC photos; run pip3 install pillow-heif first"))
                continue
            try:
                carried = carry_marks(html, cid, items, manifest)
                entries, origin = rebuild(cid, items, carried)
            except Exception as e:
                failed.append((cid, f"{type(e).__name__}: {e}"))
                continue
            html = update_cities(html, cid, entries)
            manifest[cid] = snap
            manifest.setdefault("_map", {})[cid] = origin
            updated.append((cid, len(entries)))
            film_counts[cid] = sum(1 for e in entries if e[1] is not None)
            print(f"  {cid}: {len(entries)} photos" + (f" ({film_counts[cid]} film)" if film_counts[cid] else ""))
    if html != CITIES_JS.read_text(encoding="utf-8"):
        CITIES_JS.write_text(html, encoding="utf-8")
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    new_rolls = add_rolls(html)

    # 4. summary
    print("\n========== Summary ==========")
    if created:
        print(f"Created {len(created)} folders: " + ", ".join(created))
    if film_created:
        print(f"Added {film_created} missing film/ folders")
    print(f"Updated {len(updated)} places: " + (", ".join(f"{c} ({n} photos)" for c, n in updated) or "none"))
    if new_rolls:
        print("New rolls added to film/rolls.csv (name only; please add the film stock, camera, etc.): " + ", ".join(new_rolls))
    if unchanged:
        print(f"Skipped {len(unchanged)} (originals unchanged): " + ", ".join(unchanged))
    if protected:
        print("⚠ Inbox empty but the site already has photos; left as is: " + ", ".join(f"{c} ({n} photos)" for c, n in protected))
    if empty:
        print(f"Skipped {len(empty)} (no originals yet): " + ", ".join(c for c, _ in empty))
    if loose:
        print("⚠ These photos sit directly in film/ and were not processed. Put them in a roll folder, e.g. film/2025-06 Portra400/:")
        for folder, names in loose:
            print(f"    {folder}/{FILM_DIR}/: " + ", ".join(names))
    if failed:
        print("✗ Failed; retried on the next run:")
        for c, why in failed:
            print(f"    {c}: {why}")
    if unknown:
        print("⚠ These folders in originals/ match no city id and were not processed:")
        for name in unknown:
            print(f"    {name}")
    if duplicate:
        print("⚠ These folders share a city id with another folder and were not processed:")
        for name in duplicate:
            print(f"    {name}")


if __name__ == "__main__":
    main()
