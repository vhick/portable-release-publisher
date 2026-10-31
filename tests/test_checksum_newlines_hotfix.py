"""Portable publisher Windows checksum regression checks (no network)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
spec = importlib.util.spec_from_file_location("portable_publisher_hotfix", MODULE_PATH)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ChecksumRegressionTests(unittest.TestCase):
    def test_windows_old_text_mode_adds_byte_to_checksum(self):
        """Replicate the byte-count mismatch which happened on Windows."""
        sums = "a" * 64 + "  Bitwarden-PortableApps.zip\n"
        expected = len(sums.encode("utf-8"))
        windows_old = sums.replace("\n", "\r\n").encode("utf-8")
        self.assertEqual(len(windows_old), expected + 1)

    def test_publisher_uses_binary_checksum_write(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('checksum_path.write_bytes(sums.encode("utf-8"))', source)
        self.assertNotIn('checksum_path.write_text(sums, encoding="utf-8")', source)

    def test_complete_fake_release_on_windows_style_job(self):
        """Mock gh CLI; assert draft verification works with exactly-sized LF bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dist").mkdir()
            sample = root / "dist" / "Bitwarden-PortableApps-test.zip"
            with zipfile.ZipFile(sample, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("BitwardenPortable/README.txt", "example")
            state = {"created": False, "published": False}

            def fake_gh(*args, check=True):
                args = list(args)
                if args[:2] == ["release", "view"]:
                    if not state["created"]:
                        return subprocess.CompletedProcess(args, 1, stdout="", stderr="release not found")
                    record = {
                        "isDraft": not state["published"],
                        "body": publisher.MARKER,
                        "tagName": "portable-test-r2",
                        "assets": [
                            {"name": sample.name, "size": sample.stat().st_size},
                            {"name": "SHA256SUMS.txt", "size": len(state["sums"])},
                        ]
                    }
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps(record), stderr="")
                if args[:2] == ["release", "create"]:
                    files = [Path(x) for x in args[3:5]]
                    state["sums"] = next(x for x in files if x.name == "SHA256SUMS.txt").read_bytes()
                    self.assertEqual(state["sums"], publisher.checksums([sample]).encode("utf-8"))
                    self.assertNotIn(b"\r\n", state["sums"])
                    state["created"] = True
                    return subprocess.CompletedProcess(args, 0, stdout="created", stderr="")
                if args[:2] == ["release", "download"]:
                    output = Path(args[args.index("--dir")+1]) / "SHA256SUMS.txt"
                    output.write_bytes(state["sums"])
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if args[:2] == ["release", "edit"]:
                    state["published"] = True
                    return subprocess.CompletedProcess(args, 0, stdout="edited", stderr="")
                raise AssertionError(f"Unexpected gh invocation: {args}")

            config = {
                "PUBLISH_FILES": "dist/Bitwarden-PortableApps-test.zip",
                "PUBLISH_TAG": "portable-test-r2",
                "PUBLISH_PREFIX": "portable-",
                "PUBLISH_PRUNE": "false",
                "PUBLISH_DRY_RUN": "false",
                "WORKSPACE_ROOT": str(root),
                "TARGET_REPO": "example/bitwarden-builder",
                "ACTION_EVENT": "workflow_dispatch",
                "GH_TOKEN": "test-only-fake-token",
                "SOURCE_SHA": "0"*40,
                "ACTION_RUN_ID": "12345",
            }
            with patch.dict(os.environ, config), patch.object(publisher, "run_gh", fake_gh), \
                    patch.object(publisher.shutil, "which", return_value="gh"):
                self.assertEqual(publisher.main(), 0)
            self.assertTrue(state["published"])

    def test_mismatch_reports_expected_and_actual_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp)/"a.zip"
            f.write_bytes(b"zip_placeholder")
            sums = "a" * 64 + "  a.zip\n"
            wrong = [{"name": "a.zip", "size": f.stat().st_size},
                     {"name": "SHA256SUMS.txt", "size": len(sums.encode())+1}]
            with patch.object(publisher, "run_gh", side_effect=AssertionError("should not download")):
                self.assertFalse(publisher.remote_assets_match({"assets": wrong}, [f], sums, "tag", "user/repo"))

if __name__ == "__main__":
    unittest.main()
