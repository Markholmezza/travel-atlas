#!/usr/bin/env python3
"""Contact sheet for reviewing which photos show people (pairs with tools/detect_people.swift).

Usage:
    swiftc -O tools/detect_people.swift -o /tmp/detect_people
    /tmp/detect_people <photos-dir> > review/detections.json
    python3 tools/contact_sheet.py <photos-dir> [--detections review/detections.json] [--out review]

Writes <out>/contact_sheet.html plus thumbnails in <out>/thumbs/ and larger previews in <out>/large/
(both re-encoded without metadata). Photos are split into "Suggested: keep" and "Suggested: remove";
each card shows the city, file name, film roll, and the detection boxes and reasons. Every card has a
Keep checkbox, and the panel at the top turns the choices into a list that can be copied.
City names and film marks come from data/cities.js. review/ is in .gitignore.
Needs Pillow (pip3 install Pillow).
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THUMB, LARGE = 480, 1600


def read_cities(path):
    """data/cities.js → {id: {"name": English name, "film": {file: roll}}}. Photo items may be "01.jpg" or {file:..., film:true, roll:...}."""
    text = path.read_text(encoding="utf-8")
    cities = {}
    starts = [m.start() for m in re.finditer(r'\{id:"', text)]
    for a, b in zip(starts, starts[1:] + [len(text)]):
        block = text[a:b]
        cid = re.match(r'\{id:"([^"]+)"', block).group(1)
        name = re.search(r'\ben:"([^"]*)"', block)
        film = {m.group(1): m.group(2) for m in
                re.finditer(r'\{file:"([^"]+)",\s*film:true(?:,\s*roll:"([^"]*)")?', block)}
        cities[cid] = {"name": name.group(1) if name else cid, "film": {f: r or "" for f, r in film.items()}}
    return cities


def save_preview(src, dst, size):
    from PIL import Image, ImageOps
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")      # upright, like the boxes from Vision
        im.thumbnail((size, size), getattr(Image, "Resampling", Image).LANCZOS)
        im.save(dst, "JPEG", quality=80, optimize=True)      # no exif / icc passed on: previews carry no metadata


def card(d, city, keep):
    key = f'{d["city"]}/{d["file"]}'
    roll = city["film"].get(d["file"])
    boxes = "".join(
        f'<i class="{kind}" style="left:{b["x"]*100:.2f}%;top:{b["y"]*100:.2f}%;width:{b["w"]*100:.2f}%;height:{b["h"]*100:.2f}%"'
        f' title="{kind} {b["h"]*100:.0f}% tall, confidence {b["confidence"]:.2f}"></i>'
        for b in d["faces"] + d["figures"] for kind in [b.get("source", "face")])
    tags = []
    if roll is not None:
        tags.append(f'<span class="tag film">FILM{" · " + html.escape(roll) if roll else ""}</span>')
    if d["verdict"] == "keep":
        if d["smallFigures"]:
            tags.append(f'<span class="tag small">small figures ×{d["smallFigures"]}</span>')
        if d.get("unconfirmedFigures"):
            tags.append(f'<span class="tag check">check: pose-only figure ×{d["unconfirmedFigures"]}</span>')
        for s in d.get("scene", []):
            tags.append(f'<span class="tag check">scene: {html.escape(s["label"])} {s["confidence"]:.2f}</span>')
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in d["reasons"])
    return f'''<figure class="card" data-key="{html.escape(key)}" data-auto="{d["verdict"]}">
  <a class="pic" href="large/{html.escape(key)}" data-big="large/{html.escape(key)}"><img src="thumbs/{html.escape(key)}" alt="" loading="lazy">{boxes}</a>
  <figcaption>
    <label class="pick"><input type="checkbox"{" checked" if keep else ""}> Keep</label>
    <b>{html.escape(city["name"])}</b> <code>{html.escape(key)}</code>
    <div class="tags">{"".join(tags)}</div>
    {f'<ul class="why">{reasons}</ul>' if reasons else ""}
  </figcaption>
</figure>'''


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Review</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--fg:#1d1c1a;--muted:#6d6a64;--line:#dedad3;--keep:#2f7d4f;--remove:#b3402e;--face:#e5484d;--figure:#f0a020;--pose:#12a5c4;--film:#5b5bd6}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--panel:#21201e;--fg:#ecebe8;--muted:#a19d96;--line:#37352f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:18px;margin:0 0 4px}
header p{margin:0;color:var(--muted)}
.controls{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-top:8px}
button{font:inherit;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:inherit;cursor:pointer}
details{margin-top:8px}
textarea{width:100%;height:160px;font:12px/1.4 ui-monospace,monospace;background:var(--bg);color:inherit;border:1px solid var(--line);border-radius:6px;padding:8px}
main{padding:16px}
h2{font-size:16px;margin:24px 0 10px;display:flex;gap:8px;align-items:baseline}
h2 small{color:var(--muted);font-weight:400}
h2.keep{color:var(--keep)} h2.remove{color:var(--remove)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;align-items:start}
.card{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.card.changed{outline:3px solid var(--film)}
.pic{position:relative;display:block;line-height:0}
.pic img{width:100%;height:auto}
.pic i{position:absolute;border:2px solid;border-radius:2px;pointer-events:none}
.pic i.face{border-color:var(--face)} .pic i.human{border-color:var(--figure);border-style:dashed} .pic i.pose{border-color:var(--pose);border-style:dotted}
body.noboxes .pic i{display:none}
figcaption{padding:8px 10px;font-size:13px}
figcaption code{color:var(--muted);font-size:12px}
.pick{float:right;font-weight:600;cursor:pointer}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;color:#fff}
.tag.film{background:var(--film)} .tag.small{background:var(--figure);color:#000} .tag.check{background:var(--face)}
.why{margin:4px 0 0;padding-left:18px;color:var(--remove);font-size:12px}
.legend i{display:inline-block;width:12px;height:12px;border:2px solid;vertical-align:-2px;margin-right:4px}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.88);display:flex;align-items:center;justify-content:center;z-index:10}
#lb[hidden]{display:none}
#lb img{max-width:96vw;max-height:92vh}
</style>
</head>
<body>
<header>
  <h1>Photo review: which photos show people</h1>
  <p>__SUMMARY__</p>
  <div class="controls">
    <span class="legend"><i style="border-color:var(--face)"></i>face <i style="border-color:var(--figure);border-style:dashed"></i>human rectangle <i style="border-color:var(--pose);border-style:dotted"></i>body pose</span>
    <label><input type="checkbox" id="boxes" checked> show boxes</label>
    <span id="count"></span>
    <button type="button" id="copy">Copy decision list</button>
  </div>
  <details><summary>Decision list (changes from the suggestion, then the full keep list)</summary><textarea id="out" readonly></textarea></details>
</header>
<main>
  <h2 class="keep">Suggested: keep <small>__NKEEP__ photos — no face, no figure taller than __MINH__% of the frame</small></h2>
  <div class="grid">__KEEP__</div>
  <h2 class="remove">Suggested: remove <small>__NREMOVE__ photos</small></h2>
  <div class="grid">__REMOVE__</div>
</main>
<div id="lb" hidden><img alt=""></div>
<script>
const cards = [...document.querySelectorAll(".card")];
function update(){
  const keep = [], toKeep = [], toRemove = [];
  cards.forEach(c => {
    const on = c.querySelector("input").checked, auto = c.dataset.auto === "keep";
    c.classList.toggle("changed", on !== auto);
    if(on) keep.push(c.dataset.key);
    if(on && !auto) toKeep.push(c.dataset.key);
    if(!on && auto) toRemove.push(c.dataset.key);
  });
  document.getElementById("count").textContent = `${keep.length} kept · ${cards.length - keep.length} removed · ${toKeep.length + toRemove.length} changed`;
  document.getElementById("out").value =
    `# Changes from the suggestion\\nkeep (suggested remove): ${toKeep.join(", ") || "none"}\\nremove (suggested keep): ${toRemove.join(", ") || "none"}\\n\\n# Final keep list (${keep.length})\\n${keep.join("\\n")}\\n`;
}
document.addEventListener("change", e => { if(e.target.closest(".pick")) update(); });
document.getElementById("boxes").onchange = e => document.body.classList.toggle("noboxes", !e.target.checked);
document.getElementById("copy").onclick = () => { const t = document.getElementById("out"); t.closest("details").open = true; t.select(); navigator.clipboard?.writeText(t.value).catch(()=>document.execCommand("copy")); };
const lb = document.getElementById("lb");
document.addEventListener("click", e => {
  const a = e.target.closest(".pic"); if(a){ e.preventDefault(); lb.querySelector("img").src = a.dataset.big; lb.hidden = false; return; }
  if(e.target.closest("#lb")) lb.hidden = true;
});
addEventListener("keydown", e => { if(e.key === "Escape") lb.hidden = true; });
update();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("photos", type=Path, help="photo folder laid out as <city>/<file>.jpg")
    ap.add_argument("--detections", type=Path, default=ROOT / "review" / "detections.json")
    ap.add_argument("--cities", type=Path, default=ROOT / "data" / "cities.js")
    ap.add_argument("--out", type=Path, default=ROOT / "review")
    ap.add_argument("--min-height", type=float, default=0.10, help="the threshold used for detect_people (for the caption)")
    a = ap.parse_args()
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Needs Pillow: pip3 install Pillow")

    detections = json.loads(a.detections.read_text(encoding="utf-8"))
    cities = read_cities(a.cities)
    groups = {"keep": [], "remove": []}
    for d in detections:
        src = a.photos / d["city"] / d["file"]
        save_preview(src, a.out / "thumbs" / d["city"] / d["file"], THUMB)
        save_preview(src, a.out / "large" / d["city"] / d["file"], LARGE)
        city = cities.get(d["city"], {"name": d["city"], "film": {}})
        groups[d["verdict"]].append(card(d, city, d["verdict"] == "keep"))

    kept = [d for d in detections if d["verdict"] == "keep"]
    small = sum(1 for d in kept if d["smallFigures"])
    check = sum(1 for d in kept if d.get("unconfirmedFigures") or d.get("scene"))
    summary = (f"{len(detections)} photos checked with Apple Vision (faces, full-body human rectangles and body pose, "
               f"on the whole frame and on overlapping tiles; scene labels as a cross-check). "
               f"{len(groups['keep'])} suggested keep ({small} with small figures, {check} marked check), "
               f"{len(groups['remove'])} suggested remove. Statues and posters can trigger the detectors, and very small, "
               "distant people can slip through, so scan both groups by eye. "
               "Click a photo to enlarge; untick Keep to drop it, or tick it to rescue one.")
    page = (PAGE.replace("__SUMMARY__", html.escape(summary))
            .replace("__NKEEP__", str(len(groups["keep"]))).replace("__NREMOVE__", str(len(groups["remove"])))
            .replace("__MINH__", f"{a.min_height * 100:g}")
            .replace("__KEEP__", "\n".join(groups["keep"])).replace("__REMOVE__", "\n".join(groups["remove"])))
    out = a.out / "contact_sheet.html"
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}: "
          f"{len(groups['keep'])} keep, {len(groups['remove'])} remove, {small} kept with small figures, {check} kept but marked check")


if __name__ == "__main__":
    main()
