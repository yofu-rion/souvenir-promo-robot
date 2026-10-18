#!/usr/bin/env bash
# Raspberry Pi（実機）の開発環境を整える。何度実行してもよい。
#   使い方: tools/setup-pi.sh
#   内容:   apt パッケージ、I2C/SPI/UART の有効化、Python 仮想環境（.venv）、Node の確認
# 終わったら tools/check-env.py で接続機器を確認する（docs/hardware/pi-setup.md 参照）。
set -euo pipefail
cd "$(dirname "$0")/.."

APT_PACKAGES=(
  # シミュレータ・motion-core.js のテスト（npm test）
  nodejs npm
  # Python（picamera2・libcamera・lgpio は apt 版を仮想環境から使う）
  python3-venv python3-dev build-essential
  python3-picamera2 python3-libcamera python3-lgpio python3-gpiozero python3-rpi-lgpio
  python3-numpy python3-opencv python3-serial python3-smbus2
  # AI カメラ（IMX500）のファームウェアとモデル
  imx500-all
  # バス診断
  i2c-tools
  # 音声：再生・録音・変換、日本語の音声合成（Open JTalk）
  alsa-utils pulseaudio-utils sox ffmpeg libportaudio2
  open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
)

echo "== apt パッケージ"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${APT_PACKAGES[@]}"

echo "== インターフェースの有効化（0 = 有効）"
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
# GPIO14/15 の UART をバスサーボ・ステッピングドライバ用に空ける（ログインコンソールは無効）
sudo raspi-config nonint do_serial_cons 1
sudo raspi-config nonint do_serial_hw 0

echo "== グループ（GPIO・I2C・SPI・シリアル・音声・カメラ）"
sudo usermod -aG gpio,i2c,spi,dialout,audio,video "$USER"

echo "== Python 仮想環境 .venv"
if [ ! -d .venv ]; then
  python3 -m venv --system-site-packages .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r robot/requirements.txt

echo "== Node"
node --version
npm --version

echo
echo "完了。UART の設定は再起動後に反映される（sudo reboot）。"
echo "接続確認: .venv/bin/python tools/check-env.py"
