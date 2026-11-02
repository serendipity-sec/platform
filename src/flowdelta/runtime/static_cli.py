"""저장소 루트에서 정적 plan을 실행한다 (#51).

    python -m flowdelta.runtime.static_cli --handoff <handoff-dir> --plan <plan.json> \
        --catalog <static-scan-catalog.json> --out var/<new-dir>

출력 폴더는 새 폴더여야 하며 `var/` 아래여야 한다. 원시 보고서에 대상 코드가 들어 있기 때문이다.
종료 코드: 0 모든 side 성공, 2 입력 오류 또는 실패, 130 중단.
"""

import argparse
import sys
from pathlib import Path

from flowdelta.ingestion import InputError, json_bytes, parse_file_model, parse_json
from flowdelta.pipeline_contracts import TestPlan
from flowdelta.pipeline_io import read_handoff
from flowdelta.runtime.static_adapter import run_static_plan
from flowdelta.runtime.static_scan import StaticScanCatalog


def _read(path: Path) -> object:
    try:
        return parse_json(path.read_bytes())
    except OSError as exc:
        raise InputError("STATIC_INPUT_UNREADABLE") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flowdelta.runtime.static_cli")
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        handoff = read_handoff(args.handoff)
        plan = parse_file_model(TestPlan, _read(args.plan))  # type: ignore[arg-type]
        catalog = parse_file_model(StaticScanCatalog, _read(args.catalog))  # type: ignore[arg-type]
        runs = run_static_plan(handoff, plan, catalog, args.handoff, Path.cwd(), args.out)
    except InputError as exc:
        sys.stderr.write(json_bytes(exc.diagnostic()).decode("utf-8"))
        return 2
    except KeyboardInterrupt:
        sys.stderr.write('{"status": "INTERRUPTED"}\n')
        return 130
    summary = {
        "schema_version": "0.4.0",
        "authorship": "RUNTIME_OBSERVATION",
        "plan_id": plan.plan_id,
        "comparison_run_id": runs[0].comparison_run_id,
        "basis": "STATIC_CONDITION",
        "security_outcome": None,
        "runs": [
            {
                "file": f"runs/{run.side}-1.static-run.json",
                "side": run.side,
                "status": run.status,
                "error_code": run.error_code,
                "scanned": len(run.scanned_node_ids),
                "unsupported": len(run.unsupported),
                "findings": [item.node_id for item in run.findings],
            }
            for run in runs
        ],
    }
    sys.stdout.write(json_bytes(summary).decode("utf-8"))
    return 0 if all(run.status == "SUCCEEDED" for run in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
