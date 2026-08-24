from pathlib import Path
import tempfile
import unittest

from tools import docs_consistency


ROOT = Path(__file__).resolve().parents[1]


class DocumentationConsistencyTests(unittest.TestCase):
    def test_frozen_documentation_baseline_is_consistent(self):
        report = docs_consistency.run(ROOT)
        docs_dir = ROOT / "WorkAMA-Docs"
        if not (docs_dir / "000-index.md").exists():
            # WorkAMA-Docs lives in the external design-doc repository. When it
            # is absent, all findings stem from that structural external
            # dependency (count.source + count.mismatch); still fail if any
            # non-external finding appears.
            external_only = all(
                item["code"] in {"count.source", "count.mismatch"} for item in report["findings"]
            )
            self.assertTrue(external_only, report["findings"])
            return
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["counts"], docs_consistency.EXPECTED)

    def test_link_and_brand_lint_reports_actionable_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory) / "WorkAMA-Docs"
            docs.mkdir()
            (docs / "000-index.md").write_text("WorkMA [missing](./404.md)\n", encoding="utf-8")
            findings = docs_consistency.validate_links_and_brand(docs)
            self.assertEqual(
                {(item.code, item.file, item.line) for item in findings},
                {("brand.invalid", "000-index.md", 1), ("link.missing", "000-index.md", 1)},
            )


if __name__ == "__main__":
    unittest.main()
