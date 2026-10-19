#!/usr/bin/env python3
"""Article inbox: turn the files in articles_inbox/ into data/articles.js for the site.

Usage (no arguments):
    python3 tools/build_articles.py

  1. Reads every place in data/cities.js and creates a folder "<city id> <name>" for it in articles_inbox/
     (with a hidden .gitkeep so empty folders can be committed). Existing folders are left alone; only the id before the first space counts.
  2. File names are "YYYY-MM-DD Title.ext"; the date and title come from the name.
     Supports .docx .odt .rtf .html .md (converted with Pandoc), .txt (read directly), .pdf (text extracted with pdfplumber).
     .pages and other formats are listed in the summary with a hint to export to Word; nothing stops.
  3. Only paragraphs, headings, bold and italics are kept; other formatting is dropped and images are ignored (noted in the summary).
     If the first paragraph is "cover: 01.jpg", that photo becomes the cover and the line is removed; otherwise the place's first photo is used.
  4. Writes data/articles.js, grouped by place, oldest first.
     Article links: #<city id>.<date> (a second article on the same day is <date>-2), e.g. #buenos-aires.2025-11-03;
     the older #buenos-aires.a1 still works and means that place's first article by date.

Needs pandoc (brew install pandoc); PDFs need pdfplumber (pip3 install pdfplumber).
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "articles_inbox"
CITIES_JS = ROOT / "data" / "cities.js"
OUT = ROOT / "data" / "articles.js"

PANDOC_FORMATS = {".docx": "docx", ".odt": "odt", ".rtf": "rtf", ".html": "html", ".htm": "html", ".md": "markdown"}
SUPPORTED = set(PANDOC_FORMATS) | {".txt", ".pdf"}
NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$")
COVER = re.compile(r"^\s*cover\s*[:\uff1a]\s*(\S+)\s*$", re.I)
# CJK text in PDFs: lines ending in an ideograph or full-width punctuation, or starting with an ideograph, join without a space
CJK_END = re.compile("[\u3000-\u9fff\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u3001\uff09\u300b\u300d\u300f]$")
CJK_START = re.compile("[\u3000-\u9fff]")


# ---------- places ----------

def read_cities():
    """[(id, English name, [photo file names…])] in data/cities.js order. Photos may be "01.jpg" or {file:"01.jpg", film:true, …}."""
    js = CITIES_JS.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r'\{id:"([^"]+)",\s*en:"([^"]+)"', js):
        end = js.find("\n  {id:", m.end())
        entry = js[m.start(): end if end != -1 else len(js)]
        photos = re.search(r'photos:\[([^\]]*)\]', entry)
        out.append((m.group(1), m.group(2), [a or b for a, b in re.findall(r'\{[^}]*?file:\s*"([^"]+)"[^}]*\}|"([^"]+)"', photos.group(1))] if photos else []))
    return out


# ---------- body: paragraphs, headings, bold and italics only ----------

class Simplifier(HTMLParser):
    """Collapse Pandoc's HTML into a list of blocks: {"t": "p" | "h", "h": HTML containing only <strong> <em> <br>}."""
    BLOCKS = {"p", "li", "blockquote", "td", "th", "dd", "dt", "pre", "div", "tr", "table", "ul", "ol", "figure"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    DROP = {"script", "style", "head", "title", "noscript", "svg", "math", "figcaption"}   # images are ignored, captions too

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.cur, self.kind, self.skip, self.images = [], [], "p", 0, 0
        self.bold = self.italic = 0

    def flush(self):
        text = "".join(self.cur).strip()
        text = re.sub(r"\s*<br>\s*", "<br>", text)
        text = text.replace("</strong><strong>", "").replace("</em><em>", "")   # merge adjacent runs of the same style
        text = re.sub(r"(<br>)+$", "", re.sub(r"^(<br>)+", "", text)).strip()
        plain = re.sub(r"<[^>]+>", "", text).strip()
        if plain:
            self.blocks.append({"t": self.kind, "h": text})
        self.cur, self.kind = [], "p"

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.skip += 1
        elif self.skip:
            return
        elif tag in self.HEADINGS:
            self.flush()
            self.kind = "h"
        elif tag in self.BLOCKS:
            self.flush()
        elif tag in ("strong", "b"):
            self.cur.append("<strong>")
            self.bold += 1
        elif tag in ("em", "i"):
            self.cur.append("<em>")
            self.italic += 1
        elif tag == "br":
            self.cur.append("<br>")
        elif tag == "img":
            self.images += 1

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in self.HEADINGS or tag in self.BLOCKS:
            self.flush()
        elif tag in ("strong", "b") and self.bold:
            self.cur.append("</strong>")
            self.bold -= 1
        elif tag in ("em", "i") and self.italic:
            self.cur.append("</em>")
            self.italic -= 1

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(html.escape(re.sub(r"\s+", " ", data), quote=False))

    def result(self):
        self.flush()
        return self.blocks, self.images


def tidy_rtf(path):
    """In RTF saved by Apple TextEdit and Word, a backslash at the end of a line is a paragraph break, which Pandoc reads as a line break;
    Pandoc also moves lists to the top. Turn trailing backslashes into \\par and drop list markers (lists end up as paragraphs anyway)."""
    s = path.read_bytes().decode("latin-1")            # RTF itself is ASCII; latin-1 round-trips it unchanged
    s = re.sub(r"(?<!\\)\\\r?\n", "\\\\par\n", s)
    s = re.sub(r"\{\\listtext[^{}]*\}", "", s)
    s = re.sub(r"\\ls\d+ ?|\\ilvl\d+ ?", "", s)
    tmp = tempfile.NamedTemporaryFile(suffix=".rtf", delete=False)
    tmp.write(s.encode("latin-1"))
    tmp.close()
    return Path(tmp.name)


def from_pandoc(path, fmt):
    if not shutil.which("pandoc"):
        raise RuntimeError("Pandoc isn't installed: brew install pandoc")
    src = tidy_rtf(path) if fmt == "rtf" else path
    try:
        run = subprocess.run(["pandoc", str(src), "-f", fmt, "-t", "html5", "--wrap=none"],
                             capture_output=True, text=True, timeout=120)
    finally:
        if src != path:
            src.unlink(missing_ok=True)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip().splitlines()[-1] if run.stderr.strip() else "Pandoc conversion failed")
    p = Simplifier()
    p.feed(run.stdout)
    return p.result()


def paragraphs_from_text(text, join_lines):
    """Blank lines separate paragraphs. join_lines decides how lines within a paragraph join (txt keeps line breaks, PDF joins them)."""
    blocks = []
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [l.strip() for l in para.split("\n") if l.strip()]
        if lines:
            blocks.append({"t": "p", "h": join_lines([html.escape(l, quote=False) for l in lines])})
    return blocks


def from_txt(path):
    raw = path.read_bytes()
    encodings = (("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + ("utf-8-sig", "cp1252")
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("couldn't detect the text encoding; save it as UTF-8")
    return paragraphs_from_text(text, lambda ls: "<br>".join(ls)), 0


def from_pdf(path):
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("PDFs need pdfplumber: pip3 install pdfplumber")
    # PDFs have no blank lines: split paragraphs where the gap between lines is clearly larger than the usual line spacing
    paras = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines = [l for l in page.extract_text_lines() if l["text"].strip()]
            if not lines:
                continue
            gaps = sorted(b["top"] - a["bottom"] for a, b in zip(lines, lines[1:]))
            height = sorted(l["bottom"] - l["top"] for l in lines)[len(lines) // 2]
            normal = gaps[len(gaps) // 2] if gaps else 0
            cur = [lines[0]["text"]]
            for a, b in zip(lines, lines[1:]):
                if b["top"] - a["bottom"] > normal + 0.5 * height:
                    paras.append(cur)
                    cur = []
                cur.append(b["text"])
            paras.append(cur)
    # PDF fonts often store some ideographs as look-alike Kangxi radicals (U+2F00…); map those back without a full NFKC pass,
    # which would also turn full-width punctuation into half-width
    fix = lambda t: re.sub(r"[\u2e80-\u2fdf]", lambda m: unicodedata.normalize("NFKC", m.group()), t)
    text = "\n\n".join("\n".join(fix(l) for l in p) for p in paras)

    def join(lines):     # every PDF line ends in a hard break: CJK text joins directly, other text with a space
        out = lines[0]
        for l in lines[1:]:
            out += ("" if CJK_END.search(out) or CJK_START.match(l) else " ") + l
        return out
    return paragraphs_from_text(text, join), 0


def convert(path):
    ext = path.suffix.lower()
    if ext == ".txt":
        return from_txt(path)
    if ext == ".pdf":
        return from_pdf(path)
    return from_pandoc(path, PANDOC_FORMATS[ext])


# ---------- main ----------

def main():
    cities = read_cities()
    known = {cid: (name, photos) for cid, name, photos in cities}

    INBOX.mkdir(exist_ok=True)
    folders, unknown, duplicate, created = {}, [], [], []
    for d in sorted(p for p in INBOX.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cid = d.name.split(" ", 1)[0]
        if cid not in known:
            unknown.append(d.name)
        elif cid in folders:
            duplicate.append(d.name)
        else:
            folders[cid] = d
    for cid, name, _ in cities:
        if cid not in folders:
            folders[cid] = INBOX / f"{cid} {name}"
            folders[cid].mkdir()
            created.append(folders[cid].name)
        keep = folders[cid] / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    articles, bad_name, unsupported, failed, notes = {}, [], [], [], []
    for cid, _, _ in cities:
        for f in sorted(p for p in folders[cid].iterdir() if p.is_file() and not p.name.startswith(".")):
            rel = f"{folders[cid].name}/{f.name}"
            ext = f.suffix.lower()
            if ext not in SUPPORTED:
                unsupported.append(rel)
                continue
            m = NAME.match(f.stem)
            try:
                if not m:
                    raise ValueError
                datetime.strptime(m.group(1), "%Y-%m-%d")
            except ValueError:
                bad_name.append(rel)
                continue
            try:
                blocks, images = convert(f)
            except Exception as e:
                failed.append(f"{rel}: {e}")
                continue
            cover = None
            if blocks and blocks[0]["t"] == "p":
                cm = COVER.match(re.sub(r"<[^>]+>", "", blocks[0]["h"]))
                if cm:
                    cover = cm.group(1)
                    blocks = blocks[1:]
            if images:
                notes.append(f"{rel}: contains {images} images, ignored (pages only use the cover and the place's photos)")
            if ext == ".pdf":
                notes.append(f"{rel}: paragraph breaks in PDFs can be off; please check")
            if not blocks:
                notes.append(f"{rel}: the body is empty")
            photos = known[cid][1]
            if cover and cover not in photos:     # a cover was named but the place has no such photo: use the first one instead
                notes.append(f"{rel}: cover {cover} isn't one of this place's photos; " + ("using " + photos[0] if photos else "the place has no photos yet, so no cover"))
                cover = None
            articles.setdefault(cid, []).append({
                "date": m.group(1), "title": m.group(2).strip(),
                "cover": cover or (photos[0] if photos else None), "blocks": blocks, "source": f.name,
            })

    # oldest first; a second article on the same day gets <date>-2 as its link id
    for cid, items in articles.items():
        items.sort(key=lambda a: (a["date"], a["title"]))
        seen = {}
        for a in items:
            seen[a["date"]] = seen.get(a["date"], 0) + 1
            a["slug"] = a["date"] if seen[a["date"]] == 1 else f"{a['date']}-{seen[a['date']]}"
    ordered = {cid: articles[cid] for cid, _, _ in cities if cid in articles}

    body = json.dumps(ordered, ensure_ascii=False, indent=1)
    OUT.write_text("// Generated by tools/build_articles.py from articles_inbox/; don't edit by hand.\n"
                   "// Each article: date, title, cover, blocks (t: p paragraph / h heading), slug (link id), source (original file name)\n"
                   f"const ARTICLES = {body};\n", encoding="utf-8")

    # summary
    total = sum(len(v) for v in ordered.values())
    print("========== Articles ==========")
    if created:
        print(f"Created {len(created)} folders: " + ", ".join(created))
    print(f"Wrote {OUT.relative_to(ROOT)}: {total} articles, {len(ordered)} places")
    for cid, items in ordered.items():
        for a in items:
            print(f"    #{cid}.{a['slug']}  {a['date']} {a['title']} ({a['source']}, {len(a['blocks'])} blocks, cover {a['cover'] or 'none'})")
    for n in notes:
        print("⚠ " + n)
    if bad_name:
        print("⚠ File name isn't \"YYYY-MM-DD Title.ext\"; not processed:")
        for r in bad_name:
            print(f"    articles_inbox/{r}")
    if unsupported:
        print("⚠ Unsupported format; export to Word (.docx) and add it again:")
        for r in unsupported:
            print(f"    articles_inbox/{r}")
    for r in failed:
        print(f"✗ Conversion failed: articles_inbox/{r}")
    for name in unknown:
        print(f"⚠ articles_inbox/{name} matches no city id; not processed")
    for name in duplicate:
        print(f"⚠ articles_inbox/{name} shares a city id with another folder; not processed")


if __name__ == "__main__":
    main()   # finishes normally even if some files failed: the other articles are still generated
