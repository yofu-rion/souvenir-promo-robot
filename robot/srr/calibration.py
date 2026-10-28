"""角度調整（リモコンから）。機構に組んだ状態で、実際に使う位置にサーボを合わせてパルス幅を決める。

ホーンを付けた後はサーボの端（±range/2）まで回すと機構に当たるので、端ではなく使う位置で合わせる。
  昇降：下（0°）→ パルス中心、上（ソフト上限）→ 負側の傾き
  ヨー：正面（0°）→ パルス中心、±上限 → 両側の傾き
合わせた値から pulse_min / center / max を逆算して calibration.yaml に保存する。

動かすときは毎秒 RATE_US_S の速さでパルス幅を変え、昇降の前にヨーを正面へ、ヨーの前に昇降を下へ戻す。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import yaml

from .config import PULSE_SAFE_US, ServoConfig
from .servo import Servo

log = logging.getLogger(__name__)

RATE_US_S = 400.0   # 移動の速さ（270° 版で約 54°/s。昇降 1 ストローク 1 s 以上・テーブル 60°/s 以下に収まる）
MAX_DEV_US = 250.0  # 調整を始めた値からこれ以上は動かさない（機構に当てない）
AXIS_LABEL = {'lift': '昇降', 'yaw': 'ヨー'}


@dataclass
class Point:
    unit: object        # unit.Unit
    axis: str           # lift / yaw
    key: str            # bottom / top / front / plus / minus
    label: str
    logical: float      # 論理角（昇降：ホーン角、ヨー：テーブル角）

    @property
    def servo(self) -> Servo:
        return getattr(self.unit, self.axis).servo

    @property
    def servo_deg(self) -> float:
        return getattr(self.unit, self.axis).to_servo(self.logical)


def make_points(cfg, units) -> list[Point]:
    top = cfg.lift.clamp(cfg.raw['lift']['top_deg'])
    pts = []
    for u in units:
        pts += [
            Point(u, 'lift', 'bottom', '下（0°）', 0.0),
            Point(u, 'lift', 'top', f'上（{top:+.1f}°）', top),
            Point(u, 'yaw', 'front', '正面（0°）', 0.0),
            Point(u, 'yaw', 'plus', f'{cfg.yaw.hi:+.0f}°（客から見て右）', cfg.yaw.hi),
            Point(u, 'yaw', 'minus', f'{cfg.yaw.lo:+.0f}°（客から見て左）', cfg.yaw.lo),
        ]
    return pts


def derive_pulses(sc: ServoConfig, half: float, center_us: float,
                  measured: list[tuple[float, float]]) -> tuple[float, float, float]:
    """パルス中心と、中心以外で合わせた（サーボ角, µs）から (min, center, max) を出す。

    Servo.pulse_for と同じく中心の左右で別々の直線なので、片側 1 点ずつで決まる。
    合わせていない側は、元の幅（中心からの差）をそのまま新しい中心に付ける。
    """
    lo_span = sc.pulse_center_us - sc.pulse_min_us
    hi_span = sc.pulse_max_us - sc.pulse_center_us
    for deg, us in measured:
        s = (-deg if sc.invert else deg) / half
        if s < 0:
            lo_span = (center_us - us) / -s
        elif s > 0:
            hi_span = (us - center_us) / s
    return center_us - lo_span, center_us, center_us + hi_span


class CalibSession:
    def __init__(self, cfg, units, out_path: str | None = None):
        self.cfg, self.units = cfg, units
        self.out_path = out_path or cfg.calibration_file
        self.points = make_points(cfg, units)
        self.idx = 0
        self.us = {id(p): p.servo.pulse_for(p.servo_deg) for p in self.points}
        self.base = dict(self.us)        # 保存済みの値（変更の有無を見る）
        self.start = dict(self.us)       # 調整を始めた値（MAX_DEV_US の基準）
        # 今出しているパルス幅。開始時は各軸の今の位置から求める
        self.out: dict[int, float] = {}
        for u in units:
            for name in ('lift', 'yaw'):
                ax = getattr(u, name)
                self.out[id(ax.servo)] = ax.servo.pulse_for(ax.to_servo(ax.pos if ax.pos is not None else 0.0))
        self.queue: list[tuple[Servo, float]] = []
        self.message = ''
        self.exiting = False
        self.finished = False
        self.discard_armed = False
        self._go_to(self.point)

    # ---- 状態 ----
    @property
    def point(self) -> Point:
        return self.points[self.idx]

    @property
    def moving(self) -> bool:
        return bool(self.queue)

    @property
    def dirty(self) -> bool:
        return any(abs(self.us[k] - self.base[k]) > 0.5 for k in self.us)

    def _find(self, unit, key) -> Point:
        return next(p for p in self.points if p.unit is unit and p.key == key)

    def lines(self) -> list[str]:
        p = self.point
        us = self.us[id(p)]
        if self.exiting:
            return ['角度調整を終わります', '原点へ戻しています…', '', '']
        status = self.message or ('移動中…' if self.moving else ('未保存の変更あり' if self.dirty else '◀ ▶ で点を選ぶ'))
        return [f'{self.idx + 1}/{len(self.points)} {p.unit.name} {AXIS_LABEL[p.axis]}', p.label,
                f'{us:.0f} µs（{us - self.base[id(p)]:+.0f}）', status]

    # ---- 操作 ----
    def _go_to(self, p: Point) -> None:
        """他の軸を安全な位置に戻してから、その点へ動かす。"""
        u = p.unit
        if p.axis == 'lift':
            front = self._find(u, 'front')
            self.queue.append((front.servo, self.us[id(front)]))
        else:
            bottom = self._find(u, 'bottom')
            self.queue.append((bottom.servo, self.us[id(bottom)]))
        self.queue.append((p.servo, self.us[id(p)]))

    def select(self, step: int) -> None:
        if self.exiting:
            return
        self.message, self.discard_armed = '', False
        self.idx = (self.idx + step) % len(self.points)
        self._go_to(self.point)

    def adjust(self, delta: float) -> None:
        if self.exiting:
            return
        p = self.point
        k = id(p)
        lo = max(PULSE_SAFE_US[0], self.start[k] - MAX_DEV_US)
        hi = min(PULSE_SAFE_US[1], self.start[k] + MAX_DEV_US)
        new = max(lo, min(hi, self.us[k] + delta))
        self.message = '' if new == self.us[k] + delta else f'調整できるのは ±{MAX_DEV_US:.0f} µs まで'
        self.discard_armed = False
        self.us[k] = new
        # 続けて押したときは、まだ着いていない目標を書き換える（キューを積み上げない）
        if self.queue and self.queue[-1][0] is p.servo:
            self.queue[-1] = (p.servo, new)
        else:
            self.queue.append((p.servo, new))

    def save(self) -> bool:
        if self.exiting:
            return False
        half = self.cfg.servo_half_range
        results = {}
        for u in self.units:
            res = {}
            for axis, center_key, others in (('lift', 'bottom', ('top',)), ('yaw', 'front', ('plus', 'minus'))):
                sc = getattr(u.ucfg, axis)
                center = self.us[id(self._find(u, center_key))]
                measured = [(q.servo_deg, self.us[id(q)]) for q in (self._find(u, k) for k in others)]
                mn, ce, mx = derive_pulses(sc, half, center, measured)
                if not (PULSE_SAFE_US[0] <= mn < ce < mx <= PULSE_SAFE_US[1]):
                    self.message = f'{u.name} {AXIS_LABEL[axis]}: 計算した範囲 {mn:.0f}〜{mx:.0f} µs が安全範囲外'
                    return False
                res[axis] = (sc, mn, ce, mx)
            results[u.name] = res
        data = self._read_file()
        units = data.setdefault('units', {})
        for name, res in results.items():
            for axis, (sc, mn, ce, mx) in res.items():
                sc.pulse_min_us, sc.pulse_center_us, sc.pulse_max_us = mn, ce, mx
                units.setdefault(name, {})[axis] = {
                    'pulse_min_us': round(mn, 1), 'pulse_center_us': round(ce, 1), 'pulse_max_us': round(mx, 1)}
        try:
            self._write_file(data)
        except OSError as e:
            self.message = f'保存できない: {e}'
            return False
        self.base = dict(self.us)
        self.message = '保存しました'
        log.info('角度調整を保存した: %s', self.out_path)
        return True

    def _read_file(self) -> dict:
        try:
            with open(self.out_path, encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}

    def _write_file(self, data: dict) -> None:
        tmp = self.out_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('# リモコンの角度調整で保存したパルス幅（µs）。config.yaml の値より優先される\n')
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.out_path)

    def request_exit(self, force: bool = False) -> bool:
        """原点へ戻して終わる。未保存の変更があれば、もう一度押されるまで待つ（force なら即）。"""
        if self.exiting:
            return True
        if self.dirty and not force and not self.discard_armed:
            self.discard_armed = True
            self.message = '未保存。もう一度押すと破棄'
            return False
        self.exiting = True
        self.queue.clear()
        for u in self.units:
            # 保存した（または元の）設定で原点の位置へ。ヨーを正面にしてから下げる
            self.queue.append((u.yaw.servo, u.yaw.servo.pulse_for(u.yaw.to_servo(0.0))))
            self.queue.append((u.lift.servo, u.lift.servo.pulse_for(u.lift.to_servo(self.cfg.lift.home))))
        return True

    # ---- 毎周期 ----
    def tick(self, dt: float) -> None:
        if not self.queue:
            if self.exiting:
                self.finished = True
            return
        servo, target = self.queue[0]
        cur = self.out.get(id(servo), target)
        step = RATE_US_S * dt
        nxt = target if abs(target - cur) <= step else cur + (step if target > cur else -step)
        servo.set_pulse_us(nxt)
        self.out[id(servo)] = nxt
        if nxt == target:
            self.queue.pop(0)
