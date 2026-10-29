"""Stand-in for day-trader-skill's tests/_load.py, so its test_timemachine.py
runs here verbatim: load("trader-timemachine", "timemachine") loads the
vendored copy in src/tradingbot/vendor/."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
_VENDORED = {("trader-timemachine", "timemachine"): os.path.join(ROOT, "src", "tradingbot", "vendor", "timemachine.py")}


def load(folder, name):
    path = _VENDORED[(folder, name)]
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
