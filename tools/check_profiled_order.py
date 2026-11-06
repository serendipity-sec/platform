"""Read-only checks over the complete registered order pipeline bundle."""

import argparse
from pathlib import Path

from flowdelta.artifact_io import write_new_bytes
from flowdelta.ingestion import json_bytes
from flowdelta.order_verification import verify_profiled_order

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = verify_profiled_order(args.run_root, Path(__file__).resolve().parents[1])
    write_new_bytes(args.out, json_bytes(report))
    print(json_bytes({"normal_checks": "PASS", "runs": len(report["observations"])}).decode())
