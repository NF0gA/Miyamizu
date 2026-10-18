#!/usr/bin/env bash
# Miyamizu を PyInstaller でビルド: venv 作成 → 有効化 → 不足パッケージのみ導入 → ビルド
# 出力: dist/Miyamizu/Miyamizu (フォルダ配布。icon.png を実行ファイルの隣に置くと優先して読み込む)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VENV="${MIYAMIZU_VENV:-.venv}"
PY="${PYTHON:-python3}"
[ -x "$VENV/bin/python" ] || { echo "[build] venv を作成します: $VENV"; "$PY" -m venv "$VENV"; }
# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -c "import packaging" 2>/dev/null || python -m pip install -q packaging
need=0
python tools/check_requirements.py --build >/dev/null 2>&1 || need=1
if [ "$need" = 1 ]; then
    echo "[build] 不足パッケージを導入します"
    python -m pip install --upgrade pip >/dev/null 2>&1 || true
    python -m pip install -r requirements.txt pyinstaller
fi

# ノイズシェーピング用の C ライブラリ(cc が無くても動く。その場合は実行時に再試行し、無理なら機能が無効になる)
if command -v cc >/dev/null 2>&1; then
    cc -O2 -shared -fPIC miyamizu/nshape.c -o miyamizu/libmiyamizu_nshape.so -lm && echo "[build] nshape ライブラリをビルドしました" || echo "[build] nshape のビルドに失敗(実行時に再試行します)"
else
    echo "[build] cc が無いので nshape ライブラリは同梱しません(実行時に cc があれば自動でビルド)"
fi

rm -rf build dist
pyinstaller --noconfirm --clean miyamizu.spec
cp -f icon.png dist/Miyamizu/icon.png 2>/dev/null || true
echo "[build] 完了: dist/Miyamizu/Miyamizu"
