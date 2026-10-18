# 実機（Raspberry Pi）の環境構築

駆動ユニット2基（昇降サーボ・ヨーサーボ・ロードセル）を Raspberry Pi 5 1台で動かす。ピン割り当て・電源・受け入れ確認は、要件ドキュメント「[商品自己推薦ロボット システム要件（実機立ち上げ用）](https://claude.ai/artifact/F5UaW2FXtHF9ZjsqB2cjcc)」が正。

## セットアップ

```sh
tools/setup-pi.sh                    # apt・ブートローダー・overlay・.venv・Node（何度実行してもよい）
sudo reboot                          # ブートローダー更新と PWM overlay の反映に必要
.venv/bin/python tools/check-env.py  # 接続機器の確認（--say で音声合成の声を鳴らす）
```

アンプ基板の overlay が決まったら、`I2S_OVERLAY=max98357a tools/setup-pi.sh` のように指定して実行し直す（未確定事項）。

## 入るもの

| 用途 | 中身 |
| --- | --- |
| サーボ PWM | `config.txt` に `pwm-2chan`（GPIO12/13 のハード PWM、昇降）と `pwm-pio` ×2（GPIO16/23、ヨー）。sysfs `/sys/class/pwm` を gpio グループで操作できる |
| ブートローダー | `rpi-eeprom-update -a`。2024-09 版では RP1 の PIO が起動せず、`pwm-pio` が使えない |
| Python | `.venv`（`--system-site-packages`）。gpiozero・lgpio・numpy・pyserial・smbus2 は apt 版。PyYAML・PCA9685 用ライブラリは `robot/requirements.txt` から |
| 重量 | HX711 は lgpio で読む（SCK GPIO5 共通、DT GPIO6/26） |
| 音声 | ALSA/PipeWire（`aplay`）、Open JTalk（WAV の作成）、sox、ffmpeg |
| モーション計算 | Node 20（`npm test` で `sim/motion-core.js` のテスト） |

## 要件ドキュメントとの差分

- OS は要件の Bookworm ではなく Debian 13（trixie）版の Raspberry Pi OS。gpiozero・lgpio・pwm-pio は trixie でも同じように使える。
- UART は要件のピン表にないため有効にしない。SPI と I2C は有効のまま（I2C は PCA9685 導入後に使う）。

## メモ

- Pi 5 の GPIO は RP1 チップ経由。RPi.GPIO と pigpio は使えないので、lgpio か gpiozero を使う。
- ハード PWM は `pwmchip0` の ch0 = GPIO12、ch1 = GPIO13。50 Hz なら `period` = 20000000 ns。
- pwm-pio は 1 本ごとに別の pwmchip（ch0 のみ）になる。番号は起動順で決まり GPIO の順ではない（例：`pwmchip1` = `pwm_pio@17` = GPIO23、`pwmchip2` = `pwm_pio@10` = GPIO16）。ソフトでは `device` のリンク先 `pwm_pio@<GPIO の16進>` から引く。
