"""1 基分の動作。昇降・ヨーの 2 軸と重量から、状態（待機 / 動作中 / 商品なし / 停止）を進める。

動きは「ステップ」の列で表し、1 つずつ実行する。昇降とヨーを同時に動かすことはない（要件）。
通常のサイクルは上昇してから旋回、正面に戻してから下降する。台本の説明（explain.py）では、
客側を向いたまま昇降してうなずく（ヨーが止まっている間だけ昇降する）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
    kind: str            # lift / yaw / wait / speak / talk / sync
    value: float = 0.0   # 目標角（論理角）か待ち時間。talk ではヨーの中心
    speed: float = 1.0   # 速度の倍率（原点移動は遅くする）
    file: str | None = None   # talk：流す声
    label: str = ''           # talk：リモコンに出す「n/全体 文」
    # talk：声の始まりからの時刻に合わせた動き (秒, 'lift'|'yaw', 目標の論理角, 速度の倍率[, 狙う時刻])
    cues: list = field(default_factory=list)
    end_lift: float | None = None   # talk：声が終わったら戻す高さ
    channel: str | None = None      # talk：左右の指定を上書き（stereo＝左右に別々の声）
    # talk：話す前のポーズの変更 (軸, 目標, 速さ) を順に出し、pre_lead 秒たったら（動いている途中でも）話し始める
    pre: list = field(default_factory=list)
    pre_lead: float = 0.0
    barrier: Barrier | None = None  # sync：待ち合わせ


class InterlockError(RuntimeError):
    pass


class Barrier:
    """2 基の掛け合いの待ち合わせ。全員が着いたら通す（一度通したらそのまま）。"""

    def __init__(self, names):
        self.names, self.arrived = set(names), set()

    def arrive(self, name):
        self.arrived.add(name)

    @property
    def released(self):
        return self.arrived >= self.names


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
        self.talk_t = 0.0
        self.talk_cues: list = []
        self.talk_phase = 'start'   # start / speak / settle / return
        self.talk_pre: list = []
        self.talk_pre_t = 0.0
        self.progress = ''       # 説明中の「n/全体 文」
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

    def start_explain(self, steps: list[Step]) -> bool:
        """台本による説明（explain.build_steps が作った手順）を始める。"""
        if self.state != State.IDLE or self.busy:
            return False
        self.steps = list(steps)
        self.baseline_g = self.scale.grams if self.use_scale and self.scale.filled else None
        self._set(State.ACTIVE)
        return True

    def abort(self, then: State, speed: float = 1.0) -> None:
        """今の動きをやめて原点（下降・正面）へ戻る。戻り終えたら then の状態にする。"""
        if self.speaking:
            self.player.stop()
            self.speaking = False
        self.talk_phase, self.talk_cues, self.talk_pre, self.progress = 'start', [], [], ''
        if self.lift.moving:
            # 昇降中はヨーが止まっているので、そのまま下降へ切り替え、下りてから正面へ戻す
            # （説明では横を向いたまま昇降するので、ヨーが正面とは限らない）
            self.steps = [Step('lift', self.lift_home, speed), Step('yaw', 0.0, speed)]
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
            if self.yaw.moving:
                raise InterlockError(f'{self.name}: ヨーが動いている間に昇降しようとした')
            self.lift.move_to(s.value, s.speed)
        elif s.kind == 'yaw':
            if self.lift.moving:
                raise InterlockError(f'{self.name}: 昇降中にヨーを動かそうとした')
            self.yaw.move_to(s.value, s.speed)
        elif s.kind == 'wait':
            self.wait_left = s.value
        elif s.kind == 'speak':
            self.speaking = False
        elif s.kind == 'sync':
            s.barrier.arrive(self.name)
        elif s.kind == 'talk':
            self.speaking, self.talk_phase, self.talk_t = False, 'start', 0.0
            self.talk_cues = sorted(s.cues, key=lambda c: c[0])
            self.talk_pre, self.talk_pre_t = list(s.pre), 0.0
            self.progress = s.label

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
        if s.kind == 'talk':
            return self._talk(s, dt)
        if s.kind == 'sync':
            return s.barrier.released
        raise ValueError(s.kind)

    def _talk(self, s: Step, dt: float) -> bool:
        """声を流し、声の時刻に合わせた動き（cues）を順に出す。昇降とヨーは同時に動かさない。

        file が None なら聞き役で、声は流さず、もう一方の基が流し始めた声の時刻に合わせて cues を出す。
        pre（話す前のポーズの変更）は最初に順に出し、pre_lead 秒たったら動いている途中でも話し始める（テンポを上げる）。
        段階: start（他の基の声・前の動きを待つ）→ speak（声の時刻で cues を出す。前の動きが終わるまで次は待つ）
        → settle（声が終わったら残りの cues は捨て、今の動きを終えてから end_lift へ戻す）→ 終わり
        """
        if self.talk_pre and not (self.lift.moving or self.yaw.moving):   # 話す前のポーズの変更を順に
            axis, target, speed = self.talk_pre.pop(0)
            getattr(self, axis).move_to(target, speed)
        if self.talk_phase == 'start':
            self.talk_pre_t += dt
            if s.file is None:   # 聞き役：相手（もう一方の基）の声が始まったら、その声に合わせて動く
                if self.player.playing:
                    self.talk_t, self.talk_phase = 0.0, 'speak'
                return False
            if self.player.playing or self.talk_pre_t < s.pre_lead:
                return False
            if not s.pre and (self.lift.moving or self.yaw.moving):
                return False
            snd = self.ucfg.sound
            self.player.play(s.file, snd.volume, s.channel or snd.channel)
            self.speaking, self.talk_t, self.talk_phase = True, 0.0, 'speak'
            return False
        if self.talk_phase == 'speak':
            self.talk_t += dt
            if self.player.playing:
                if self.talk_cues and self.talk_t >= self.talk_cues[0][0] and not self.talk_pre \
                        and not (self.lift.moving or self.yaw.moving):
                    cue = self.talk_cues.pop(0)
                    getattr(self, cue[1]).move_to(cue[2], cue[3])
                return False
            self.speaking, self.talk_cues, self.talk_phase = False, [], 'settle'
        if self.talk_phase == 'settle':
            if self.talk_pre or self.lift.moving or self.yaw.moving:
                return False
            self.talk_phase = 'return'
            if s.end_lift is not None:
                self.lift.move_to(s.end_lift, 0.6)
            return False
        if self.lift.moving or self.yaw.moving:   # return
            return False
        self.progress = ''
        return True

    def _finished(self) -> None:
        if self.after_abort is not None:
            nxt, self.after_abort = self.after_abort, None
            if nxt == State.IDLE and not self.has_product:
                nxt = State.NO_PRODUCT
            self._set(nxt)
        elif self.state in (State.HOMING, State.ACTIVE):
            self._set(State.IDLE if self.has_product else State.NO_PRODUCT)
        self.baseline_g = None
