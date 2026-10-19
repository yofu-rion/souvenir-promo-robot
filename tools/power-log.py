#!/usr/bin/env python3
"""Pi 5 の電源を 1 秒に 5 回記録する（瞬断・電圧低下の調査用）。
  使い方: tools/power-log.py [出力ファイル]   既定 /var/log/srr-power.log
  記録: 時刻、入力 5 V（EXT5V）、3.3 V、throttled、起動からの秒数
落ちる直前の行を見れば、電圧が下がってから落ちたのか、いきなり切れたのかが分かる。
"""
import os
import re
import subprocess
import sys
import time

OUT = sys.argv[1] if len(sys.argv) > 1 else '/var/log/srr-power.log'
INTERVAL = 0.2


def volts(name: str) -> float:
    out = subprocess.run(['vcgencmd', 'pmic_read_adc', name], capture_output=True, text=True).stdout
    m = re.search(r'=([\d.]+)V', out)
    return float(m.group(1)) if m else float('nan')


def throttled() -> str:
    out = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True).stdout
    return out.strip().split('=')[-1]


def uptime() -> float:
    with open('/proc/uptime') as f:
        return float(f.read().split()[0])


def main() -> None:
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.write(fd, f'# 起動 {time.strftime("%F %T")}（起動から {uptime():.0f} s）\n'.encode())
    low5, low33 = 9.9, 9.9
    while True:
        v5, v33, th = volts('EXT5V_V'), volts('3V3_SYS_V'), throttled()
        low5, low33 = min(low5, v5), min(low33, v33)
        line = (f'{time.strftime("%F %T")}.{int(time.time() * 10) % 10} 5V={v5:.3f} 3V3={v33:.3f} '
                f'throttled={th} 最低5V={low5:.3f} 最低3V3={low33:.3f} up={uptime():.1f}\n')
        os.write(fd, line.encode())
        os.fsync(fd)   # 電源が切れても直前の行まで残す
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
