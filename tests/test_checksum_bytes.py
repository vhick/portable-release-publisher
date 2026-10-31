"""Regression tests for v2.1.1: Windows CRLF must not modify SHA256SUMS.txt."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PUBLISHER = Path(__file__).resolve().parents[1] / "scripts" / "publish.py"
spec = importlib.util.spec_from_file_location("publisher_checksum_under_test", PUBLISHER)
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)


class ChecksumBytesTests(unittest.TestCase):
    def test_publisher_uses_binary_checksum_write(self):
        source = PUBLISHER.read_text(encoding="utf-8")
        self.assertIn('checksum_path.write_bytes(sums.encode("utf-8"))', source)
        self.assertIn('found.read_bytes() == sums.encode("utf-8")', source)

    def test_checksum_manifest_bytes_are_lf_only(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "Native.zip"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("test.txt", "valid native package")
            sums = pub.checksums([p])
            target = Path(folder) / "SHA256SUMS.txt"
            target.write_bytes(sums.encode("utf-8"))
            self.assertEqual(target.read_bytes(), sums.encode("utf-8"))
            self.assertNotIn(b"\r\n", target.read_bytes())
            self.assertEqual(target.stat().st_size, len(sums.encode("utf-8")))

    def test_windows_text_translation_would_break_length_and_binary_match(self):
        sums = "a" * 64 + "  App.paf.exe\n" + "b" * 64 + "  App.zip\n"
        expected = sums.encode("utf-8")
        windows_text_mode = sums.replace("\n", "\r\n").encode("utf-8")
        self.assertNotEqual(expected, windows_text_mode)
        self.assertEqual(len(windows_text_mode) - len(expected), 2)

    def test_release_checksum_comparison_is_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            (d / "A.zip").write_bytes(b"fake for remote-check path only")
            sums = "checksum-line\n"
            (d / "SHA256SUMS.txt").write_bytes(sums.encode("utf-8"))
            record = {"assets": [{"name": "A.zip", "size": (d / "A.zip").stat().st_size},
                                 {"name": "SHA256SUMS.txt", "size": len(sums.encode("utf-8"))}]}
            # Replace TemporaryDirectory in publish.py with deterministic fixture
            class FakeTmp:
                def __init__(self, **kwargs): pass
                def __enter__(self): return str(d)
                def __exit__(self, *args): return None
            with patch.object(pub.tempfile, "TemporaryDirectory", FakeTmp), patch.object(pub, "run_gh"):
                self.assertTrue(pub.remote_assets_match(record, [d/"A.zip"], sums, "portable-v1", "vhick/repo"))
                (d / "SHA256SUMS.txt").write_bytes(sums.replace("\n", "\r\n").encode("utf-8"))
                self.assertFalse(pub.remote_assets_match(record, [d/"A.zip"], sums, "portable-v1", "vhick/repo"))


if __name__ == "__main__":
    unittest.main()
