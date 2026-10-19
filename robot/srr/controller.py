"""2 基をまとめて 50 Hz で回す。起動時の原点移動（1 基ずつ）、一定間隔の自動動作、終了処理。

リモコン（remote.py）があれば、開始・停止・1 回だけの動作と、角度調整（calibration.py）を受け付ける。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time

from .audio import Player
from .axis import Axis
from .calibration import CalibSession
from .config import Config
from .hx711 import Gpio, HX711Bus, Scale
from .servo import Servo, make_pwm
from .unit import State, Unit

log = logging.getLogger(__name__)
TICK_HZ = 50
REMOTE_MIN_S = 0.2       # リモコンへ送る最短の間隔（重量の表示で毎周期変わるので間引く）
REMOTE_REFRESH_S = 2.0   # 変わらなくても送り直す間隔（取りこぼし対策）


def write_pose(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning('姿勢を保存できない: %s', e)


def forget_pose(path: str, name: str) -> None:
    """加減速なしで動かした後など、姿勢が分からなくなった基の記録を消す。"""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if data.pop(name, None) is not None:
        write_pose(path, data)


def build_units(cfg: Config, player, backend: str | None = None) -> list[Unit]:
    backend = backend or cfg.pwm_backend
    units = []
    for u in cfg.units:
        def servo(sc, label):
            pwm = make_pwm(backend, sc, cfg.pwm_frequency_hz, cfg.pca9685_address, f'{u.name}.{label}')
            return Servo(sc, pwm, cfg.servo_range_deg, f'{u.name}.{label}')
        lift = Axis(f'{u.name}.昇降', cfg.lift, servo(u.lift, 'lift'), ratio=1.0)
        yaw = Axis(f'{u.name}.ヨー', cfg.yaw, servo(u.yaw, 'yaw'), ratio=cfg.yaw_gear_ratio)
        scale = Scale(u.scale.offset, u.scale.counts_per_g, u.scale.present_g, cfg.average_window)
        units.append(Unit(cfg, u, lift, yaw, scale, player))
    return units


class Controller:
    def __init__(self, cfg: Config, units: list[Unit], bus: HX711Bus | None, player, remote=None):
        self.cfg, self.units, self.bus, self.player = cfg, units, bus, player
        self.now = 0.0
        self.homing_queue: list[Unit] = []
        self.homing_next_at = 0.0
        self.next_unit = 0
        self.last_cycle_end = 0.0
        self.stopping = False
        self.pose = self._load_pose()
        if bus is None:
            for u in units:
                u.use_scale = False
        # リモコン：paused の間は自動動作を始めない。calib は角度調整中のセッション
        self.remote = remote
        self.paused = remote is not None and cfg.remote_start_paused
        self.calib: CalibSession | None = None
        self.calib_pending = False
        self.remote_sent: dict | None = None
        self.remote_sent_at = -1e9

    # ---- 姿勢の保存 ----
    def _load_pose(self) -> dict:
        try:
            with open(self.cfg.state_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_pose(self) -> None:
        for u in self.units:
            if u.lift.pos is not None:
                self.pose[u.name] = u.pose()
        write_pose(self.cfg.state_file, self.pose)

    # ---- 起動 ----
    def read_scales(self) -> None:
        if self.bus is None or not self.bus.ready():
            return
        vals = self.bus.read(timeout=0)
        if vals is None:
            return
        for u, raw in zip(self.units, vals):
            u.scale.add_raw(raw)

    def tare(self, timeout: float = 3.0, sleep=time.sleep) -> None:
        """サーボを動かす前に重量を貯め、台が空の基だけ風袋引きする。"""
        if self.bus is None:
            return
        end = time.monotonic() + timeout
        while not all(u.scale.filled for u in self.units) and time.monotonic() < end:
            self.read_scales()
            sleep(0.01)
        for u in self.units:
            if not u.scale.filled:
                log.warning('%s: HX711 から読めない', u.name)
            elif u.scale.tare_if_empty():
                log.info('%s: 風袋引き（生値 %.0f）', u.name, u.scale.offset)
            else:
                log.info('%s: 商品が載っているので保存済みのゼロ点を使う（%.0f g）', u.name, u.scale.grams)

    def start(self) -> None:
        """1 基ずつ出力を始めて原点へ移す（突入電流を避ける）。"""
        self.homing_queue = list(self.units)
        self.homing_next_at = self.now

    # ---- 毎周期 ----
    def tick(self, dt: float) -> None:
        self.now += dt
        self.read_scales()
        self._remote_events()
        if self.calib is not None:
            self._calib_tick(dt)
            self._push_remote()
            return
        if self.calib_pending and not self.homing_queue \
                and not any(u.busy or u.state in (State.HOMING, State.ACTIVE) for u in self.units):
            self.calib_pending = False
            log.info('角度調整を始める')
            self.calib = CalibSession(self.cfg, self.units)
        self._homing()
        if not self.stopping:
            self._schedule()
        for u in self.units:
            if u.lift.pos is not None:
                was_active = u.state == State.ACTIVE
                u.tick(dt)
                if was_active and u.state != State.ACTIVE:
                    self.last_cycle_end = self.now
                    self.save_pose()
        if hasattr(self.player, 'step'):
            self.player.step(dt)
        self._push_remote()

    def _homing(self) -> None:
        if not self.homing_queue or self.now < self.homing_next_at:
            return
        if any(u.state == State.HOMING for u in self.units):
            return
        u = self.homing_queue.pop(0)
        log.info('%s: 原点移動を始める', u.name)
        u.begin_homing(self.pose.get(u.name), self.cfg.homing_speed_scale)
        self.homing_next_at = self.now + self.cfg.homing_gap_s

    def _schedule(self) -> None:
        """一定間隔で、待機中の基を 1 基ずつ交互に動かす（音声が重ならないように）。"""
        if self.paused or self.calib_pending:
            return
        if self.now - self.last_cycle_end < self.cfg.auto_interval_s:
            return
        self._start_next()

    def _start_next(self) -> bool:
        if self.homing_queue or any(u.state in (State.HOMING, State.ACTIVE) for u in self.units):
            return False
        n = len(self.units)
        for k in range(n):
            u = self.units[(self.next_unit + k) % n]
            if u.state == State.IDLE and u.has_product and u.start_cycle():
                self.next_unit = (self.next_unit + k + 1) % n
                return True
        return False

    # ---- リモコン ----
    def _remote_events(self) -> None:
        if self.remote is None:
            return
        for ev in self.remote.poll():
            self.handle_remote(ev)

    def handle_remote(self, ev: dict) -> None:
        c, v = ev.get('c'), ev.get('v')
        if c == 'hello':
            self.remote_sent, self.remote_sent_at = None, -1e9   # つなぎ直したので画面を送り直す
            return
        if self.stopping:
            return
        if self.calib is not None:
            if c == 'mode' and v == 'remote':
                self.calib.request_exit()
            elif c == 'prev':
                self.calib.select(-1)
            elif c == 'next':
                self.calib.select(1)
            elif c == 'adj' and isinstance(v, (int, float)):
                self.calib.adjust(float(v))
            elif c == 'save':
                self.calib.save()
            self.remote_sent_at = -1e9   # 押した結果はすぐ返す
            return
        if c == 'start' and not self.calib_pending:
            log.info('リモコン：開始')
            self.paused = False
            self.last_cycle_end = self.now - self.cfg.auto_interval_s  # すぐ 1 回目を始める
        elif c == 'stop':
            log.info('リモコン：停止')
            self.paused = True
            for u in self.units:
                if u.state == State.ACTIVE:
                    u.abort(State.IDLE)
        elif c == 'once' and not self.calib_pending:
            if not self._start_next():
                log.info('リモコン：1 回動かせる基がない（原点移動中・動作中・商品なし）')
        elif c == 'mode' and v == 'calib':
            log.info('リモコン：角度調整へ。原点へ戻してから始める')
            self.paused = True
            self.calib_pending = True
            for u in self.units:
                if u.state == State.ACTIVE:
                    u.abort(State.IDLE)
        elif c == 'mode' and v == 'remote':
            self.calib_pending = False
        self.remote_sent_at = -1e9

    def _calib_tick(self, dt: float) -> None:
        if self.stopping:
            self.calib.request_exit(force=True)
        self.calib.tick(dt)
        if not self.calib.finished:
            return
        # 原点のパルス幅まで戻したので、その位置から通常の制御に戻す（保存した設定で）
        for u in self.units:
            u.yaw.start_at(0.0)
            u.lift.start_at(self.cfg.lift.home)
            u.steps, u.step = [], None
            u._set(State.STOPPED if self.stopping else (State.IDLE if u.has_product else State.NO_PRODUCT))
        log.info('角度調整を終えた')
        self.calib = None
        self.save_pose()
        self.remote_sent_at = -1e9

    def remote_view(self) -> dict:
        if self.calib is not None:
            return {'m': 'calib', 'run': False, 't': self.calib.lines()}
        if self.calib_pending:
            return {'m': 'calib', 'run': False, 't': ['角度調整の準備', '原点へ戻しています…', '', '']}
        head = '停止中（終了処理）' if self.stopping else ('停止中：開始で動く' if self.paused else '運転中')
        rows = []
        for u in self.units:
            w = f'{u.scale.grams:.0f} g' if u.use_scale and u.scale.filled else '— g'
            rows.append(f'{u.name} {u.state.value} {w}')
        return {'m': 'remote', 'run': not self.paused and not self.stopping, 't': [head] + rows[:3]}

    def _push_remote(self) -> None:
        if self.remote is None or not self.remote.connected:
            return
        since = self.now - self.remote_sent_at
        if since < REMOTE_MIN_S:
            return
        view = self.remote_view()
        if view != self.remote_sent or since >= REMOTE_REFRESH_S:
            self.remote.send(view)
            self.remote_sent, self.remote_sent_at = view, self.now

    # ---- 終了 ----
    def request_stop(self) -> None:
        if self.stopping:
            return
        log.info('終了：原点へ戻してから PWM を止める')
        self.stopping = True
        self.homing_queue = []
        for u in self.units:
            if u.lift.pos is not None:
                u.stop()

    @property
    def stopped(self) -> bool:
        return self.stopping and self.calib is None and all(u.lift.pos is None or (u.state == State.STOPPED and not u.busy)
                                     for u in self.units)

    def close(self) -> None:
        self.save_pose()
        for u in self.units:
            for ax in (u.lift, u.yaw):
                ax.servo.close()
        if self.bus is not None:
            self.bus.power_down()

    def status(self) -> str:
        parts = []
        for u in self.units:
            w = f'{u.scale.grams:.0f} g' if u.use_scale and u.scale.filled else '— g'
            lp = '—' if u.lift.pos is None else f'{u.lift.pos:.1f}°'
            yp = '—' if u.yaw.pos is None else f'{u.yaw.pos:.1f}°'
            parts.append(f'{u.name} {u.state.value} 昇降 {lp} ヨー {yp} {w}')
        return ' | '.join(parts)


def run(cfg: Config, no_scale: bool = False, no_remote: bool = False, stop_timeout: float = 20.0) -> None:
    """systemd から呼ぶ本番ループ。SIGTERM / SIGINT で原点へ戻して止まる。"""
    player = Player(cfg.audio_device, cfg.speech_fallback_s)
    units = build_units(cfg, player)
    gpio = bus = None
    if not cfg.hx711_enabled and not no_scale:
        log.info('重量センサーは使わない（config の hx711.enabled: false）。台はいつも商品ありとして動かす')
    if not no_scale and cfg.hx711_enabled:
        gpio = Gpio()
        bus = HX711Bus(gpio, cfg.hx711_sck_gpio, [u.scale.dt_gpio for u in cfg.units])
    remote = None
    if cfg.remote_enabled and not no_remote:
        from .remote import RemoteLink
        remote = RemoteLink(cfg.remote_name)
        remote.start()
        log.info('リモコン（%s）を探す。%s', cfg.remote_name,
                 '「開始」を押すまで動かさない' if cfg.remote_start_paused else '自動で動かす')
    ctl = Controller(cfg, units, bus, player, remote)

    signal.signal(signal.SIGTERM, lambda *_: ctl.request_stop())
    signal.signal(signal.SIGINT, lambda *_: ctl.request_stop())

    try:
        ctl.tare()
        ctl.start()
        dt = 1.0 / TICK_HZ
        nxt = time.monotonic()
        last_log = stop_at = 0.0
        while not ctl.stopped:
            ctl.tick(dt)
            if ctl.stopping and not stop_at:
                stop_at = time.monotonic() + stop_timeout
            if stop_at and time.monotonic() > stop_at:
                log.error('原点へ戻りきらないまま時間切れ。PWM を止める')
                break
            if time.monotonic() - last_log > 10:
                log.info(ctl.status())
                last_log = time.monotonic()
            nxt += dt
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()  # 遅れたら取り戻そうとせず、そこから数え直す
    finally:
        ctl.close()
        if remote is not None:
            remote.close()
        if gpio is not None:
            gpio.close()
        log.info('停止した')
