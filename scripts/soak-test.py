#!/usr/bin/env python3
"""Repeat simulated fault tests. Uses only temporary state and a virtual serial companion."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cycles", type=int, default=3)
parser.add_argument("--output", type=Path, default=Path("soak-report.json"))
args = parser.parse_args()
if not 1 <= args.cycles <= 10000:
    parser.error("cycles must be between 1 and 10000")
root = Path(__file__).resolve().parents[1]
results = []
for cycle in range(args.cycles):
    started = time.time()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_hardware_simulation.py",
            "tests/test_delivery_memory.py",
            "tests/test_deployment.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    results.append(
        {
            "cycle": cycle + 1,
            "started": started,
            "seconds": time.time() - started,
            "passed": result.returncode == 0,
            "output": (result.stdout + result.stderr)[-12000:],
        }
    )
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump({"radio_hardware_used": False, "cycles": results}, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, args.output)
    print(f"Durchlauf {cycle + 1}/{args.cycles}: {'OK' if result.returncode == 0 else 'FEHLER'}", flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
