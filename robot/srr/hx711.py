"""HX711（ロードセル）の読み取り。2 個で SCK を共有し、DT を同時に読む。

Pi 5 では lgpio でビットバンする。SCK の High が 60 µs を超えると HX711 が省電力に入るので、
1 ビットごとに High → Low をすぐ返す。プロセスが割り込まれて化けた値は外れ値として捨てる。
"""
from __future__ import annotations

import time
from collections import deque

GAIN_PULSES = {128: 1, 32: 2, 64: 3}  # 25〜27 発目でチャンネル A/B とゲインを決める


class Gpio:
    """lgpio を薄く包む。テストではこの代わりにモックを渡す。"""

    def __init__(self, chip: int = 0):
        import lgpio
        self.lg = lgpio
        self.h = lgpio.gpiochip_open(chip)

    def output(self, pin, level=0):
        self.lg.gpio_claim_output(self.h, pin, level)

    def input(self, pin):
        # 未接続のときに DT が Low（変換完了）に見えないようプルアップする
        self.lg.gpio_claim_input(self.h, pin, self.lg.SET_PULL_UP)

    def write(self, pin, level):
        self.lg.gpio_write(self.h, pin, level)

    def read(self, pin):
        return self.lg.gpio_read(self.h, pin)

    def close(self):
        self.lg.gpiochip_close(self.h)


def to_signed24(v: int) -> int:
    return v - (1 << 24) if v & 0x800000 else v


class HX711Bus:
    """SCK 共通の HX711 群。read() で全 DT の生値（符号付き 24 bit）を同時に返す。"""

    def __init__(self, gpio, sck: int, dts: list[int], gain: int = 128):
        self.gpio, self.sck, self.dts = gpio, sck, list(dts)
        self.extra = GAIN_PULSES[gain]
        gpio.output(sck, 0)
        for dt in self.dts:
            gpio.input(dt)

    def ready(self) -> bool:
        return all(self.gpio.read(dt) == 0 for dt in self.dts)

    def read(self, timeout: float = 0.5) -> list[int] | None:
        """全 DT が変換完了（Low）になるのを待って読む。時間切れは None。"""
        deadline = time.monotonic() + timeout
        while not self.ready():
            if time.monotonic() > deadline:
                return None
            time.sleep(0.001)
        g, vals = self.gpio, [0] * len(self.dts)
        for _ in range(24):
            g.write(self.sck, 1)
            g.write(self.sck, 0)
            for i, dt in enumerate(self.dts):
                vals[i] = (vals[i] << 1) | g.read(dt)
        for _ in range(self.extra):
            g.write(self.sck, 1)
            g.write(self.sck, 0)
        return [to_signed24(v) for v in vals]

    def power_down(self):
        self.gpio.write(self.sck, 1)
        time.sleep(0.0001)


class Scale:
    """1 基分の重量。生値 → グラム、移動平均、風袋引き、商品の有無の判定。"""

    def __init__(self, offset: float, counts_per_g: float, present_g: float, window: int = 8,
                 outlier_g: float = 2000.0):
        self.offset, self.counts_per_g, self.present_g = offset, counts_per_g, present_g
        self.buf: deque[float] = deque(maxlen=max(1, window))
        self.outlier_counts = outlier_g * abs(counts_per_g)
        self.last_raw: int | None = None

    def add_raw(self, raw: int) -> None:
        # ビットが化けた値（直前の平均から極端に離れた 1 点）は捨てる。2 回続けば本物とみなす
        if self.buf and abs(raw - self.mean_raw) > self.outlier_counts:
            if self.last_raw is None or abs(raw - self.last_raw) > self.outlier_counts:
                self.last_raw = raw
                return
            self.buf.clear()
        self.last_raw = raw
        self.buf.append(raw)

    @property
    def mean_raw(self) -> float:
        return sum(self.buf) / len(self.buf) if self.buf else float(self.offset)

    @property
    def grams(self) -> float:
        return (self.mean_raw - self.offset) / self.counts_per_g

    @property
    def present(self) -> bool:
        return self.grams >= self.present_g

    @property
    def filled(self) -> bool:
        return len(self.buf) == self.buf.maxlen

    def tare(self) -> None:
        self.offset = self.mean_raw

    def tare_if_empty(self) -> bool:
        """台が空らしいときだけ風袋引きする。起動時に商品が載っていたらゼロを取り直さない。"""
        if self.buf and abs(self.grams) < self.present_g:
            self.tare()
            return True
        return False
