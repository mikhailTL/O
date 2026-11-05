#!/usr/bin/env python3
"""Turn OSWorld result directories into a markdown table (success rate per domain).

Layout (OSWorld ``run.py`` / ``run_o.py``):
    <result_dir>/<action_space>/<observation_type>/<model>/<domain>/<example_id>/result.txt

``result.txt`` holds one number (OSWorld's ``env.evaluate()`` score, 0..1). Examples without
a ``result.txt`` are counted as *unfinished*, never as failures. The table reports what the
files say; nothing is extrapolated.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path


def collect(result_dir: Path, action_space: str = "pyautogui", observation_type: str | None = None,
            model: str | None = None) -> dict[str, dict]:
    """``{domain: {"scores": [...], "unfinished": n}}`` for every example dir under the filters."""
    per_domain: dict[str, dict] = defaultdict(lambda: {"scores": [], "unfinished": 0})
    base = result_dir / action_space
    if not base.is_dir():
        return {}
    for obs_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if observation_type and obs_dir.name != observation_type:
            continue
        for model_dir in sorted(p for p in obs_dir.iterdir() if p.is_dir()):
            if model and model_dir.name != model:
                continue
            for domain_dir in sorted(p for p in model_dir.iterdir() if p.is_dir()):
                for example_dir in sorted(p for p in domain_dir.iterdir() if p.is_dir()):
                    result = example_dir / "result.txt"
                    if not result.is_file():
                        per_domain[domain_dir.name]["unfinished"] += 1
                        continue
                    try:
                        score = float(result.read_text(encoding="utf-8").strip())
                    except ValueError:
                        per_domain[domain_dir.name]["unfinished"] += 1
                        continue
                    per_domain[domain_dir.name]["scores"].append(score)
    return dict(per_domain)


def markdown(per_domain: dict[str, dict]) -> str:
    rows = ["| domain | finished | success | success rate | unfinished |", "|---|---:|---:|---:|---:|"]
    total_done = total_ok = total_unfinished = 0
    for domain in sorted(per_domain):
        scores = per_domain[domain]["scores"]
        done = len(scores)
        ok = sum(1 for s in scores if s >= 1.0)
        unfinished = per_domain[domain]["unfinished"]
        rate = f"{100.0 * ok / done:.1f}%" if done else "n/a"
        rows.append(f"| {domain} | {done} | {ok} | {rate} | {unfinished} |")
        total_done += done
        total_ok += ok
        total_unfinished += unfinished
    rate = f"{100.0 * total_ok / total_done:.1f}%" if total_done else "n/a"
    rows.append(f"| **all** | {total_done} | {total_ok} | {rate} | {total_unfinished} |")
    return "\n".join(rows)


def offline_section(table_path: Path) -> str:
    """The table ``offline.py --out`` wrote, included verbatim under its own heading."""
    text = table_path.read_text(encoding="utf-8").strip()
    if text.startswith("# "):
        text = text.split("\n", 1)[1].lstrip("\n") if "\n" in text else ""  # this section supplies the title
    return "## Offline grounding/judge regression\n\n" + text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Markdown success-rate table from OSWorld result dirs.")
    p.add_argument("--result_dir", default="./results")
    p.add_argument("--action_space", default="pyautogui")
    p.add_argument("--observation_type", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--offline", default=None, help="markdown table written by offline.py --out, appended verbatim")
    args = p.parse_args(argv)
    per_domain = collect(Path(args.result_dir), args.action_space, args.observation_type, args.model)
    offline_path = Path(args.offline) if args.offline else None
    if offline_path is not None and not offline_path.is_file():
        print(f"no offline table at {offline_path}", file=sys.stderr)
        return 1
    if not per_domain and offline_path is None:
        print(f"no results under {args.result_dir}/{args.action_space}", file=sys.stderr)
        return 1
    if offline_path is None:
        print(markdown(per_domain))
        return 0
    sections: list[str] = []
    if per_domain:
        sections.append("## OSWorld results\n\n" + markdown(per_domain))
    else:
        print(f"no results under {args.result_dir}/{args.action_space}; offline table only", file=sys.stderr)
    sections.append(offline_section(offline_path))
    print("\n\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
