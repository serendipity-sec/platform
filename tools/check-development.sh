#!/bin/sh
set -eu

python -c 'import sys; assert sys.version_info[:3] == (3, 13, 15), sys.version; print(sys.version)'
uv --version
test "$(uv --version | cut -d ' ' -f 2)" = "0.9.30"
ruff check --no-cache src tests
ruff format --no-cache --check src tests
mypy --cache-dir /tmp/flowdelta-mypy
pytest -p no:cacheprovider
flowdelta check-examples examples/contracts/order-lookup
flowdelta check-examples examples/contracts/support-triage
check_output=$(mktemp -d /tmp/flowdelta-plan.XXXXXX)
flowdelta plan examples/contracts/order-lookup \
  --catalog examples/contracts/order-lookup/template-catalog.json \
  --cards examples/contracts/order-lookup/cards \
  --out "$check_output/plan.json"
