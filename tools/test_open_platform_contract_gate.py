from pathlib import Path
import json
import tempfile
import unittest

from tools import open_platform_contract_gate


ROOT = Path(__file__).resolve().parents[1]


class OpenPlatformContractGateTests(unittest.TestCase):
    def test_repository_open_platform_contract_is_complete(self):
        report = open_platform_contract_gate.audit(ROOT)
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["operation_count"], len(open_platform_contract_gate.OPENAPI_OPERATIONS))

    def test_evidence_gate_rejects_promoted_external_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = Path(directory)
            for filename in open_platform_contract_gate.EVIDENCE_FILES:
                (evidence_dir / filename).write_text(
                    json.dumps({
                        **{field: [] for field in open_platform_contract_gate.COMMON_EVIDENCE_FIELDS if field in {"verified_boundary", "pending_boundary"}},
                        "evidence_schema_version": 2,
                        "verification_scope": "local-compose",
                        "protocol_profile": "test",
                        "verification_target": "http://localhost:20200",
                        "staging_gate": "pending_external",
                        "public_protocol_verified": True,
                        "signature_mutual_trust_verified": False,
                    }),
                    encoding="utf-8",
                )
            report = open_platform_contract_gate.audit(ROOT, evidence_dir=evidence_dir, require_evidence=True)
            self.assertFalse(report["ok"])
            self.assertIn("evidence.external_boundary_promoted", {item["code"] for item in report["findings"]})


if __name__ == "__main__":
    unittest.main()
