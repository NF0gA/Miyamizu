#!/usr/bin/env bash
# Miyamizu をアプリメニュー(KDE 等)に登録する。ソース実行(start.sh)/ビルド版(dist/Miyamizu/Miyamizu)のどちらでも可。
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
EXEC="$HERE/start.sh"
[ -x "$HERE/dist/Miyamizu/Miyamizu" ] && EXEC="$HERE/dist/Miyamizu/Miyamizu"
mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/512x512/apps
cp -f "$HERE/miyamizu/assets/icon-512.png" ~/.local/share/icons/hicolor/512x512/apps/miyamizu.png
cat > ~/.local/share/applications/miyamizu.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Miyamizu
Comment=音質(時間軸の正確さ)優先の音楽プレイヤー
Exec=$EXEC
Icon=miyamizu
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupWMClass=miyamizu
DESK
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "登録しました: ~/.local/share/applications/miyamizu.desktop"
