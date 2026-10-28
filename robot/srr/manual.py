"""手動モード（リモコンの「手動」）。基の昇降・ヨーを 1 本ずつ、可動範囲の中で直接動かす。

昇降は高さの割合（0% が最下、100% が最上）、ヨーはテーブル角（°）で指定する。動きはいつもの速度・加速度の上限で
なめらかに（Axis.move_to）。同じ基の昇降とヨーは同時に動かさない（片方が動いている間、もう片方は受け付けない）。
"""
from __future__ import annotations

from .unit import State

AXIS_LABEL = {'lift': '昇降', 'yaw': 'ヨー'}
SPEED = 0.6   # 手動で動かすときの速さ（上限に対する割合）


class ManualSession:
    def __init__(self, cfg, units):
        self.cfg, self.units = cfg, units
        self.targets = [(u, axis) for u in units for axis in ('lift', 'yaw')]
        self.idx = 0
        self.message = ''
        self.home_lift = cfg.lift.home
        self.top_lift = cfg.lift.clamp(cfg.raw['lift']['top_deg'])

    # ---- 換算 ----
    def lift_percent(self, pos: float) -> float:
        return (pos - self.home_lift) / (self.top_lift - self.home_lift) * 100

    def lift_pos(self, percent: float) -> float:
        p = max(0.0, min(100.0, percent))
        return self.cfg.lift.clamp(self.home_lift + (self.top_lift - self.home_lift) * p / 100)

    @property
    def target(self):
        return self.targets[self.idx]

    # ---- 操作 ----
    def select(self, step: int) -> None:
        self.idx = (self.idx + step) % len(self.targets)
        self.message = ''

    def adjust(self, delta: float) -> None:
        u, axis = self.target
        ax, other = (u.lift, u.yaw) if axis == 'lift' else (u.yaw, u.lift)
        if other.moving:
            self.message = f'{AXIS_LABEL["yaw" if axis == "lift" else "lift"]}が動いている間は動かせない'
            return
        if ax.pos is None:
            self.message = 'まだ位置が分からない（原点へ戻す）'
            return
        goal = ax.target if ax.target is not None else ax.pos   # 続けて押したら、目標に足していく
        if axis == 'lift':
            new = self.lift_pos(self.lift_percent(goal) + delta)
        else:
            new = self.cfg.yaw.clamp(goal + delta)
        self.message = '' if new != goal else '可動範囲の端'
        ax.move_to(new, SPEED)

    def home(self) -> None:
        u, _ = self.target
        if u.lift.moving or u.yaw.moving:
            self.message = '動いている間は戻せない'
            return
        u.abort(State.IDLE)   # ヨーを正面にしてから下げる（unit.home_steps）
        self.message = '原点へ戻しています'

    def lines(self) -> list[str]:
        u, axis = self.target
        ax = getattr(u, axis)
        head = f'{self.idx + 1}/{len(self.targets)} {u.name} {AXIS_LABEL[axis]}'
        if ax.pos is None:
            return [head, '位置が分からない', '', self.message]
        if axis == 'lift':
            value = f'高さ {self.lift_percent(ax.pos):.0f}%'
            rng = '0%（最下）〜 100%（最上）'
        else:
            value = f'向き {ax.pos:+.1f}°'
            rng = f'{self.cfg.yaw.lo:+.0f}° 〜 {self.cfg.yaw.hi:+.0f}°（＋は客から見て右）'
        status = self.message or ('移動中…' if ax.moving else '±1 / ±10 で動かす')
        return [head, value, rng, status]

    def exit(self) -> None:
        """手動を終える。全基を原点へ戻す（戻り終えると待機）。"""
        for u in self.units:
            if u.lift.pos is not None:
                u.abort(State.IDLE)
