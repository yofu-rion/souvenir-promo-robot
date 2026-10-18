#!/usr/bin/env bash
# Raspberry Pi（実機）の開発環境を整える。何度実行してもよい。
#   使い方: tools/setup-pi.sh
#           I2S_OVERLAY=max98357a tools/setup-pi.sh   # アンプ基板の overlay が決まったら指定する
#   内容:   apt パッケージ、ブートローダー更新、I2C/SPI と サーボ用 PWM の有効化、Python 仮想環境（.venv）、Node の確認
# ピン割り当ては「商品自己推薦ロボット システム要件（実機立ち上げ用）」のピン表に従う。
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

echo "== ブートローダー（EEPROM）"
# 古いブートローダーでは RP1 の PIO が使えず、pwm-pio（ヨーサーボ）が動かない
sudo rpi-eeprom-update -a

echo "== インターフェースの有効化（0 = 有効）"
sudo raspi-config nonint do_i2c 0   # PCA9685 導入後に使う
sudo raspi-config nonint do_spi 0

echo "== サーボ用 PWM・I2S（/boot/firmware/config.txt）"
# 昇降：ハード PWM GPIO12/13、ヨー：PIO の PWM GPIO16/23（I2S が GPIO18/19/21 を使うため）
CONFIG=/boot/firmware/config.txt
BEGIN='# >>> souvenir-promo-robot'
END='# <<< souvenir-promo-robot'
block="$BEGIN
[all]
dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
dtoverlay=pwm-pio,gpio=16
dtoverlay=pwm-pio,gpio=23"
if [ -n "${I2S_OVERLAY:-}" ]; then
  block="$block
dtoverlay=$I2S_OVERLAY"
fi
block="$block
$END"
sudo sed -i "/^$BEGIN\$/,/^$END\$/d" "$CONFIG"
printf '%s\n' "$block" | sudo tee -a "$CONFIG" >/dev/null

echo "== グループ（GPIO・I2C・SPI・音声・カメラ）"
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
echo "完了。ブートローダー更新と PWM overlay は再起動後に反映される（sudo reboot）。"
echo "接続確認: .venv/bin/python tools/check-env.py"
