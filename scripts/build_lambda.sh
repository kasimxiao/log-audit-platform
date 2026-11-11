#!/bin/bash
# 打包 Lambda 代码（arm64 / py3.12）：app 源码 + 依赖 + GeoIP 库 + 评估集
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
B="$ROOT/backend/build"
rm -rf "$B" && mkdir -p "$B/geo"
cp "$ROOT"/backend/app/*.py "$ROOT"/backend/app/collector.sh "$B/"
cp "$ROOT/evals/dataset.jsonl" "$B/dataset.jsonl"
cp "$ROOT/data/geo/dbip-country-lite.mmdb" "$B/geo/"
"$ROOT/.venv/bin/pip" install -q --target "$B" --platform manylinux2014_aarch64 --python-version 3.12 \
  --implementation cp --only-binary=:all: sqlglot==$("$ROOT/.venv/bin/pip" show sqlglot | awk '/^Version/{print $2}') maxminddb tzdata
find "$B" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
# 入库 Lambda 单独一个包：额外带 pyarrow（写 Parquet），避免其他函数背上 100MB+ 依赖
E="$ROOT/backend/build_extractor"
rm -rf "$E" && cp -R "$B" "$E" && rm -rf "$E/geo"
"$ROOT/.venv/bin/pip" install -q --target "$E" --platform manylinux2014_aarch64 --python-version 3.12 \
  --implementation cp --only-binary=:all: pyarrow
find "$E" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$E"/pyarrow/include "$E"/pyarrow/tests 2>/dev/null || true
du -sh "$B" "$E"
