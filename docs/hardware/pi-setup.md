# 実機（Raspberry Pi）の環境構築

実機の制御には Raspberry Pi 5（Raspberry Pi OS / Debian 13 trixie）を使う。

## セットアップ

```sh
tools/setup-pi.sh                    # apt・インターフェース・.venv・Node（何度実行してもよい）
sudo reboot                          # UART（GPIO14/15）を使うときだけ必要
.venv/bin/python tools/check-env.py  # 接続機器の確認（--say で音声合成の声を鳴らす）
```

## 入るもの

| 用途 | 中身 |
| --- | --- |
| モーション計算 | Node 20（`npm test` で `sim/motion-core.js` のテスト） |
| Python | `.venv`（`--system-site-packages`）。picamera2・libcamera・lgpio・gpiozero・numpy・opencv・pyserial・smbus2 は apt 版を使い、それ以外は `robot/requirements.txt` から入れる |
| モーター | PCA9685（I2C サーボドライバ）用の Adafruit ライブラリ。シリアルバスサーボやステッピングドライバ（UART）には pyserial を使う |
| センサー | AI カメラ（IMX500）のファームウェアとモデル（`/usr/share/imx500-models`）。重量センサー（HX711）は lgpio で読む予定 |
| 音声 | Open JTalk（日本語の音声合成）、ALSA/PipeWire、sounddevice、sox、ffmpeg |

## インターフェース

| バス | デバイス | ピン | 用途の例 |
| --- | --- | --- | --- |
| I2C | `/dev/i2c-1` | GPIO2（SDA）/ GPIO3（SCL） | PCA9685、ToF 距離センサー |
| SPI | `/dev/spidev0.0`, `0.1` | GPIO8–11 | AD 変換、LED |
| UART | `/dev/ttyAMA0`（再起動後） | GPIO14（TX）/ GPIO15（RX） | バスサーボ、TMC2209 |
| GPIO | lgpio / gpiozero | 任意 | HX711（DT・SCK）、ステップ／方向 |

Pi 5 の GPIO は RP1 チップ経由なので、RPi.GPIO は使えない。代わりに lgpio か gpiozero を使う（RPi.GPIO 前提のライブラリは `python3-rpi-lgpio` の互換層で動く）。
