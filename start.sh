#!/usr/bin/env bash
# Miyamizu 起動スクリプト: venv 作成 → 有効化 → 不足パッケージのみ導入 → 起動
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VENV="${MIYAMIZU_VENV:-.venv}"
PY="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
    echo "[start] venv を作成します: $VENV"
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# requirements.txt の各要件が満たされているか確認し、足りない時だけ pip を呼ぶ
python -c "import packaging" 2>/dev/null || python -m pip install -q packaging
if ! python tools/check_requirements.py >/dev/null 2>&1
then
    echo "[start] 不足パッケージを導入します"
    python -m pip install --upgrade pip >/dev/null 2>&1 || true
    python -m pip install -r requirements.txt
fi

exec python -m miyamizu "$@"
