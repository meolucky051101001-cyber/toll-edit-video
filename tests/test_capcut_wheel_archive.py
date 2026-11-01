import base64
import csv
import hashlib
import io
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from scripts import export_capcut_wheel as exporter


class WheelArchiveTests(unittest.TestCase):
    def fake_distribution(self, root, modified=False):
        contents = {
            "capcut_tts_api/__init__.py": b"VALUE = 1\n",
            "capcut_tts_api-1.0.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: capcut-tts-api\nVersion: 1.0.0\n",
            "capcut_tts_api-1.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        records = []
        for name, content in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content + (b"changed" if modified and name.endswith("__init__.py") else b""))
            class Entry:
                def as_posix(self):
                    return self.path
            entry = Entry()
            entry.path = name
            entry.size = len(content)
            entry.hash = SimpleNamespace(mode="sha256", value=base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode())
            records.append(entry)
        return SimpleNamespace(version="1.0.0", files=records,
                               read_text=lambda name: contents["capcut_tts_api-1.0.0.dist-info/" + name].decode(),
                               locate_file=lambda entry: root / entry.as_posix())

    def test_archive_has_correct_record_and_reproducible_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            distribution = self.fake_distribution(root / "installed")
            with patch.object(exporter.metadata, "distribution", return_value=distribution):
                path = exporter.export_capcut_wheel(root / "wheels")
                first = path.read_bytes()
                self.assertEqual(exporter.export_capcut_wheel(root / "wheels").read_bytes(), first)
            with zipfile.ZipFile(path) as archive:
                rows = csv.reader(io.StringIO(archive.read("capcut_tts_api-1.0.0.dist-info/RECORD").decode()))
                for name, digest, size in rows:
                    self.assertNotIn("..", PurePosixPath(name).parts)
                    if digest:
                        content = archive.read(name)
                        self.assertEqual(len(content), int(size))
                        actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                        self.assertEqual(digest, "sha256=" + actual)

    def test_modified_installed_file_is_rejected_before_creating_wheel(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            distribution = self.fake_distribution(root / "installed", modified=True)
            with patch.object(exporter.metadata, "distribution", return_value=distribution), self.assertRaises(ValueError):
                exporter.export_capcut_wheel(root / "wheels")
            self.assertFalse((root / "wheels").exists())
