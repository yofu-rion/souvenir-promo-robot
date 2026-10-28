"""実機の立ち上げ・校正用コマンド。

  python -m srr status                       設定と PWM の出力先を表示
  python -m srr servo 1 yaw --servo-angle 135  サーボ角で 1 本だけ動かす（ホーンなしの単体確認）
  python -m srr servo 1 lift --us 1500         パルス幅で 1 本だけ動かす（校正）
  python -m srr move 1 yaw 45                  機構に組んだ状態で、加減速つきで動かす（論理角）
  python -m srr scale                          HX711 の生値とグラムを表示
  python -m srr scale-cal 1 --known 500        既知の重さで係数を校正
  python -m srr play 1                         その基の音声を鳴らす
  python -m srr run [--no-scale] [--no-remote]  本番の動作（systemd から起動）。リモコンで開始・角度調整
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from . import config as config_mod
from .axis import Axis
from .servo import Servo, make_pwm, sysfs_location


def pick_unit(cfg, key: str):
    for u in cfg.units:
        if key in (str(u.number), u.name):
            return u
    nums = '・'.join(str(u.number) for u in cfg.units)
    raise SystemExit(f'基が見つからない: {key}（有効な基は {nums}。enabled: false の基は選べない）')


def make_servo(cfg, u, axis: str, backend: str | None):
    sc = getattr(u, axis)
    pwm = make_pwm(backend or cfg.pwm_backend, sc, cfg.pwm_frequency_hz, cfg.pca9685_address, f'{u.name}.{axis}')
    return Servo(sc, pwm, cfg.servo_range_deg, f'{u.name}.{axis}')


def hold(seconds: float | None) -> None:
    """Ctrl-C か指定秒数まで出力を保つ。"""
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    end = time.monotonic() + seconds if seconds else None
    try:
        while not stop and (end is None or time.monotonic() < end):
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass


def cmd_status(cfg, args):
    print(f'DS3218 {cfg.servo_range_deg:.0f}° 版、PWM {cfg.pwm_backend} {cfg.pwm_frequency_hz:.0f} Hz')
    print(f'昇降（ホーン角）{cfg.lift.lo:+.1f}〜{cfg.lift.hi:+.1f}°、原点 {cfg.lift.home:+.1f}°、'
          f'最高 {cfg.lift.vmax:.1f}°/s')
    print(f'ヨー（テーブル角）{cfg.yaw.lo:+.1f}〜{cfg.yaw.hi:+.1f}°、サーボ角 = {cfg.yaw_gear_ratio:+.2f} × テーブル角、'
          f'最高 {cfg.yaw.vmax:.0f}°/s、客側 {cfg.yaw_present_deg:+.0f}°')
    for u in cfg.units:
        print(f'\n{u.number}: {u.name}（{u.product}）')
        for axis in ('lift', 'yaw'):
            sc = getattr(u, axis)
            dev, ch = sysfs_location(sc.gpio)
            print(f'  {axis:4} GPIO{sc.gpio:<2} {dev} ch{ch}  パルス {sc.pulse_min_us:.0f}/{sc.pulse_center_us:.0f}/'
                  f'{sc.pulse_max_us:.0f} µs{"  反転" if sc.invert else ""}')
        print(f'  重量 DT GPIO{u.scale.dt_gpio}  係数 {u.scale.counts_per_g} /g  しきい値 {u.scale.present_g} g')
        print(f'  音声 {u.sound.file}（{"あり" if u.sound.file and os.path.exists(u.sound.file) else "なし"}）'
              f' 音量 {u.sound.volume} {u.sound.channel}')
    print(f'\n姿勢ファイル {cfg.state_file}')


def cmd_servo(cfg, args):
    u = pick_unit(cfg, args.unit)
    s = make_servo(cfg, u, args.axis, args.backend)
    if args.us is not None:
        us = s.set_pulse_us(args.us)
        what = f'{us:.0f} µs'
    elif args.servo_angle is not None:
        us = s.set_angle(args.servo_angle)
        what = f'サーボ角 {s.angle:+.1f}° = {us:.0f} µs'
    else:
        lim = cfg.lift if args.axis == 'lift' else cfg.yaw
        ratio = 1.0 if args.axis == 'lift' else cfg.yaw_gear_ratio
        logical = lim.clamp(args.angle)
        us = s.set_angle(ratio * logical)
        what = f'論理角 {logical:+.1f}°（サーボ角 {s.angle:+.1f}°）= {us:.0f} µs'
        if logical != args.angle:
            what += f'  ※ {args.angle:+.1f}° は可動範囲外なので丸めた'
    print(f'{u.name} {args.axis}: {what}。Ctrl-C で PWM を止める')
    hold(args.hold)
    s.close()
    from .controller import forget_pose
    forget_pose(cfg.state_file, u.name)  # 加減速なしで動かしたので、次の起動は原点から


def cmd_move(cfg, args):
    """加減速つきで 1 軸を動かす。保存した姿勢（なければ原点）から始め、着いた姿勢を保存する。"""
    from .controller import Controller, TICK_HZ
    from .unit import Unit
    from .hx711 import Scale

    u = pick_unit(cfg, args.unit)
    lift = Axis('昇降', cfg.lift, make_servo(cfg, u, 'lift', args.backend))
    yaw = Axis('ヨー', cfg.yaw, make_servo(cfg, u, 'yaw', args.backend), ratio=cfg.yaw_gear_ratio)
    unit = Unit(cfg, u, lift, yaw, Scale(0, 1, 0), player=None)
    unit.use_scale = False
    ctl = Controller(cfg, [unit], None, None)
    pose = ctl.pose.get(u.name) or {}
    lift.start_at(pose.get('lift') if pose.get('lift') is not None else cfg.lift.home)
    yaw.start_at(pose.get('yaw') if pose.get('yaw') is not None else 0.0)
    if args.axis == 'lift' and not unit.yaw_front:
        sys.exit(f'ヨーが正面にない（{yaw.pos:+.1f}°）ので昇降できない。先に move {args.unit} yaw 0')
    if args.axis == 'yaw' and args.angle and lift.pos != unit.lift_top:
        print(f'注意：上昇していない状態（昇降 {lift.pos:+.1f}°）で旋回する')
    ax = lift if args.axis == 'lift' else yaw
    ax.move_to(args.angle, args.speed)
    print(f'{u.name} {args.axis}: {ax.pos:+.1f}° → {ax.target:+.1f}°')
    dt = 1.0 / TICK_HZ
    while ax.moving:
        ax.update(dt)
        time.sleep(dt)
    ctl.save_pose()
    print(f'着いた（{ax.pos:+.1f}°）。Ctrl-C で PWM を止める')
    hold(args.hold)
    lift.servo.close()
    yaw.servo.close()


def open_bus(cfg):
    from .hx711 import Gpio, HX711Bus
    if not cfg.hx711_enabled:
        print('※ config では重量センサーを使わない設定（hx711.enabled: false）。確認のために読む')
    gpio = Gpio()
    return gpio, HX711Bus(gpio, cfg.hx711_sck_gpio, [u.scale.dt_gpio for u in cfg.units])


def cmd_scale(cfg, args):
    from .hx711 import Scale
    gpio, bus = open_bus(cfg)
    scales = [Scale(u.scale.offset, u.scale.counts_per_g, u.scale.present_g, cfg.average_window) for u in cfg.units]
    end = time.monotonic() + args.seconds if args.seconds else None
    try:
        while end is None or time.monotonic() < end:
            vals = bus.read(timeout=1.0)
            if vals is None:
                print('HX711 が応答しない（配線と 3.3 V を確認）')
                continue
            cols = []
            for u, sc, raw in zip(cfg.units, scales, vals):
                sc.add_raw(raw)
                cols.append(f'{u.name} 生値 {raw:>9d} 平均 {sc.mean_raw:>11.0f} {sc.grams:>8.1f} g'
                            f' {"あり" if sc.present else "なし"}')
            print(' | '.join(cols), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        gpio.close()


def average_raw(bus, index: int, n: int) -> float:
    vals = []
    while len(vals) < n:
        v = bus.read(timeout=1.0)
        if v is None:
            raise SystemExit('HX711 が応答しない')
        vals.append(v[index])
    vals.sort()
    mid = vals[n // 4: n - n // 4] or vals  # 上下 1/4 を捨てて平均
    return sum(mid) / len(mid)


def cmd_scale_cal(cfg, args):
    u = pick_unit(cfg, args.unit)
    gpio, bus = open_bus(cfg)
    try:
        input(f'{u.name}: 台を空にして Enter')
        offset = average_raw(bus, u.index, args.samples)
        input(f'{args.known:g} g の重りを載せて Enter')
        loaded = average_raw(bus, u.index, args.samples)
    finally:
        gpio.close()
    if abs(loaded - offset) < 1:
        sys.exit('重りを載せても値が変わらない（配線を確認）')
    factor = (loaded - offset) / args.known
    print(f'\nconfig.yaml の {u.name} の scale に書く値:')
    print(f'      offset: {offset:.0f}')
    print(f'      counts_per_g: {factor:.3f}')


def cmd_play(cfg, args):
    from .audio import Player
    u = pick_unit(cfg, args.unit)
    p = Player(cfg.audio_device, cfg.speech_fallback_s)
    p.play(u.sound.file, u.sound.volume, u.sound.channel)
    while p.playing:
        time.sleep(0.05)


class _WavTimedPlayer:
    """WAV の長さだけ「再生中」になる（計算だけで流すとき用）。"""

    def __init__(self, fallback):
        self.left, self.fallback = 0.0, fallback

    def play(self, path, volume=1.0, channel='both'):
        import wave
        try:
            with wave.open(path) as w:
                self.left = w.getnframes() / w.getframerate()
        except (OSError, wave.Error, TypeError):
            print(f'  ※ 声がない: {path}（{self.fallback:g} 秒として数える）')
            self.left = self.fallback

    def step(self, dt):
        self.left = max(0.0, self.left - dt)

    @property
    def playing(self):
        return self.left > 0

    def stop(self):
        self.left = 0.0


def pick_program(cfg, ctl, which: str | None) -> None:
    """--program（番号か名前）で台本を選ぶ。なければリモコンで選んだ台本（settings.json）。"""
    if which is None:
        return
    names = [p.name for p in cfg.programs]
    if which.isdigit() and 1 <= int(which) <= len(names):
        ctl.program_i = int(which) - 1
    elif which in names:
        ctl.program_i = names.index(which)
    else:
        sys.exit('台本は ' + ' / '.join(f'{i + 1}={n}' for i, n in enumerate(names)))


def explain_duo_preview(cfg, program: str | None = None, swap: bool | None = None) -> None:
    """2 基で台本を、サーボを動かさずに計算だけで流す。"""
    from .controller import Controller, build_units
    from .unit import State
    if not cfg.programs or len(cfg.units) < 2:
        sys.exit('2 基の台本には sequence.programs と、有効な基が 2 つ要る（units の enabled）')
    player = _WavTimedPlayer(cfg.speech_fallback_s)
    units = build_units(cfg, player, backend='mock')
    ctl = Controller(cfg, units, None, player)
    ctl.paused = True
    pick_program(cfg, ctl, program)
    if swap is not None:
        ctl.settings['swap'] = swap
    print(f'台本：{ctl.program.name}' + ('（キャラ入れ替え）' if ctl.swap else ''))
    for u in units:
        u.lift.start_at(cfg.lift.home)
        u.yaw.start_at(0.0)
        u._set(State.IDLE)
    if not ctl._begin_duo():
        sys.exit('掛け合いを始められない')
    dt, t, last = 0.02, 0.0, {}
    while any(u.busy for u in units) and t < 900:
        ctl.tick(dt)
        t += dt
        for u in units:
            if u.progress and u.progress != last.get(u.name):
                last[u.name] = u.progress
                print(f'{t:6.1f} s  {u.name}  {u.progress}')
    print(f'全体 {t:.1f} 秒（声 {ctl.program.voices}）')


def cmd_explain(cfg, args):
    """台本の説明を、サーボを動かさずに計算だけで流し、文ごとの開始時刻と全体の長さを出す。"""
    if args.unit == 'duo':
        return explain_duo_preview(cfg, args.program, True if args.swap else None)
    import wave
    from .axis import Axis
    from .explain import build_steps, load_plan
    from .hx711 import Scale
    from .servo import MockPwm, Servo
    from .unit import Unit
    u = pick_unit(cfg, args.unit)
    if u.explain is None:
        sys.exit(f'{u.name} に explain（台本と声）の設定がない')
    plan = load_plan(u.explain.plan)
    from . import settings as settings_mod
    st = settings_mod.load(settings_mod.path_for(cfg))
    amp, tempo = settings_mod.effective(cfg, st)
    steps = build_steps(cfg, plan, u.explain.voices, u.explain.dance, amp=amp, tempo=tempo)

    class TimedPlayer:   # WAV の長さだけ「再生中」になる
        def __init__(self):
            self.left = 0.0

        def play(self, path, volume=1.0, channel='both'):
            try:
                with wave.open(path) as w:
                    self.left = w.getnframes() / w.getframerate()
            except (OSError, wave.Error):
                print(f'  ※ 声がない: {path}（{cfg.speech_fallback_s:g} 秒として数える）')
                self.left = cfg.speech_fallback_s

        @property
        def playing(self):
            return self.left > 0

        def stop(self):
            self.left = 0.0

    player = TimedPlayer()
    mk = lambda sc: Servo(sc, MockPwm(), cfg.servo_range_deg)
    unit = Unit(cfg, u, Axis('昇降', cfg.lift, mk(u.lift)), Axis('ヨー', cfg.yaw, mk(u.yaw), ratio=cfg.yaw_gear_ratio),
                Scale(0, 1, 0), player)
    unit.use_scale = False
    unit.lift.accel_scale = unit.yaw.accel_scale = tempo
    unit.lift.start_at(cfg.lift.home)
    unit.yaw.start_at(0.0)
    unit.state = unit.state.IDLE
    unit.start_explain(steps)
    dt, t, last = 0.02, 0.0, ''
    while unit.busy and t < 600:
        unit.tick(dt)
        player.left = max(0.0, player.left - dt)
        t += dt
        if unit.progress and unit.progress != last:
            last = unit.progress
            print(f'{t:6.1f} s  {unit.progress}')
    print(f'全体 {t:.1f} 秒（{len(plan["sentences"])} 文、声 {u.explain.voices}）')


def cmd_sync_check(cfg, args):
    from .synccheck import report, run_check
    report(run_check(cfg, pick_unit(cfg, args.unit)))


def cmd_run(cfg, args):
    from .controller import run
    run(cfg, no_scale=args.no_scale, no_remote=args.no_remote)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m srr', description='商品自己推薦ロボットの実機制御')
    ap.add_argument('--config', help='設定ファイル（既定 robot/config.yaml、環境変数 SRR_CONFIG）')
    ap.add_argument('--backend', choices=['sysfs', 'pca9685', 'mock'], help='PWM の出し方を一時的に変える')
    ap.add_argument('-v', '--verbose', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('status', help='設定と PWM の出力先を表示')

    p = sub.add_parser('servo', help='1 本だけ動かす（加減速なし。ホーンを付ける前の確認・校正用）')
    p.add_argument('unit')
    p.add_argument('axis', choices=['lift', 'yaw'])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--angle', type=float, help='論理角（昇降：ホーン角、ヨー：テーブル角）。可動範囲に丸める')
    g.add_argument('--servo-angle', type=float, help='サーボ角（中心 0°、±range/2 まで）')
    g.add_argument('--us', type=float, help='パルス幅 µs（400〜2600 に丸める）')
    p.add_argument('--hold', type=float, help='この秒数で PWM を止める（既定は Ctrl-C まで）')

    p = sub.add_parser('move', help='加減速つきで 1 軸を動かす（機構に組んだ後）')
    p.add_argument('unit')
    p.add_argument('axis', choices=['lift', 'yaw'])
    p.add_argument('angle', type=float, help='論理角（昇降：ホーン角、ヨー：テーブル角）')
    p.add_argument('--speed', type=float, default=0.5, help='最高速度に対する倍率（既定 0.5）')
    p.add_argument('--hold', type=float)

    p = sub.add_parser('scale', help='HX711 の生値とグラムを表示')
    p.add_argument('--seconds', type=float)

    p = sub.add_parser('scale-cal', help='既知の重さで係数を校正')
    p.add_argument('unit')
    p.add_argument('--known', type=float, required=True, help='重りの重さ g')
    p.add_argument('--samples', type=int, default=40)

    p = sub.add_parser('play', help='その基の音声を鳴らす')
    p.add_argument('unit')

    p = sub.add_parser('explain', help='台本の説明を計算だけで流し、文ごとの時刻と全体の長さを出す（duo で 2 基の台本）')
    p.add_argument('unit')
    p.add_argument('--program', help='duo で流す台本（sequence.programs の番号か名前。既定はリモコンで選んだ台本）')
    p.add_argument('--swap', action='store_true', help='duo でキャラの配置を入れ替える（既定はリモコンの設定）')

    p = sub.add_parser('sync-check', help='実機で説明を 1 回流し、動きと声のずれを測る（サービスは止めておく）')
    p.add_argument('unit')

    p = sub.add_parser('run', help='本番の動作')
    p.add_argument('--no-scale', action='store_true', help='ロードセルなしで動かす（重量を見ない）')
    p.add_argument('--no-remote', action='store_true', help='リモコンを使わない（config の remote を無視して自動で動かす）')

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger('bleak').setLevel(logging.INFO)  # -v でも BLE の D-Bus のやりとりまでは出さない
    cfg = config_mod.load(args.config)
    if args.backend:
        cfg.pwm_backend = args.backend
    {'status': cmd_status, 'servo': cmd_servo, 'move': cmd_move, 'scale': cmd_scale,
     'scale-cal': cmd_scale_cal, 'play': cmd_play, 'explain': cmd_explain, 'sync-check': cmd_sync_check, 'run': cmd_run}[args.cmd](cfg, args)
