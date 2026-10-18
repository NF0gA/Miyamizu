#!/usr/bin/env bash
# リアルタイム優先度(SCHED_FIFO)を使えるようにする設定(openSUSE 向け・要 sudo)。
#  1) rtkit を入れて起動(再ログイン不要。最も手軽)
#  2) /etc/security/limits.d/ に audio グループ用の rtprio/memlock を追加し、自分を audio グループに追加(再ログインが必要)
# どちらか一方でも RT は有効になります。何を変更するか表示して、確認してから実行します。
set -euo pipefail

CONF=/etc/security/limits.d/99-miyamizu-audio.conf
cat <<MSG
これから次を実行します(sudo のパスワードを聞かれます):
  1. sudo zypper in rtkit && sudo systemctl enable --now rtkit-daemon
  2. sudo tee $CONF   (@audio - rtprio 95 / @audio - memlock unlimited)
  3. sudo usermod -aG audio $USER
MSG
read -r -p "実行しますか? [y/N] " a
[ "${a:-N}" = "y" ] || { echo "中止しました"; exit 0; }

sudo zypper --non-interactive in rtkit || echo "(rtkit の導入に失敗/スキップ)"
sudo systemctl enable --now rtkit-daemon || echo "(rtkit-daemon の起動に失敗/スキップ)"
sudo rm -f /etc/security/limits.d/99-hifiplayer-audio.conf   # 旧名の設定ファイルがあれば置き換える
sudo mkdir -p "$(dirname "$CONF")"      # Tumbleweed には /etc/security/limits.d が無いことがある
printf '@audio - rtprio 95\n@audio - memlock unlimited\n' | sudo tee "$CONF" >/dev/null
sudo usermod -aG audio "$USER"
echo
echo "完了。rtkit は次回の再生から有効です。limits.d / audio グループは再ログイン後に有効になります。"
echo "rtkit の状態: $(systemctl is-active rtkit-daemon 2>/dev/null || true)"
echo "確認: プレイヤーの「RT:」行が SCHED_FIFO または rtkit になっていれば成功です。"
