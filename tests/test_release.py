import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from scripts.prepare_release import digest, distributions, formula


class ReleaseTests(unittest.TestCase):
    def test_checks_versions_and_distribution_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "port-0.3.0-py3-none-any.whl"
            sdist = root / "port-0.3.0.tar.gz"
            metadata = b"Name: port\nVersion: 0.3.0\n"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("port-0.3.0.dist-info/METADATA", metadata)
            with tarfile.open(sdist, "w:gz") as archive:
                item = tarfile.TarInfo("port-0.3.0/PKG-INFO")
                item.size = len(metadata)
                archive.addfile(item, io.BytesIO(metadata))
            self.assertEqual(len(distributions(root, "v0.3.0")), 2)
            with self.assertRaises(ValueError):
                distributions(root, "v0.2.2")
            with self.assertRaises(ValueError):
                distributions(root, "main")
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("port-0.3.0.dist-info/METADATA", b"Name: wrong-project\nVersion: 0.3.0\n")
            with self.assertRaises(ValueError):
                distributions(root, "v0.3.0")

    def test_formula_pins_sources_resources_and_system_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            sdist = Path(directory) / "port-0.3.0.tar.gz"
            sdist.write_bytes(b"test release")
            resources = json.loads((Path(__file__).resolve().parents[1] / "scripts/homebrew-resources.json").read_text())
            result = formula(sdist, "v0.3.0", resources)
            self.assertIn(digest(sdist), result)
            self.assertIn('depends_on "rsync"', result)
            self.assertIn('depends_on "python@3.14"', result)
            self.assertIn("build_isolation: false", result)
            for resource in resources:
                self.assertIn(resource["sha256"], result)
                self.assertEqual(len(resource["sha256"]), 64)
                self.assertTrue(resource["url"].endswith("none-any.whl"))


if __name__ == "__main__":
    unittest.main()
