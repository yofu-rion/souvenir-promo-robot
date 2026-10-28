"""1 軸の台形速度追従。論理角（昇降：ホーン角、ヨー：テーブル角）で動かし、毎周期サーボへ出す。

sim/motion-core.js の stepAxis と同じ考え方（速度・加速度上限つきで目標へ追従）。
"""
from __future__ import annotations

import math

from .config import AxisLimits
from .servo import Servo


class Axis:
    def __init__(self, name: str, limits: AxisLimits, servo: Servo, ratio: float = 1.0):
        self.name, self.limits, self.servo, self.ratio = name, limits, servo, ratio
        self.pos: float | None = None   # 論理角。None = まだ出力していない（位置不明）
        self.vel = 0.0
        self.target: float | None = None
        self.speed_scale = 1.0
        self.accel_scale = 1.0   # 加速度の倍率（リモコンの「速さ」。100% を超えるとキビキビ動く）

    def to_servo(self, logical: float) -> float:
        return self.ratio * logical

    def from_servo(self, servo_deg: float) -> float:
        return servo_deg / self.ratio

    def start_at(self, logical: float) -> None:
        """位置を決めて出力を始める（起動時の初期姿勢）。"""
        self.pos = self.limits.clamp(logical)
        self.vel = 0.0
        self.target = self.pos
        self.servo.set_angle(self.to_servo(self.pos))

    def move_to(self, logical: float, speed_scale: float = 1.0) -> None:
        if self.pos is None:
            raise RuntimeError(f'{self.name}: 位置が不明なまま動かそうとした（start_at が先）')
        self.target = self.limits.clamp(logical)
        self.speed_scale = max(0.05, min(1.0, speed_scale))

    @property
    def moving(self) -> bool:
        return self.pos is not None and (self.pos != self.target or self.vel != 0.0)

    def stop(self) -> None:
        """その場で止める（減速なし。異常時用）。"""
        if self.pos is not None:
            self.target, self.vel = self.pos, 0.0

    def update(self, dt: float) -> None:
        if self.pos is None or not self.moving:
            return
        lim = self.limits
        vmax, amax = lim.vmax * self.speed_scale, lim.amax * self.accel_scale
        err = self.target - self.pos
        v_des = math.copysign(min(vmax, math.sqrt(2 * amax * abs(err))), err)
        dv = amax * dt
        self.vel += max(-dv, min(dv, v_des - self.vel))
        nxt = self.pos + self.vel * dt
        if (self.target - nxt) * err <= 0:  # 目標を越えた／着いた
            nxt, self.vel = self.target, 0.0
        self.pos = lim.clamp(nxt)
        if self.pos != nxt:
            self.vel = 0.0
        self.servo.set_angle(self.to_servo(self.pos))
