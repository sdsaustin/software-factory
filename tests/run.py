#!/usr/bin/env python3
"""Regression suite for the software-factory plugin. Standard library only.

  python3 tests/run.py               deterministic scenarios (no network, no Claude login)
  python3 tests/run.py --live        also the live scenarios, which run `claude -p` on your normal login
  python3 tests/run.py -k server     only scenarios whose name contains "server"
  python3 tests/run.py --list        list scenarios

Each scenario runs in its own temp directory with isolated git config. A failing scenario's
directory is kept and its path printed; passing ones are removed unless --keep is given.
"""
import argparse
import importlib.util
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from helpers import Fixture, Skip  # noqa: E402


def collect(dirs):
    found = []
    for d in dirs:
        for path in sorted((HERE / d).glob("test_*.py")):
            spec = importlib.util.spec_from_file_location(f"{d}.{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name, fn in vars(mod).items():
                if name.startswith("test_") and callable(fn):
                    found.append((f"{d}/{path.stem[5:]}::{name[5:]}", fn))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="also run the live claude -p scenarios (slow, uses your subscription)")
    ap.add_argument("-k", dest="pattern", help="run only scenarios whose name contains this text")
    ap.add_argument("--keep", action="store_true", help="keep every scenario's temp directory")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--permission-mode", default="auto",
                    help="permission mode for live claude runs (default: auto)")
    args = ap.parse_args()

    scenarios = collect(["scenarios"] + (["live"] if args.live else []))
    if args.pattern:
        scenarios = [s for s in scenarios if args.pattern in s[0]]
    if args.list:
        print("\n".join(name for name, _ in scenarios))
        return 0

    options = {"permission_mode": args.permission_mode}
    failed, skipped, passed = [], [], 0
    for name, fn in scenarios:
        fx = Fixture(name, options)
        start = time.time()
        try:
            fn(fx)
        except Skip as e:
            skipped.append(name)
            print(f"SKIP {name}: {e}")
            fx.cleanup()
            continue
        except Exception:
            failed.append(name)
            print(f"FAIL {name} ({time.time() - start:.1f}s)")
            print("  " + traceback.format_exc().rstrip().replace("\n", "\n  "))
            print(f"  fixture kept: {fx.root}")
            continue
        passed += 1
        print(f"ok   {name} ({time.time() - start:.1f}s)")
        if args.keep:
            print(f"  fixture kept: {fx.root}")
        else:
            fx.cleanup()

    print(f"\n{passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
