"""Prevent dependency installs from restoring a compromised CA trust bundle."""
from pathlib import Path
import unittest


class TLSDependencyFloorTests(unittest.TestCase):
    def test_certifi_security_floor(self):
        requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
        entries = {
            line.strip()
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("certifi>=2026.10.19", entries)


if __name__ == "__main__":
    unittest.main()
