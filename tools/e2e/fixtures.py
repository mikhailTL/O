#!/usr/bin/env python3
"""Fixtures and outcome checks for tools/e2e/real-run.js.

  python3 fixtures.py make <workspace> <manifest.json>     photos/ receipts/ notes/ webapp-less workspace
  python3 fixtures.py photos <workspace> <manifest.json>   only photos/
  python3 fixtures.py receipts <workspace> <manifest.json> only receipts/
  python3 fixtures.py notes <workspace>                    only notes/
  python3 fixtures.py check-photos <workspace> <manifest.json>
  python3 fixtures.py check-xlsx <xlsx> <manifest.json>
Every check prints one JSON object {ok, problems:[...], ...}.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

CATS = ["Groceries", "Dining", "Transport", "Office"]


# ------------------------------------------------------------------ fixtures
def make_photos(ws: Path, manifest: dict) -> None:
    from PIL import Image

    rnd = random.Random(7)
    d = ws / "photos"
    d.mkdir(parents=True, exist_ok=True)
    start = datetime(2024, 1, 3, 9, 0, 0)
    days = sorted(rnd.sample(range(0, 150), 18))
    shots = []
    for i in range(30):
        day = days[i % len(days)] if i < 18 else rnd.choice(days[:8])  # 12 extra shots on days that already have one
        shots.append(start + timedelta(days=day, hours=rnd.randint(0, 10), minutes=rnd.randint(0, 59), seconds=rnd.randint(0, 59)))
    rnd.shuffle(shots)
    out = {}
    for i, ts in enumerate(shots):
        prefix = rnd.choice(["IMG_", "DSC_", "PXL_"])
        ext = ".JPG" if i % 10 == 3 else ".jpg"
        name = f"{prefix}{1000 + i * 7}{ext}"
        img = Image.new("RGB", (160, 120), (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
        exif = Image.Exif()
        exif[0x010F] = "FixtureCam"
        exif[0x0132] = (ts + timedelta(days=400)).strftime("%Y:%m:%d %H:%M:%S")  # DateTime (file change): a decoy
        exif.get_ifd(0x8769)[0x9003] = ts.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
        p = d / name
        img.save(p, "JPEG", exif=exif.tobytes())
        os.utime(p, (1700000000 + i, 1700000000 + i))  # mtimes say nothing about the shot date
        out[name] = ts.strftime("%Y-%m-%d %H:%M:%S")
    manifest["photos"] = out


def make_receipts(ws: Path, manifest: dict) -> None:
    rnd = random.Random(11)
    d = ws / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    totals = {c: 0.0 for c in CATS}
    vendors = {"Groceries": ["Publix", "Whole Foods", "Trader Joe's"], "Dining": ["Shake Shack", "Chipotle", "Blue Bottle Coffee"],
               "Transport": ["Uber", "Shell", "SunPass"], "Office": ["Staples", "Office Depot", "Best Buy"]}
    for i in range(7):
        cat = CATS[i % 4]
        vendor = rnd.choice(vendors[cat])
        items = [(f"Item {k + 1}", round(rnd.uniform(2, 40), 2)) for k in range(rnd.randint(1, 4))]
        sub = round(sum(p for _, p in items), 2)
        tax = round(sub * 0.07, 2)
        total = round(sub + tax, 2)
        totals[cat] += total
        lines = [vendor.upper(), f"Date: 2024-0{rnd.randint(1, 9)}-{rnd.randint(10, 28)}", f"Category: {cat}", "-" * 24]
        lines += [f"{n:<16}{p:>8.2f}" for n, p in items]
        lines += ["-" * 24, f"{'Subtotal':<16}{sub:>8.2f}", f"{'Tax':<16}{tax:>8.2f}", f"{'TOTAL':<16}{total:>8.2f}", "Thank you!"]
        (d / f"receipt_{i + 1:02d}.txt").write_text("\n".join(lines) + "\n")
    for j in range(2):
        rows = ["date,vendor,category,amount"]
        for k in range(5):
            cat = rnd.choice(CATS)
            amt = round(rnd.uniform(3, 120), 2)
            totals[cat] += amt
            rows.append(f"2024-0{rnd.randint(1, 9)}-{rnd.randint(10, 28)},{rnd.choice(vendors[cat])},{cat},{amt:.2f}")
        (d / f"card_export_{j + 1}.csv").write_text("\n".join(rows) + "\n")
    manifest["receipts"] = {c: round(v, 2) for c, v in totals.items()}
    manifest["receipts_grand"] = round(sum(totals.values()), 2)


def make_notes(ws: Path) -> None:
    d = ws / "notes"
    d.mkdir(parents=True, exist_ok=True)
    (d / "standup-2024-11-04.md").write_text("# Standup Nov 4\n- Shipped the onboarding flow\n- Blocked on the payment sandbox keys\n- Next: write tests for the sync worker\n")
    (d / "ideas.txt").write_text("App ideas\n1. A pantry tracker that reads grocery receipts\n2. A study timer that blocks distracting sites\n3. Voice notes that turn into todo lists\n")
    (d / "reading-list.md").write_text("# Reading\n- Designing Data-Intensive Applications (ch. 5 replication)\n- The Mom Test\n- Paul Graham: Do Things That Don't Scale\n")


# ------------------------------------------------------------------ checks
def _exif_date(p: Path) -> str | None:
    from PIL import Image

    try:
        with Image.open(p) as im:
            v = im.getexif().get_ifd(0x8769).get(0x9003)
        return datetime.strptime(v, "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d") if v else None
    except Exception:  # noqa: BLE001
        return None


def check_photos(ws: Path, manifest: dict) -> dict:
    root = ws / "photos"
    problems: list[str] = []
    want = sorted(v[:10] for v in manifest["photos"].values())
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg")]
    got = []
    per_day: dict[str, list[int]] = {}
    folders = set()
    for p in files:
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_(\d+)\.jpg", p.name)
        date = _exif_date(p)
        if not m:
            problems.append(f"bad name: {p.relative_to(root)}")
            continue
        if date != m.group(1):
            problems.append(f"name date {m.group(1)} != EXIF {date}: {p.relative_to(root)}")
        if p.parent == root:
            problems.append(f"not in a month folder: {p.name}")
        else:
            rel_dir = str(p.parent.relative_to(root))
            folders.add(rel_dir)
            ym = m.group(1)[:7]
            month_name = datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%B").lower()
            if ym not in rel_dir and ym.replace("-", "/") not in rel_dir and not (m.group(1)[:4] in rel_dir and month_name[:3] in rel_dir.lower()):
                problems.append(f"folder {rel_dir} does not name month {ym}: {p.name}")
        got.append(m.group(1))
        per_day.setdefault(m.group(1), []).append(int(m.group(2)))
    if sorted(got) != want:
        problems.append(f"dates on disk differ from the shots: {len(got)} files vs {len(want)} shots")
    for day, ns in per_day.items():
        ns = sorted(ns)
        if ns not in (list(range(1, len(ns) + 1)), list(range(0, len(ns)))):
            problems.append(f"{day}: sequence numbers {ns} are not consecutive")
    leftovers = [p.name for p in root.iterdir() if p.is_file()]
    if leftovers:
        problems.append(f"files left at photos/ top level: {leftovers[:5]}")
    return {"ok": not problems, "problems": problems[:20], "files": len(files), "folders": sorted(folders)}


_REF = r"(?:'?([^'!]+)'?!)?\$?([A-Z]{1,3})\$?(\d+)"


class _Eval:
    """Just enough Excel to read back what an agent wrote with openpyxl: refs, ranges, SUM, SUMIF(S), + - * /."""

    def __init__(self, wb):
        self.wb = wb

    def cell(self, ws, ref: str):
        v = ws[ref].value
        if isinstance(v, str) and v.startswith("="):
            return self.formula(ws, v[1:])
        return v

    def rng(self, ws, a: str, b: str):
        from openpyxl.utils import column_index_from_string, get_column_letter

        ma = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d*)", a)
        mb = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d*)", b)
        c1, c2 = column_index_from_string(ma.group(1)), column_index_from_string(mb.group(1))
        r1 = int(ma.group(2) or 1)
        r2 = int(mb.group(2) or ws.max_row)
        return [[self.cell(ws, f"{get_column_letter(c)}{r}") for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)]

    def formula(self, ws, f: str):
        env_ranges: list = []

        def sheet(name):
            return self.wb[name] if name else ws

        def sub_range(m):
            env_ranges.append(self.rng(sheet(m.group(1)), m.group(2), m.group(3)))
            return f"_R[{len(env_ranges) - 1}]"

        expr = re.sub(r"(?:'?([^'!(),]+)'?!)?(\$?[A-Z]{1,3}\$?\d*):(\$?[A-Z]{1,3}\$?\d*)", sub_range, f)
        expr = re.sub(_REF, lambda m: f"_C({m.group(1)!r},{m.group(2) + m.group(3)!r})", expr)
        expr = re.sub(r"\bSUMIFS\(", "_SUMIFS(", expr, flags=re.I)
        expr = re.sub(r"\bSUMIF\(", "_SUMIF(", expr, flags=re.I)
        expr = re.sub(r"\bSUM\(", "_SUM(", expr, flags=re.I)
        expr = re.sub(r"\bROUND\(", "round(", expr, flags=re.I)
        flat = lambda r: [x for row in r for x in row] if isinstance(r, list) else [r]  # noqa: E731
        num = lambda x: float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0  # noqa: E731

        def match(v, crit):
            return str(v).strip().lower() == str(crit).strip().lower()

        def SUMIF(r, crit, s=None):
            s = r if s is None else s
            return sum(num(b) for a, b in zip(flat(r), flat(s)) if match(a, crit))

        def SUMIFS(s, *pairs):
            vals = flat(s)
            keep = [True] * len(vals)
            for i in range(0, len(pairs), 2):
                for k, a in enumerate(flat(pairs[i])):
                    if k < len(keep) and not match(a, pairs[i + 1]):
                        keep[k] = False
            return sum(num(v) for v, k in zip(vals, keep) if k)

        scope = {"_R": env_ranges, "_C": lambda sh, ref: self.cell(sheet(sh), ref),
                 "_SUM": lambda *a: sum(num(x) for r in a for x in flat(r)), "_SUMIF": SUMIF, "_SUMIFS": SUMIFS}
        try:
            return eval(expr, {"__builtins__": {"round": round}}, scope)  # noqa: S307 - our own fixture file
        except Exception:  # noqa: BLE001
            return None


def check_xlsx(path: Path, manifest: dict) -> dict:
    import openpyxl

    problems: list[str] = []
    if not path.exists():
        return {"ok": False, "problems": [f"{path} does not exist"]}
    wb = openpyxl.load_workbook(path)
    ev = _Eval(wb)
    want = manifest["receipts"]
    grand = manifest["receipts_grand"]
    best = None
    for ws in wb.worksheets:
        found: dict[str, tuple[int, float]] = {}
        total_rows: list[tuple[int, float]] = []
        for row in ws.iter_rows():
            labels = [c for c in row if isinstance(c.value, str) and not c.value.startswith("=")]
            nums = []
            for c in row:
                v = ev.cell(ws, c.coordinate)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    nums.append(float(v))
            if not labels or not nums:
                continue
            text = " ".join(str(c.value) for c in labels).strip()
            for cat in want:
                if text.lower() == cat.lower() or re.fullmatch(rf"(total\s*[:\-]?\s*)?{cat}(\s+total)?", text, re.I):
                    if cat not in found:
                        found[cat] = (row[0].row, nums[-1])
            if re.search(r"\btotal\b", text, re.I) and not any(c.lower() in text.lower() for c in want):
                total_rows.append((row[0].row, nums[-1]))
        score = sum(1 for c, (_, v) in found.items() if abs(v - want[c]) < 0.011)
        if best is None or score > best[0]:
            best = (score, ws.title, found, total_rows)
    score, title, found, total_rows = best
    for cat, v in want.items():
        if cat not in found:
            problems.append(f"no total row for {cat} (want {v})")
        elif abs(found[cat][1] - v) >= 0.011:
            problems.append(f"{cat}: {found[cat][1]} != {v}")
    cat_rows = [r for r, _ in found.values()]
    grand_row = next((r for r, v in total_rows if abs(v - grand) < 0.011), None)
    position = None
    if grand_row is not None and cat_rows:
        position = "top" if grand_row < min(cat_rows) else "bottom" if grand_row > max(cat_rows) else "middle"
    return {"ok": not problems, "problems": problems, "sheet": title, "found": {k: v[1] for k, v in found.items()},
            "want": want, "grand": grand, "grand_row": grand_row, "category_rows": sorted(cat_rows), "total_position": position,
            "sheets": wb.sheetnames}


def main(argv: list[str]) -> int:
    cmd, ws = argv[0], Path(argv[1])
    man_path = Path(argv[2]) if len(argv) > 2 else None
    manifest = json.loads(man_path.read_text()) if man_path and man_path.exists() else {}
    if cmd in ("make", "photos", "receipts", "notes"):
        ws.mkdir(parents=True, exist_ok=True)
        if cmd in ("make", "photos"):
            make_photos(ws, manifest)
        if cmd in ("make", "receipts"):
            make_receipts(ws, manifest)
        if cmd in ("make", "notes"):
            make_notes(ws)
        if man_path:
            man_path.write_text(json.dumps(manifest, indent=2))
        print(json.dumps({"ok": True, "workspace": str(ws)}))
        return 0
    if cmd == "check-photos":
        print(json.dumps(check_photos(ws, manifest)))
        return 0
    if cmd == "check-xlsx":
        print(json.dumps(check_xlsx(ws, manifest)))
        return 0
    print(json.dumps({"ok": False, "problems": [f"unknown command {cmd}"]}))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
