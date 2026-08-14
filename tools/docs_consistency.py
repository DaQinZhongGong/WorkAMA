#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


EXPECTED = {
    "baseline_documents": 32, "functional_requirements": 61, "nonfunctional_requirements": 16,
    "pages": 77, "api_operations": 855, "ws_events": 24, "nats_subjects": 24,
    "webhook_events": 19, "postgres_tables": 176, "clickhouse_tables": 4,
    "state_machines": 17, "acceptance_scenarios": 144, "analytics_events": 43,
}
FORBIDDEN_BRANDS = ("WorkMA", "Work-AMA", "Work AMA")


@dataclass
class Finding:
    code: str
    message: str
    file: str | None = None
    line: int | None = None


def read_numbered(docs: Path, number: str) -> tuple[Path, str]:
    matches = sorted(docs.glob(f"{number}-*.md"))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {number}-*.md, found {len(matches)}")
    return matches[0], matches[0].read_text(encoding="utf-8")


def section(text: str, start: str, end: str | None = None) -> str:
    start_at = text.index(start)
    end_at = text.find(end, start_at + len(start)) if end else -1
    return text[start_at:end_at if end_at >= 0 else None]


def table_ids(text: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, text, flags=re.MULTILINE))


def collect_counts(docs: Path) -> dict[str, int]:
    _, index = read_numbered(docs, "000")
    _, requirements = read_numbered(docs, "100")
    _, pages = read_numbered(docs, "200")
    _, data = read_numbered(docs, "620")
    _, contracts = read_numbered(docs, "720")
    _, acceptance = read_numbered(docs, "830")
    _, analytics = read_numbered(docs, "840")

    baseline_documents = table_ids(section(index, "## 3.", "## 4."), r"^\|\s*(\d{3})\s*\|")
    fr = table_ids(requirements, r"^\|\s*(FR-[GPACX]-\d{2})\s*\|")
    nfr = table_ids(requirements, r"^\|\s*(NFR-\d{2})\s*\|")
    page_ids = table_ids(pages, r"^\|\s*((?:W-[A-Z]+|S|D|M|MP|E)-\d{2})\b")
    page_heading_counts = [int(value) for value in re.findall(
        r"^###\s+(?:3\.[123]|4\.[1234])\s+[^\n]*\((\d+)\s*页\)", pages, flags=re.MULTILINE
    )]
    if len(page_heading_counts) != 7 or not page_ids:
        raise ValueError("page registry headings or page IDs are incomplete")
    operations = table_ids(contracts, r"^\|\s*([a-zA-Z][a-zA-Z0-9]+)\s*\|\s*`(?:GET|POST|PUT|PATCH|DELETE)\s+")
    ws = table_ids(section(contracts, "## 8.", "## 9."), r"^\|\s*`([^`]+)`\s*\|")
    nats = table_ids(section(contracts, "## 10.", "## 11."), r"^\|\s*`([^`]+)`\s*\|")
    webhooks = table_ids(section(contracts, "## 11.", "## 12."), r"^\|\s*`([^`]+)`\s*\|")
    postgres = table_ids(section(data, "## 3.", "## 4."), r"^\|\s*`((?:id|gw|pf|ag|bill|sec|ops)_[a-z0-9_]+)`\s*\|")
    clickhouse = table_ids(section(data, "### 4.1", "### 4.2"), r"^\|\s*`([a-z0-9_]+)`\s*\|")
    state_machines = set(re.findall(r"^### 5\.(\d+)\s+", section(data, "## 5.", "## 6."), flags=re.MULTILINE))
    scenarios = table_ids(acceptance, r"^\|\s*(AC-[A-Z]+-\d{3})\s*\|")
    analytics_section = section(analytics, "## 7.", "## 8.")
    analytics_events: set[str] = set()
    for line in analytics_section.splitlines():
        if re.match(r"^\|\s*[^-|].*\|\s*\d+\s*\|", line) and "合计" not in line:
            cells = line.split("|")
            if len(cells) >= 4:
                analytics_events.update(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", cells[3]))
    return {
        "baseline_documents": len(baseline_documents), "functional_requirements": len(fr),
        "nonfunctional_requirements": len(nfr), "pages": sum(page_heading_counts),
        "api_operations": len(operations), "ws_events": len(ws), "nats_subjects": len(nats),
        "webhook_events": len(webhooks), "postgres_tables": len(postgres),
        "clickhouse_tables": len(clickhouse), "state_machines": len(state_machines),
        "acceptance_scenarios": len(scenarios), "analytics_events": len(analytics_events),
    }


def validate_links_and_brand(docs: Path) -> list[Finding]:
    findings: list[Finding] = []
    files = sorted(docs.glob("*.md"))
    numbers = {path.name[:3] for path in files if re.match(r"^\d{3}-", path.name)}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            for brand in FORBIDDEN_BRANDS:
                if brand.lower() in line.lower():
                    findings.append(Finding("brand.invalid", f"use the frozen brand WorkAMA, not {brand}", path.name, line_no))
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", line):
                clean = target.split("#", 1)[0]
                if not clean or re.match(r"^[a-z]+://", clean):
                    continue
                if not (path.parent / clean).resolve().exists():
                    findings.append(Finding("link.missing", f"relative link does not exist: {target}", path.name, line_no))
            for number in re.findall(r"《(?:\[[^\]]+\]\([^)]*\)|[^》]*?)(\d{3})(?:[^》]*)》", line):
                if number not in numbers:
                    findings.append(Finding("reference.missing", f"numbered document does not exist: {number}", path.name, line_no))
    return findings


def run(root: Path) -> dict:
    docs = root / "WorkAMA-Docs"
    findings = validate_links_and_brand(docs)
    try:
        counts = collect_counts(docs)
    except (ValueError, OSError) as exc:
        counts = {}
        findings.append(Finding("count.source", str(exc)))
    for key, expected in EXPECTED.items():
        actual = counts.get(key)
        if actual != expected:
            findings.append(Finding("count.mismatch", f"{key}: expected {expected}, found {actual}"))
    return {"ok": not findings, "counts": counts, "expected": EXPECTED, "findings": [asdict(item) for item in findings]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the frozen WorkAMA documentation baseline")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report = run(args.root.resolve())
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
