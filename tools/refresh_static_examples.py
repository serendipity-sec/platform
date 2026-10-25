"""Regenerate the static-scan rule, reference examples and schema after a C contract change."""

from pathlib import Path

from flowdelta.ingestion import json_bytes
from flowdelta.runtime.static_examples import write_static_examples
from flowdelta.runtime.static_rules import RULE_PATH, rule_yaml
from flowdelta.runtime.static_scan import static_schema

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    (ROOT / RULE_PATH).write_bytes(rule_yaml().encode("utf-8"))
    write_static_examples(ROOT / "examples/static-scan", ROOT)
    (ROOT / "schemas/static-scan-0.4.0.schema.json").write_bytes(json_bytes(static_schema()))


if __name__ == "__main__":
    main()
