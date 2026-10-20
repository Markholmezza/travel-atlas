#!/usr/bin/env python3
"""Tests for the data validation: tools/validate_data.py, tools/load_sqlite.py, db/schema.sql, db/checks/.

Usage:
    python3 tools/test_validation.py [-v]           run all tests (standard library unittest)
    python3 tools/test_validation.py --show EC-012  run the validator on one fixture and print what it reports

Fixtures (tests/fixtures/):
  _base/            a small, complete, clean site; it must raise no error and no warning (the negative control)
  EC-xxx_<what>/    one deliberate error for check EC-xxx: only the files that differ from _base, plus expected.txt
  OK-xxx_<what>/    a tricky but valid case kept as a regression test for a revised rule; it must raise no error
  expected.txt lists the Check IDs that must fire (the first is the check under test; # starts a comment).
  A test copies _base into a temporary directory, copies the overlay on top and validates that.

What is tested:
  - every fixture raises exactly the errors in its expected.txt, so the rule fires and nothing else breaks,
    plus the warnings it lists; the check under test is detected by the layer the plan names
  - the SQL layer on its own: for checks implemented as SQL constraints, tools/load_sqlite.py alone rejects the row
  - the clean base and the real data raise no error; exit codes are 1 with errors and 0 without
  - privacy: the denylisted term never appears in the terminal output, the Markdown report or the CSV report
  - traceability: every check in docs/validation_plan.md is implemented where the plan says and has a fixture;
    every Check ID in code is in the plan; SQL query files agree with the plan; every constraint in
    db/schema.sql maps to a Check ID
"""

import contextlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_data as ad      # noqa: E402
import load_sqlite           # noqa: E402
import validate_data as vd   # noqa: E402

ROOT = ad.ROOT
FIXTURES = ROOT / "tests" / "fixtures"
BASE = FIXTURES / "_base"
VALIDATE = ROOT / "tools" / "validate_data.py"
PLAN = ad.load_plan()
# The synthetic name in tests/fixtures/_base/tools/privacy_denylist.txt, in any spelling the matcher accepts
TERM = re.compile(r"quentin[\W_]*placeholder", re.I)


def cases():
    return sorted(p for p in FIXTURES.iterdir() if p.is_dir() and p.name != "_base")


def expected(case):
    lines = (case / "expected.txt").read_text(encoding="utf-8").splitlines()
    return [l.split("#", 1)[0].strip() for l in lines if l.split("#", 1)[0].strip()]


@contextlib.contextmanager
def site(case=None):
    """A temporary copy of _base with the case's overlay on top."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "site"
        shutil.copytree(BASE, root)
        if case:
            shutil.copytree(case, root, dirs_exist_ok=True)
            (root / "expected.txt").unlink()
        yield root


def validate(root):
    # use_env=False: fixtures use their own synthetic denylist only, so results do not depend on CI secrets
    return vd.validate(root, root / "build", use_env=False)


def run_cli(root):
    """The validator as CI and Cloudflare run it: a separate process (no inherited denylist variable)."""
    env = {k: v for k, v in os.environ.items() if k != ad.DENYLIST_ENV}
    return subprocess.run([sys.executable, str(VALIDATE), "--data-dir", str(root)],
                          capture_output=True, text=True, env=env, timeout=300)


class FixtureTests(unittest.TestCase):
    """Each fixture raises exactly the errors it was built to raise (test methods are added below)."""


class SqlLayerTests(unittest.TestCase):
    """The SQLite constraints catch the error without the Python layer (test methods are added below)."""

    def test_ec029_query_without_foreign_keys(self):
        # EC-029 is enforced by a foreign key while loading; the query must still find an orphan when a database
        # was filled without PRAGMA foreign_keys = ON
        with site() as root:
            db = root / "build" / "atlas.db"
            load_sqlite.load(ad.Dataset(root), db)
            conn = sqlite3.connect(str(db))
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE locations SET parent_id = 'brandenburg' WHERE id = 'potsdam'")
            conn.commit()
            conn.close()
            found = vd.run_sql_checks(db, PLAN)
        self.assertIn(("EC-029", "potsdam"), {(f.check_id, f.record_id) for f in found})


def _fixture_test(case):
    def test(self):
        want = expected(case)
        with site(case) as root:
            r = validate(root)
        listing = "\n" + "\n".join(f.line() for f in r.findings)
        self.assertEqual(r.ids("error"), {c for c in want if PLAN[c].severity == "error"}, listing)
        self.assertLessEqual({c for c in want if PLAN[c].severity == "warning"}, r.ids("warning"), listing)
        if want:
            primary = PLAN[want[0]]
            layers = {s for f in r.findings if f.check_id == primary.id for s in f.sources}
            if "Python" in primary.implemented_in:
                self.assertIn("python", layers, f"{primary.id} should be found by the Python layer")
            if "SQL query" in primary.implemented_in and "SQL constraint" not in primary.implemented_in:
                self.assertIn("sql", layers, f"{primary.id} should be found by an SQL query")
    return test


def _sql_layer_test(case, check_id):
    def test(self):
        with site(case) as root:
            res = load_sqlite.load(ad.Dataset(root), root / "build" / "atlas.db")
        self.assertIn(check_id, {v.check_id for v in res.violations},
                      [(v.check_id, v.table, v.record_id, v.constraint) for v in res.violations])
    return test


for _case in cases():
    _name = "test_" + re.sub(r"\W", "_", _case.name)
    setattr(FixtureTests, _name, _fixture_test(_case))
    _want = expected(_case)
    if _want and "SQL constraint" in PLAN[_want[0]].implemented_in:
        setattr(SqlLayerTests, _name, _sql_layer_test(_case, _want[0]))


class RunTests(unittest.TestCase):
    def test_base_is_clean(self):
        with site() as root:
            r = validate(root)
        self.assertEqual([f.line() for f in r.findings], [])
        self.assertEqual(r.skipped, [], "the base has a denylist, so EC-055 must run")

    def test_real_data_has_no_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = vd.validate(ROOT, Path(tmp))
        self.assertEqual([f.line() for f in r.errors], [])

    def test_exit_codes_and_line_format(self):
        with site(FIXTURES / "EC-012_latitude_out_of_range") as root:
            p = run_cli(root)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("[EC-012] error locations zurich lat=-132.8 Latitude out of range [-90, 90]", p.stdout)
        self.assertTrue(p.stdout.rstrip().splitlines()[-1].startswith("1 error, 0 warnings"), p.stdout)
        with site() as root:
            p = run_cli(root)
            reports = [(root / "build" / f).is_file() for f in ("validation_report.md", "validation_report.csv")]
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(reports, [True, True])


class PrivacyTests(unittest.TestCase):
    def outputs(self, root):
        p = run_cli(root)
        build = root / "build"
        return p, {"terminal": p.stdout + p.stderr,
                   "markdown": (build / "validation_report.md").read_text(encoding="utf-8"),
                   "csv": (build / "validation_report.csv").read_text(encoding="utf-8")}

    def test_reports_never_show_the_denylisted_term(self):
        with site(FIXTURES / "EC-055_denylisted_name") as root:
            p, out = self.outputs(root)
        self.assertEqual(p.returncode, 1)
        self.assertIn("[EC-055] error data/cities.js line 3 *** Privacy denylist term", out["terminal"])
        self.assertIn("[EC-055] error notes/***.txt path *** Privacy denylist term", out["terminal"])
        for name, text in out.items():
            self.assertIsNone(TERM.search(text), f"the {name} output contains the denylisted term")
            self.assertIn("***", text, name)

    def test_redaction_covers_every_check(self):
        # the term as a location id: other checks (EC-051 here) would print it as a record id
        with site() as root:
            js = root / "data" / "cities.js"
            js.write_text(js.read_text(encoding="utf-8").replace("];\n\nconst COUNTRIES",
                '  {id:"quentin-placeholder", en:"Q", country:"Germany", region:"Europe", lat:52.5, lng:13.4, size:"big"},\n'
                "];\n\nconst COUNTRIES"), encoding="utf-8")
            p, out = self.outputs(root)
        self.assertIn("[EC-051] warning locations *** photos=0 Location has no photos", out["terminal"])
        for name, text in out.items():
            self.assertIsNone(TERM.search(text), f"the {name} output contains the denylisted term")

    def test_matcher(self):
        deny = ad.Denylist(["Quentin Placeholder"], [])
        for s in ("Quentin Placeholder", "QUENTIN-placeholder.jpg", "Quéntin  Placeholder", "x/quentin_placeholder/y"):
            self.assertIsNone(TERM.search(deny.redact(s).replace("é", "e")), s)
        for s in ("Quentin Placeholders", "Quentin", "quentinplaceholder"):
            self.assertEqual(deny.redact(s), s)


class TraceabilityTests(unittest.TestCase):
    PY = [ROOT / "tools" / "validate_data.py", ROOT / "tools" / "atlas_data.py"]
    SCHEMA = ROOT / "db" / "schema.sql"
    QUERIES = sorted((ROOT / "db" / "checks").glob("*.sql"))

    @staticmethod
    def ids(*paths):
        return {i for p in paths for i in re.findall(r"EC-\d{3}", p.read_text(encoding="utf-8"))}

    def test_every_id_in_code_is_in_the_plan(self):
        for p in self.PY + [ROOT / "tools" / "load_sqlite.py", self.SCHEMA] + self.QUERIES:
            self.assertFalse(self.ids(p) - set(PLAN), f"{p.name} uses Check IDs that are not in the plan")

    def test_every_check_is_implemented_where_the_plan_says(self):
        py, schema = self.ids(*self.PY), self.ids(self.SCHEMA, ROOT / "tools" / "load_sqlite.py")
        queries = {p.name[:6] for p in self.QUERIES}
        for c in PLAN.values():
            with self.subTest(c.id):
                if "Python" in c.implemented_in:
                    self.assertIn(c.id, py)
                if "SQL constraint" in c.implemented_in:
                    self.assertIn(c.id, schema)
                self.assertEqual("SQL query" in c.implemented_in, c.id in queries)

    def test_every_check_has_a_fixture(self):
        covered = {c.name[:6] for c in cases() if c.name.startswith("EC-")}
        self.assertEqual(set(PLAN) - covered, set())
        for c in cases():
            if c.name.startswith("EC-"):
                self.assertEqual(expected(c)[0], c.name[:6], f"{c.name}: the first expected ID is the check under test")

    def test_sql_files_agree_with_the_plan(self):
        for p in self.QUERIES:
            with self.subTest(p.name):
                text = p.read_text(encoding="utf-8")
                m = re.match(r"-- (EC-\d{3}) · (.+?) · (error|warning)\n", text)
                self.assertTrue(m, "first line must be: -- EC-xxx · Category · severity")
                c = PLAN[m.group(1)]
                self.assertEqual((m.group(1), m.group(2), m.group(3)), (p.name[:6], c.category, c.severity))
                self.assertIn(f"'{c.id}'", text)
                self.assertIn(f"'{c.severity}'", text)

    def test_every_schema_constraint_has_a_check_id(self):
        text = self.SCHEMA.read_text(encoding="utf-8")
        names = re.findall(r'CONSTRAINT "([^"]+)" CHECK', text)
        self.assertEqual(len(names), len(re.findall(r"\bCHECK \(", text)), "every CHECK must be named")
        for n in names:
            self.assertRegex(n, r"^EC-\d{3}:\w+$")
        conn = sqlite3.connect(":memory:")
        conn.executescript(text)
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            fks = defaultdict(list)
            for fid, _, _, frm, *_ in conn.execute(f"PRAGMA foreign_key_list({table})"):
                fks[fid].append(frm)
            for cols in fks.values():
                self.assertIn((table, ",".join(cols)), load_sqlite.FOREIGN_KEY_CHECK)
            for _, index, unique, *_ in conn.execute(f"PRAGMA index_list({table})").fetchall():
                if unique:
                    cols = [r[2] for r in conn.execute(f"PRAGMA index_info({index})")]
                    self.assertIn((table, ",".join(cols)), load_sqlite.UNIQUE_CHECK)
            if any(r[3] for r in conn.execute(f"PRAGMA table_info({table})")):
                self.assertIn(table, load_sqlite.REQUIRED_CHECK)


def show(prefix):
    """Run the validator on one fixture (for demos): python3 tools/test_validation.py --show EC-012"""
    case = next((c for c in cases() if c.name.startswith(prefix)), None)
    if case is None:
        sys.exit(f"No fixture starts with {prefix}")
    note = [l[1:].strip() for l in (case / "expected.txt").read_text(encoding="utf-8").splitlines() if l.startswith("#")]
    print(f"Fixture {case.name}: {' '.join(note)}\nExpected: {', '.join(expected(case)) or 'no error'}\n")
    with site(case) as root:
        print(validate(root).terminal())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--show":
        show(sys.argv[2])
    else:
        unittest.main()
