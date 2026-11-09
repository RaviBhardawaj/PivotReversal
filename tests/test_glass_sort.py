"""Watchlist order: entry on first, then strikes near their entry, then setups, rest, closed."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "glass_web", "glass_sort.js")

ROWS = [
    {"name": "far", "trade": False, "closed": False, "setup": False, "gap": 30.0},
    {"name": "closed", "trade": True, "closed": True, "setup": False, "gap": 2.0},
    {"name": "near_far", "trade": False, "closed": False, "setup": False, "gap": -4.5},
    {"name": "setup", "trade": False, "closed": False, "setup": True, "gap": 12.0},
    {"name": "live", "trade": True, "closed": False, "setup": False, "gap": 9.0},
    {"name": "near_close", "trade": False, "closed": False, "setup": True, "gap": 0.8},
    {"name": "no_ltp", "trade": False, "closed": False, "setup": False, "gap": None},
]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class EntryFirstOrder(unittest.TestCase):
    def test_order(self):
        code = (f"const s = require({json.dumps(JS)});"
                f"console.log(JSON.stringify(s.entryFirst({json.dumps(ROWS)}).map(r => r.name)));")
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), ["live", "near_close", "near_far", "setup", "far", "no_ltp", "closed"])


if __name__ == "__main__":
    unittest.main()
