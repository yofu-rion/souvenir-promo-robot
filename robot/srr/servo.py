"""サーボ出力層。PWM の出し方（sysfs・PCA9685・モック）と、角度→パルス幅の変換を分ける。

PCA9685 に移るときは PwmOutput の実装を差し替えるだけで、Servo から上は変えない。
"""
from __future__ import annotations

import glob
import os
import time

from .config import PULSE_SAFE_US, ServoConfig

PWM_CLASS = '/sys/class/pwm'
# Pi 5（RP1）のハード PWM：GPIO → チャンネル。それ以外の GPIO は pwm-pio（1 本 1 チップ）
RP1_PWM_DEVICE = '1f00098000.pwm'
RP1_PWM_CHANNELS = {12: 0, 13: 1, 18: 2, 19: 3}


class PwmOutput:
    """1 チャンネルの PWM 出力。パルス幅（µs）で指令する。"""

    def set_pulse_us(self, us: float) -> None:
        raise NotImplementedError

    def disable(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.disable()


class MockPwm(PwmOutput):
    def __init__(self, name: str = ''):
        self.name = name
        self.pulse_us: float | None = None
        self.enabled = False
        self.history: list[float] = []

    def set_pulse_us(self, us):
        self.pulse_us = us
        self.enabled = True
        self.history.append(us)

    def disable(self):
        self.enabled = False


def sysfs_location(gpio: int) -> tuple[str, int]:
    """GPIO から（device 名, チャンネル）を返す。pwmchip の番号は起動順で変わるので使わない。"""
    if gpio in RP1_PWM_CHANNELS:
        return RP1_PWM_DEVICE, RP1_PWM_CHANNELS[gpio]
    return f'pwm_pio@{gpio:x}', 0


def find_pwmchip(device: str, root: str = PWM_CLASS) -> str:
    for chip in sorted(glob.glob(os.path.join(root, 'pwmchip*'))):
        if os.path.basename(os.path.realpath(os.path.join(chip, 'device'))) == device:
            return chip
    raise FileNotFoundError(f'{device} の pwmchip がない（config.txt の overlay と再起動を確認）')


class SysfsPwm(PwmOutput):
    def __init__(self, gpio: int, frequency_hz: float = 50.0, root: str = PWM_CLASS):
        device, self.channel = sysfs_location(gpio)
        self.gpio = gpio
        self.chip = find_pwmchip(device, root)
        self.path = os.path.join(self.chip, f'pwm{self.channel}')
        self.period_ns = int(round(1e9 / frequency_hz))
        if not os.path.isdir(self.path):
            self._write(os.path.join(self.chip, 'export'), self.channel)
        # export 直後は udev がグループ権限を付けるまで書けないことがある
        deadline = time.monotonic() + 2.0
        while True:
            try:
                self._write_attr('duty_cycle', 0)
                self._write_attr('period', self.period_ns)
                break
            except (FileNotFoundError, PermissionError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.enabled = False

    @staticmethod
    def _write(path, value):
        with open(path, 'w') as f:
            f.write(str(value))

    def _write_attr(self, name, value):
        self._write(os.path.join(self.path, name), value)

    def set_pulse_us(self, us):
        self._write_attr('duty_cycle', int(round(us * 1000)))
        if not self.enabled:
            self._write_attr('enable', 1)
            self.enabled = True

    def disable(self):
        if self.enabled:
            self._write_attr('enable', 0)
            self.enabled = False

    def close(self):
        self.disable()
        try:
            self._write(os.path.join(self.chip, 'unexport'), self.channel)
        except OSError:
            pass


class Pca9685Pwm(PwmOutput):
    """PCA9685 導入後の出力。ボードは全チャンネルで共有する。"""
    _boards: dict = {}

    def __init__(self, channel: int, frequency_hz: float = 50.0, address: int = 0x40):
        board = self._boards.get(address)
        if board is None:
            import board as _board  # Adafruit Blinka
            from adafruit_pca9685 import PCA9685
            board = PCA9685(_board.I2C(), address=address)
            board.frequency = int(frequency_hz)
            self._boards[address] = board
        self.ch = board.channels[channel]
        self.period_us = 1e6 / frequency_hz

    def set_pulse_us(self, us):
        self.ch.duty_cycle = int(round(us / self.period_us * 0xFFFF))

    def disable(self):
        self.ch.duty_cycle = 0


def make_pwm(backend: str, servo: ServoConfig, frequency_hz: float = 50.0, address: int = 0x40,
             name: str = '') -> PwmOutput:
    if backend == 'sysfs':
        return SysfsPwm(servo.gpio, frequency_hz)
    if backend == 'pca9685':
        return Pca9685Pwm(servo.pca9685_channel, frequency_hz, address)
    if backend == 'mock':
        return MockPwm(name)
    raise ValueError(f'pwm.backend が不明: {backend}')


class Servo:
    """サーボ角（度、0 = パルス中心）とパルス幅を相互に変換して出力する。

    min/max はサーボ角 −range/2・+range/2 に対応し、中心の左右で別々に直線補間する。
    """

    def __init__(self, cfg: ServoConfig, pwm: PwmOutput, range_deg: float, name: str = ''):
        self.cfg, self.pwm, self.name = cfg, pwm, name
        self.half = range_deg / 2
        self.angle: float | None = None

    def pulse_for(self, servo_deg: float) -> float:
        c = self.cfg
        s = max(-self.half, min(self.half, -servo_deg if c.invert else servo_deg)) / self.half
        span = (c.pulse_max_us - c.pulse_center_us) if s >= 0 else (c.pulse_center_us - c.pulse_min_us)
        return c.pulse_center_us + s * span

    def set_angle(self, servo_deg: float) -> float:
        us = self.pulse_for(servo_deg)
        self.pwm.set_pulse_us(us)
        self.angle = max(-self.half, min(self.half, servo_deg))
        return us

    def set_pulse_us(self, us: float) -> float:
        """校正用。安全範囲にだけ収めて、そのまま出す。"""
        us = max(PULSE_SAFE_US[0], min(PULSE_SAFE_US[1], us))
        self.pwm.set_pulse_us(us)
        self.angle = None
        return us

    def disable(self):
        self.pwm.disable()

    def close(self):
        self.pwm.close()
