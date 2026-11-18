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


def run_js(body):
    code = f"const s = require({json.dumps(JS)});{body}"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


LADDER = [
    {"name": "24600PE", "strike": 24600, "type": "PE", "buy": True},
    {"name": "24400CE", "strike": 24400, "type": "CE", "buy": True},
    {"name": "24500PE", "strike": 24500, "type": "PE", "buy": True},
    {"name": "24500CE-sell", "strike": 24500, "type": "CE", "buy": False},
    {"name": "24500CE", "strike": 24500, "type": "CE", "buy": True},
]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StrikeLadder(unittest.TestCase):
    """Rows keep a fixed strike order whatever the price does; only the labels and spot line move."""

    def test_fixed_order(self):
        out = run_js(f"console.log(JSON.stringify(s.ladder({json.dumps(LADDER)}).map(r => r.name)));")
        self.assertEqual(out, ["24400CE", "24500CE", "24500CE-sell", "24500PE", "24600PE"])
        # input order (e.g. a reshuffled server list) does not change the result
        rev = list(reversed(LADDER))
        self.assertEqual(run_js(f"console.log(JSON.stringify(s.ladder({json.dumps(rev)}).map(r => r.name)));"), out)

    def test_moneyness_and_spot_line(self):
        out = run_js("console.log(JSON.stringify(["
                     "s.strikeStep([24400, 24500, 24600, 24500]),"
                     "s.moneyness('CE', 24500, 24512, 100).label,"
                     "s.moneyness('CE', 24300, 24512, 100).label,"
                     "s.moneyness('PE', 24300, 24512, 100).label,"
                     "s.moneyness('PE', 24700, 24512, 100).label,"
                     f"s.spotSlot(s.ladder({json.dumps(LADDER)}), 24512),"
                     f"s.spotSlot(s.ladder({json.dumps(LADDER)}), 24390),"
                     f"s.spotSlot(s.ladder({json.dumps(LADDER)}), 24700)]));")
        self.assertEqual(out, [100, "ATM", "2 ITM", "2 OTM", "2 ITM", 4, 0, 5])

    def test_new_and_gone_rows(self):
        out = run_js(
            "const t = s.makeTracker({newMs: 60, goneMs: 120}); const r = [];"
            "const st = (keys, now, ctx) => { const x = t.step(keys.map(k => ({key: k})), now, ctx);"
            "  r.push([[...x.fresh].sort(), x.ghosts.map(g => g.key).sort()]); };"
            "st(['a','b'], 0, 'c1');"      # first view: nothing is NEW
            "st(['a','b','c'], 10, 'c1');"  # c added -> NEW
            "st(['a','c'], 20, 'c1');"      # b left -> ghost
            "st(['a','c'], 100, 'c1');"     # c no longer NEW, b still a ghost
            "st(['a','c'], 200, 'c1');"     # b gone for good
            "st(['x'], 210, 'c2');"         # filter / symbol change: start over, no ghosts
            "t.forget(['x']); st([], 220, 'c2');"
            "console.log(JSON.stringify(r));")
        self.assertEqual(out, [[[], []], [["c"], []], [["c"], ["b"]], [[], ["b"]], [[], []], [[], []], [[], []]])


if __name__ == "__main__":
    unittest.main()
