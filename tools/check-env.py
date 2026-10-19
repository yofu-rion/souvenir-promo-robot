#!/usr/bin/env python3
"""実機の環境と接続機器を確認する。
  使い方: .venv/bin/python tools/check-env.py [--say]
  --say を付けると、Open JTalk で合成した声を既定の出力から鳴らす。
機器をつないだら実行し、PWM・HX711・I2C・カメラ・音声デバイスが見えているかを確かめる。
"""
import glob
import importlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, NG, INFO = '[OK]', '[NG]', '[--]'

# よく使う I2C デバイスのアドレス
I2C_NAMES = {
    0x40: 'PCA9685（サーボドライバ）',
    0x70: 'PCA9685 全体呼び出し',
    0x29: 'VL53L0X/VL53L1X（ToF 距離）',
    0x3c: 'SSD1306（OLED）',
    0x68: 'MPU6050 / DS3231',
    0x48: 'ADS1115（AD 変換）',
}


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, '', str(e))


def section(title):
    print(f'\n== {title}')


def check_board():
    section('ボード')
    with open('/proc/device-tree/model') as f:
        print(INFO, f.read().strip('\x00\n'))
    print(INFO, 'Python', sys.version.split()[0], '(venv)' if sys.prefix != sys.base_prefix else '(venv ではない)')
    node = run(['node', '--version'])
    print(OK if node.returncode == 0 else NG, 'Node', node.stdout.strip() or node.stderr.strip())


def check_python_modules():
    section('Python モジュール')
    for name in ['numpy', 'cv2', 'lgpio', 'gpiozero', 'smbus2', 'serial', 'picamera2', 'yaml',
                 'sounddevice', 'soundfile', 'adafruit_pca9685', 'adafruit_servokit']:
        try:
            mod = importlib.import_module(name)
            print(OK, name, getattr(mod, '__version__', ''))
        except Exception as e:  # sounddevice は PortAudio がないと OSError になる
            print(NG, name, f'{type(e).__name__}: {e}')


def check_power():
    section('電源・ブートローダー')
    t = run(['vcgencmd', 'get_throttled']).stdout.strip()
    print(OK if t.endswith('=0x0') else NG, t, '' if t.endswith('=0x0') else '（電圧不足か温度制限の履歴あり）')
    pending = os.path.exists('/boot/firmware/pieeprom.upd')
    print(INFO if pending else OK, 'ブートローダー更新', '再起動待ち' if pending else '保留なし')
    pio = os.path.exists('/dev/pio0')
    print(OK if pio else NG, 'RP1 PIO（ヨーの pwm-pio に必要）', '/dev/pio0' if pio else 'なし（ブートローダー更新と再起動が必要）')


def check_pwm():
    section('サーボ PWM（昇降 GPIO12/13、ヨー GPIO16/23）')
    for chip in sorted(glob.glob('/sys/class/pwm/pwmchip*')):
        dev = os.path.basename(os.path.realpath(os.path.join(chip, 'device')))
        with open(os.path.join(chip, 'npwm')) as f:
            print(OK, os.path.basename(chip), dev, f'{f.read().strip()}ch')
    if not glob.glob('/sys/class/pwm/pwmchip*'):
        print(NG, 'pwmchip なし（config.txt の overlay と再起動を確認）')
    out = run(['pinctrl', 'get', '12,13,16,23']).stdout
    for line in out.splitlines():
        print('    ', line.strip())


def check_hx711():
    section('重量 HX711（SCK GPIO5、DT GPIO6/26）')
    try:
        import lgpio
        import time
        h = lgpio.gpiochip_open(0)
        try:
            # SCK が High のままだと HX711 は休止して DT を High に保つので、先に Low にする
            lgpio.gpio_claim_output(h, 5, 0)
            time.sleep(0.2)
            for gpio, label in [(6, '1基目'), (26, '2基目')]:
                lgpio.gpio_claim_input(h, gpio, lgpio.SET_PULL_UP)
                # HX711 は変換が終わるたびに DT を Low にする（10/80 Hz）。つながっていなければ High のまま
                end, low = time.monotonic() + 0.3, False
                while time.monotonic() < end and not low:
                    low = lgpio.gpio_read(h, gpio) == 0
                lgpio.gpio_free(h, gpio)
                print(OK if low else INFO, f'{label} DT GPIO{gpio}', '応答あり' if low else '応答なし')
        finally:
            lgpio.gpiochip_close(h)
    except Exception as e:
        print(NG, 'GPIO を読めない', f'{type(e).__name__}: {e}')


def check_buses():
    section('バス')
    for pattern, label in [('/dev/i2c-1', 'I2C（GPIO2/3、PCA9685 導入後）'), ('/dev/spidev0.*', 'SPI0'),
                           ('/dev/ttyUSB*', 'USB シリアル'), ('/dev/ttyACM*', 'USB CDC')]:
        found = sorted(glob.glob(pattern))
        print(OK if found else INFO, label, ' '.join(found) or 'なし')
    if os.path.exists('/dev/i2c-1'):
        out = run(['i2cdetect', '-y', '1']).stdout
        addrs = [int(tok, 16) for line in out.splitlines()[1:] for tok in line.split()[1:]
                 if tok not in ('--', 'UU')]
        if not addrs:
            print(INFO, 'I2C-1 に応答するデバイスなし')
        for a in addrs:
            print(OK, f'I2C-1 0x{a:02x}', I2C_NAMES.get(a, ''))


def check_camera():
    section('カメラ')
    out = run(['rpicam-hello', '--list-cameras'])
    text = (out.stdout + out.stderr).strip()
    has = 'Available cameras' in text
    print(OK if has else INFO, 'CSI カメラ' if has else 'CSI カメラなし')
    if has:
        for line in text.splitlines():
            if line.strip()[:1].isdigit():
                print('    ', line.strip(), '← AI カメラ' if 'imx500' in line else '')
    if os.path.isdir('/usr/share/imx500-models'):
        print(INFO, 'IMX500 モデル', len(os.listdir('/usr/share/imx500-models')), '個（/usr/share/imx500-models）')
    usb = [d for d in sorted(glob.glob('/dev/v4l/by-id/*')) if 'usb' in d.lower()]
    print(OK if usb else INFO, 'USB カメラ', ' '.join(os.path.basename(d) for d in usb) or 'なし')


def check_audio(say):
    section('音声')
    for cmd, label in [(['aplay', '-l'], '再生'), (['arecord', '-l'], '録音')]:
        cards = [l for l in run(cmd).stdout.splitlines() if l.startswith('card ')]
        print(OK if cards else INFO, label, 'デバイスなし' if not cards else '')
        for c in cards:
            print('    ', c)
    sink = run(['pactl', 'get-default-sink']).stdout.strip()
    print(INFO, '既定の出力', sink or '不明')

    voice = '/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice'
    dic = '/var/lib/mecab/dic/open-jtalk/naist-jdic'
    if not (shutil.which('open_jtalk') and os.path.exists(voice) and os.path.isdir(dic)):
        print(NG, 'Open JTalk が見つからない')
        return
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, 'say.wav')
        r = run(['sh', '-c', f'echo "ねえねえ、こっち見て！" | open_jtalk -x {dic} -m {voice} -ow {wav}'])
        ok = r.returncode == 0 and os.path.getsize(wav) > 1000
        print(OK if ok else NG, 'Open JTalk で音声合成', r.stderr.strip())
        if ok and say:
            p = run(['paplay', wav]) if shutil.which('paplay') else run(['aplay', '-q', wav])
            print(OK if p.returncode == 0 else NG, '再生', p.stderr.strip())


def check_tests():
    section('モーション計算のテスト（npm test）')
    r = subprocess.run(['npm', 'test', '--silent'], cwd=ROOT, capture_output=True, text=True)
    summary = [l for l in r.stdout.splitlines() if l.startswith(('# pass', '# fail'))]
    print(OK if r.returncode == 0 else NG, ' / '.join(s.lstrip('# ') for s in summary))


if __name__ == '__main__':
    check_board()
    check_python_modules()
    check_power()
    check_pwm()
    check_hx711()
    check_buses()
    check_camera()
    check_audio('--say' in sys.argv)
    check_tests()
