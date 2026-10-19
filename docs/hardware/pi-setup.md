# 実機（Raspberry Pi）の環境構築

駆動ユニット2基（昇降サーボ・ヨーサーボ・ロードセル）を Raspberry Pi 5 1台で動かす。ピン割り当て・電源・受け入れ確認は、要件ドキュメント「[商品自己推薦ロボット システム要件（実機立ち上げ用）](https://claude.ai/artifact/F5UaW2FXtHF9ZjsqB2cjcc)」が正。
配線・端子台の割り当ては [wiring.md](wiring.md)、M5Stack リモコンは [remote.md](remote.md)。

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

## 制御ソフト（robot/srr）

設定は `robot/config.yaml`。パルス幅・HX711 係数は仮の値なので、下の校正で実測値に置き換える。コマンドは `robot/` で実行する（`cd robot`、Python は `../.venv/bin/python`）。

| 立ち上げ手順 | コマンド | 内容 |
| --- | --- | --- |
| 確認 | `python -m srr status` | 可動範囲・速度・各サーボの出力先（GPIO → pwmchip）を表示 |
| 2. サーボ単体 | `python -m srr servo 1 yaw --servo-angle 135` | 1 本だけ、加減速なしで動かす。`--servo-angle` はサーボ角、`--us` はパルス幅、`--angle` は論理角（可動範囲に丸める）。Ctrl-C で PWM を止める |
| 2. ホーン取り付け | `python -m srr servo 1 lift --angle 0` と `servo 1 yaw --angle 0` | 「昇降 0°」「テーブル正面」に置いてからホーンを付ける |
| 3. 機構に組んで | `python -m srr move 1 lift -60.5`、`move 1 yaw 90` | 加減速つきで動かす。前回の姿勢から始め、着いた姿勢を保存する。昇降はヨーが正面のときだけ |
| 4. 重量 | `python -m srr scale`、`python -m srr scale-cal 1 --known 500` | 生値とグラムを表示。既知の重りで係数を出し、`config.yaml` に書く値を表示する。今は `hx711.enabled: false`（使わない）で、`run` は重さを見ずに動く |
| 5. 音声 | `python -m srr play 1` | その基の音声を鳴らす（`robot/sounds/unit1.wav`、16 bit WAV） |
| 6. 通し運転 | `python -m srr run`（ロードセルなしなら `--no-scale`） | 本番の動作。Ctrl-C で原点へ戻してから止まる |

- **論理角**：昇降はホーン角（0° 最下 〜 −61.5° 最上、ソフト上限は −1.0〜−60.5°）、ヨーはテーブル角（±90°、180° 版サーボなら ±60°）。ヨーのサーボ角は −1.5 × テーブル角。
- **パルス幅**：`pulse_min_us` / `pulse_max_us` はサーボ角 −range/2・+range/2、`pulse_center_us` はサーボ角 0°（昇降 0°・テーブル正面）。左右で別々に直線補間する。
- **状態**：原点移動 → 待機 → 動作中（上昇 → 客側へ旋回 → 音声 → 正面 → 下降）→ 待機。商品が持ち上げられたら原点へ戻って「商品なし」で待ち、戻されたら待機へ。動作中に重量が `surge_g` 以上急に変わったら止めて原点へ戻る。
- **起動・終了**：起動時は台が空なら風袋引きする（商品が載っていたら保存したゼロ点を使う）。原点移動は 1 基ずつ、通常の 30% の速度で、前回の終了姿勢から始める（`~/.local/state/srr-robot/pose.json`）。`servo` で加減速なしに動かした基は姿勢が分からなくなるので、次の起動では原点へ直接出す。
- **PCA9685 への移行**：`config.yaml` の `pwm.backend` を `pca9685` にする。チャンネルは既定で 1 基目昇降 = 0、ヨー = 1、2 基目昇降 = 2、ヨー = 3（`pca9685_channel` で変更可）。
- **テスト**：`npm run test:robot`（PWM・HX711・音声をモックにして状態機械まで通す）。

### 常駐（systemd）

```sh
tools/install-service.sh --now     # /etc/systemd/system/srr-robot.service を入れて起動（sudo を使う）
journalctl -u srr-robot -f         # ログ
sudo systemctl stop srr-robot      # 止める（原点へ戻してから PWM を止める）
```

異常終了したら 3 秒後に自動で再起動する。サービスはユーザーのログイン前に動くので PipeWire を使えない。音声は `config.yaml` の `audio.device` に ALSA のデバイスを直接書く（例：`plughw:CARD=MAX98357A`。名前は `aplay -L` で確認）。

## メモ

- Pi 5 の GPIO は RP1 チップ経由。RPi.GPIO と pigpio は使えないので、lgpio か gpiozero を使う。
- ハード PWM は `pwmchip0` の ch0 = GPIO12、ch1 = GPIO13。50 Hz なら `period` = 20000000 ns。
- pwm-pio は 1 本ごとに別の pwmchip（ch0 のみ）になる。番号は起動順で決まり GPIO の順ではない（例：`pwmchip1` = `pwm_pio@17` = GPIO23、`pwmchip2` = `pwm_pio@10` = GPIO16）。ソフトでは `device` のリンク先 `pwm_pio@<GPIO の16進>` から引く。
