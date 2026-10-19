#!/usr/bin/env bash
# 実機制御を systemd サービス（srr-robot）として入れる。何度実行してもよい。
#   使い方: tools/install-service.sh          入れて、起動時に自動で動くようにする（今は起動しない）
#           tools/install-service.sh --now    入れて、すぐ起動する
# ログ: journalctl -u srr-robot -f   止める: sudo systemctl stop srr-robot
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
UNIT=/etc/systemd/system/srr-robot.service

sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$USER|g" robot/systemd/srr-robot.service | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable srr-robot.service
if [ "${1:-}" = "--now" ]; then
  sudo systemctl restart srr-robot.service
  systemctl --no-pager status srr-robot.service | head -12
fi
echo "入れた: $UNIT"
