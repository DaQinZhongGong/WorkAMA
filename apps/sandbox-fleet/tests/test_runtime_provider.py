import os
import tempfile
import unittest
from pathlib import Path

from workama_sandbox.main import firecracker_preflight


class RuntimeProviderTests(unittest.TestCase):
    def test_missing_firecracker_prerequisites_are_external_pending(self):
        with tempfile.TemporaryDirectory() as root:
            result = firecracker_preflight(
                str(Path(root) / "missing-firecracker"),
                str(Path(root) / "missing-sockets"),
                str(Path(root) / "missing-kernel"),
                str(Path(root) / "missing-rootfs"),
                str(Path(root) / "missing-kvm"),
            )
        self.assertEqual(result["status"], "pending_external")
        self.assertFalse(result["ready"])
        self.assertIn("binary", result["missing"])
        self.assertIn("kvm", result["missing"])

    def test_complete_firecracker_prerequisites_are_ready(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            sockets = base / "sockets"
            sockets.mkdir()
            binary = base / "firecracker"
            kernel = base / "vmlinux"
            rootfs = base / "rootfs.ext4"
            for path in (binary, kernel, rootfs):
                path.write_bytes(b"fixture")
            binary.chmod(binary.stat().st_mode | 0o111)
            result = firecracker_preflight(str(binary), str(sockets), str(kernel), str(rootfs), "/dev/null")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["ready"])
        self.assertEqual(result["missing"], [])


if __name__ == "__main__":
    unittest.main()
