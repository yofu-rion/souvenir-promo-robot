"""1 基分の動作。昇降・ヨーの 2 軸と重量から、状態（待機 / 動作中 / 商品なし / 停止）を進める。

動きは「ステップ」の列で表し、1 つずつ実行する。昇降とヨーを同時に動かすことはない。
昇降はヨーが正面にあるときだけ動かす（上昇してから旋回、正面に戻してから下降）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .axis import Axis
from .config import Config, UnitConfig
from .hx711 import Scale

log = logging.getLogger(__name__)
FRONT_TOL_DEG = 0.5


class State(Enum):
    HOMING = '原点移動'
    IDLE = '待機'
    ACTIVE = '動作中'
    NO_PRODUCT = '商品なし'
    STOPPED = '停止'


@dataclass
class Step:
    kind: str            # lift / yaw / wait / speak
    value: float = 0.0   # 目標角（論理角）か待ち時間
    speed: float = 1.0   # 速度の倍率（原点移動は遅くする）


class InterlockError(RuntimeError):
    pass


class Unit:
    def __init__(self, cfg: Config, ucfg: UnitConfig, lift: Axis, yaw: Axis, scale: Scale, player):
        self.cfg, self.ucfg = cfg, ucfg
        self.name = ucfg.name
        self.lift, self.yaw, self.scale, self.player = lift, yaw, scale, player
        self.state = State.STOPPED
        self.steps: list[Step] = []
        self.step: Step | None = None
        self.wait_left = 0.0
        self.speaking = False
        self.after_abort: State | None = None
        self.present_for = 0.0
        self.absent_for = 0.0
        self.baseline_g: float | None = None
        self.use_scale = True   # False = 重量を見ない（ロードセル未接続での動作確認用）

    @property
    def has_product(self) -> bool:
        return not self.use_scale or (self.scale.filled and self.scale.present)

    # ---- 姿勢 ----
    @property
    def lift_home(self) -> float:
        return self.cfg.lift.home

    @property
    def lift_top(self) -> float:
        lim, top = self.cfg.lift, self.cfg.raw['lift']['top_deg']
        return lim.clamp(top)

    @property
    def yaw_front(self) -> bool:
        return self.yaw.pos is not None and abs(self.yaw.pos) <= FRONT_TOL_DEG and not self.yaw.moving

    @property
    def at_home(self) -> bool:
        return self.yaw_front and self.lift.pos == self.lift_home and not self.lift.moving

    @property
    def busy(self) -> bool:
        return self.step is not None or bool(self.steps)

    def pose(self) -> dict:
        return {'lift': self.lift.pos, 'yaw': self.yaw.pos}

    def home_steps(self, speed: float = 1.0) -> list[Step]:
        return [Step('yaw', 0.0, speed), Step('lift', self.lift_home, speed)]

    # ---- 状態の切り替え ----
    def begin_homing(self, pose: dict | None, speed: float) -> None:
        """出力を始めて、保存していた姿勢（なければ原点）から原点へゆっくり戻す（F7）。"""
        pose = pose or {}
        self.lift.start_at(pose.get('lift') if pose.get('lift') is not None else self.lift_home)
        self.yaw.start_at(pose.get('yaw') if pose.get('yaw') is not None else 0.0)
        self.steps, self.step = self.home_steps(speed), None
        self._set(State.HOMING)

    def start_cycle(self) -> bool:
        """1 サイクル（F5）：上昇 → 客側へ旋回 → 音声 → 正面へ戻る → 下降。"""
        if self.state != State.IDLE or self.busy:
            return False
        c = self.cfg
        self.steps = [
            Step('lift', self.lift_top),
            Step('wait', c.hold_top_s),
            Step('yaw', c.yaw_present_deg),
            Step('speak'),
            Step('wait', c.after_speech_s),
            Step('yaw', 0.0),
            Step('lift', self.lift_home),
        ]
        self.baseline_g = self.scale.grams if self.use_scale and self.scale.filled else None
        self._set(State.ACTIVE)
        return True

    def abort(self, then: State, speed: float = 1.0) -> None:
        """今の動きをやめて原点（下降・正面）へ戻る。戻り終えたら then の状態にする。"""
        if self.speaking:
            self.player.stop()
            self.speaking = False
        if self.lift.moving:
            # 昇降中はヨーが正面にあるので、そのまま下降へ切り替える
            self.steps = [Step('lift', self.lift_home, speed)]
        else:
            self.steps = self.home_steps(speed)
        self.step = None
        self.after_abort = then

    def stop(self, speed: float = 1.0) -> None:
        """終了（F8）：原点へ戻してから停止にする。PWM を止めるのはコントローラ。"""
        self.abort(State.STOPPED, speed)

    def _set(self, state: State) -> None:
        if state != self.state:
            log.info('%s: %s → %s', self.name, self.state.value, state.value)
            self.state = state

    # ---- 毎周期 ----
    def tick(self, dt: float) -> None:
        self._watch_weight(dt)
        self._run_steps(dt)
        self.lift.update(dt)
        self.yaw.update(dt)

    def _watch_weight(self, dt: float) -> None:
        if not self.use_scale or not self.scale.filled:
            return
        present = self.scale.present
        self.present_for = self.present_for + dt if present else 0.0
        self.absent_for = 0.0 if present else self.absent_for + dt
        c = self.cfg
        if self.after_abort is not None:
            return
        if self.state in (State.IDLE, State.ACTIVE) and self.absent_for >= c.absent_hold_s:
            log.info('%s: 商品が持ち上げられた（%.0f g）', self.name, self.scale.grams)
            self.abort(State.NO_PRODUCT)
        elif self.state == State.ACTIVE and self.baseline_g is not None \
                and abs(self.scale.grams - self.baseline_g) >= c.surge_g:
            log.warning('%s: 動作中に重量が急変（%.0f → %.0f g）。止めて原点へ戻す',
                        self.name, self.baseline_g, self.scale.grams)
            self.abort(State.IDLE)
        elif self.state == State.NO_PRODUCT and not self.busy and self.present_for >= c.return_hold_s:
            log.info('%s: 商品が戻された（%.0f g）', self.name, self.scale.grams)
            self._set(State.IDLE)

    def _run_steps(self, dt: float) -> None:
        if self.step is None:
            if not self.steps:
                self._finished()
                return
            self.step = self.steps.pop(0)
            self._enter(self.step)
        if self._done(self.step, dt):
            self.step = None

    def _enter(self, s: Step) -> None:
        if s.kind == 'lift':
            if self.yaw.moving or not self.yaw_front:
                raise InterlockError(f'{self.name}: ヨーが正面にないまま昇降しようとした')
            self.lift.move_to(s.value, s.speed)
        elif s.kind == 'yaw':
            if self.lift.moving:
                raise InterlockError(f'{self.name}: 昇降中にヨーを動かそうとした')
            self.yaw.move_to(s.value, s.speed)
        elif s.kind == 'wait':
            self.wait_left = s.value
        elif s.kind == 'speak':
            self.speaking = False

    def _done(self, s: Step, dt: float) -> bool:
        if s.kind == 'lift':
            return not self.lift.moving
        if s.kind == 'yaw':
            return not self.yaw.moving
        if s.kind == 'wait':
            self.wait_left -= dt
            return self.wait_left <= 0
        if s.kind == 'speak':
            if not self.speaking:
                if self.player.playing:   # もう一方の基が話している
                    return False
                snd = self.ucfg.sound
                self.player.play(snd.file, snd.volume, snd.channel)
                self.speaking = True
                return False
            if self.player.playing:
                return False
            self.speaking = False
            return True
        raise ValueError(s.kind)

    def _finished(self) -> None:
        if self.after_abort is not None:
            nxt, self.after_abort = self.after_abort, None
            if nxt == State.IDLE and not self.has_product:
                nxt = State.NO_PRODUCT
            self._set(nxt)
        elif self.state in (State.HOMING, State.ACTIVE):
            self._set(State.IDLE if self.has_product else State.NO_PRODUCT)
        self.baseline_g = None
