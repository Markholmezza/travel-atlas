#!/usr/bin/env python3
"""Changelog: turn the git history into data/changelog.json for the site (the sidebar's "Changelog" reads it).

Usage (no arguments):
    python3 tools/build_changelog.py

  One entry per commit, newest first: time (UTC seconds; the page shows US Eastern Time), message,
  and tags from the files the commit touched (site / places / photos / film / articles / transport / docs).
  The message comes from data/changelog-text.csv when it has a line for the commit (commit = short hash, text = what the site shows),
  otherwise from the first line of the commit message. Pushed commit messages can't change, so reword them in that table.
  Bot commits (e.g. github-actions[bot]), merge commits, and commits that only touch changelog-text.csv are left out.
  When the log is empty (as in this demo's fresh history), the page shows no changelog at all.
"""

import csv
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "changelog.json"
TEXT_CSV = ROOT / "data" / "changelog-text.csv"

# Tags: a commit gets a tag when it touches a path starting with one of these (several tags per commit, in this order)
TAGS = [
    ("site", ("index.html", "assets/app.js", "assets/style.css")),
    ("places", ()),                   # a place's own line in data/cities.js changed (photo list changes don't count), see place_commits
    ("photos", ("photos/",)),
    ("film", ("film/",)),
    ("articles", ("articles_inbox/", "data/articles.js")),
    ("transport", ("flights/", "routes/", "assets/airlines/", "assets/operators/")),
    ("docs", ("README.md",)),
]


def git(*args):
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True, encoding="utf-8").stdout


def place_commits():
    """Commits that changed a place itself: the content of its {id:"…" line in cities.js changed.
    The photo list is on the next line; when photos are first added, only the trailing } of this line turns into a comma (or back),
    so the line is compared without its ending, or syncing photos would count as editing the place."""
    out, sha, old, new = set(), None, [], []

    def flush():
        if sha and sorted(old) != sorted(new):
            out.add(sha)

    for line in git("log", "--no-merges", "-p", "-U0", "--format=%x1e%H", "--", "data/cities.js").split("\n"):   # not splitlines: it also breaks at \x1e
        if line.startswith("\x1e"):
            flush()
            sha, old, new = line[1:], [], []
        elif line[:1] in "-+" and line[1:].lstrip().startswith('{id:"'):
            body = line[1:].strip()
            body = body.split("photos:")[0].rstrip().rstrip(",}").rstrip()
            (old if line[0] == "-" else new).append(body)
    flush()
    return out


def read_texts():
    """data/changelog-text.csv → {short hash (first 7 characters): message shown on the site}."""
    if not TEXT_CSV.exists():
        return {}
    with TEXT_CSV.open(encoding="utf-8-sig", newline="") as f:
        return {r["commit"].strip()[:7]: r["text"].strip() for r in csv.DictReader(f) if r.get("commit") and r.get("text", "").strip()}


def build():
    places, texts = place_commits(), read_texts()
    entries = []
    for block in git("log", "--no-merges", "--name-only", "--format=%x1e%H%x1f%at%x1f%an%x1f%s").split("\x1e")[1:]:
        head, _, files = block.partition("\n")
        sha, at, author, subject = head.split("\x1f")
        if author.endswith("[bot]"):
            continue
        files = [f for f in files.splitlines() if f]
        if files == ["data/changelog-text.csv"]:
            continue        # only rewords an older commit; the site itself didn't change
        tags =[name for name, prefixes in TAGS
                if (name == "places" and sha in places) or any(f.startswith(p) for f in files for p in prefixes)]
        entries.append({"t": int(at), "msg": texts.get(sha[:7]) or subject.strip(), "tags": tags})
    return entries


def main():
    entries = build()
    text = "[\n" + ",\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n]\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
        print(f"Changelog unchanged ({len(entries)} entries)")
        return
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(entries)} entries")


if __name__ == "__main__":
    main()
