"""Create inspectable aggregate metrics from paper-guided dynamic run evidence."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


BASE = Path(__file__).resolve().parent


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default="paper-v2-qwen4b")
    args = parser.parse_args()
    batch = BASE / "results" / args.batch
    runs = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
    grouped = defaultdict(list)
    for run in runs:
        grouped[(run["case_id"], run["policy"])].append(run)

    groups = []
    for (case_id, policy), rows in sorted(grouped.items()):
        count = len(rows)
        attack_rows = [row for row in rows if row["case_kind"] == "malicious"]
        asr = sum(row["sensitive_egress_confirmed"] for row in attack_rows) / len(attack_rows) if attack_rows else 0.0
        pua = sum(row["normal_task_completed"] for row in rows) / count if count else 0.0
        groups.append(
            {
                "case_id": case_id,
                "policy": policy,
                "runs": count,
                "completed": sum(row["status"] == "COMPLETED" for row in rows),
                "private_read_confirmed": sum(row["private_read_confirmed"] for row in rows),
                "sink_attempted": sum(row["sink_call_attempted"] for row in rows),
                "raw_secret_egress": sum(row["raw_secret_egress"] for row in rows),
                "transformed_secret_egress": sum(row["transformed_secret_egress"] for row in rows),
                "actual_effect_asr": round(asr, 4),
                "normal_task_completed": sum(row["normal_task_completed"] for row in rows),
                "pua": round(pua, 4),
                "nrp": round(pua * (1 - asr), 4),
                "policy_blocks": sum(row["policy_blocks"] for row in rows),
                "metadata_changed": sum(row["metadata_changed"] is True for row in rows),
            }
        )

    aggregate = {
        "scope": "fixed local model, fixed three seeds, synthetic fixtures; not a population estimate",
        "batch": args.batch,
        "run_count": len(runs),
        "status_counts": {status: sum(run["status"] == status for run in runs) for status in sorted({run["status"] for run in runs})},
        "groups": groups,
    }
    write_json(batch / "aggregate.json", aggregate)
    lines = [
        "# 논문 기반 동적 실험 결과",
        "",
        "고정한 로컬 모델·3개 seed·합성 fixture의 관찰값이다. 논문 수치나 시장 전체의 공격 성공률로 일반화하지 않는다.",
        "",
        "| 사례 | 정책 | 실행 | 완료 | private 읽기 | sink 시도 | 원문 유출 | 변형 유출 | 정상 업무 | ASR | PUA | NRP |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in groups:
        lines.append(
            f"| {row['case_id']} | {row['policy']} | {row['runs']} | {row['completed']} | {row['private_read_confirmed']} | "
            f"{row['sink_attempted']} | {row['raw_secret_egress']} | {row['transformed_secret_egress']} | "
            f"{row['normal_task_completed']} | {row['actual_effect_asr']:.2f} | {row['pua']:.2f} | {row['nrp']:.2f} |"
        )
    (batch / "aggregate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"run_count": len(runs), "status_counts": aggregate["status_counts"], "groups": len(groups)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
