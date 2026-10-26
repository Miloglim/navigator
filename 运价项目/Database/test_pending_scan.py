import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from ops.pending_scan import _sails


class PendingScanTests(unittest.TestCase):
    def test_surcharge_effective_dates_are_not_sailing_lines(self):
        self.assertEqual(_sails("7.25\u540ePCS"), set())
        self.assertEqual(_sails("10.19\u8d77SCS"), set())
        self.assertEqual(_sails("11.3 USD2950"), set())

    def test_explicit_date_and_service_still_matches(self):
        self.assertEqual(_sails("SHEKOU 10/30 FL2"), {"10/30 FL2"})


if __name__ == "__main__":
    unittest.main()
