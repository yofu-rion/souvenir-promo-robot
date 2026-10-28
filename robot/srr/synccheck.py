"""実機で台本の説明を 1 回流し、動きと声のずれを測る（python -m srr sync-check 1）。

声の時刻は ALSA の記録（/proc/asound/…/status の trigger_time と hw_ptr）から取る。trigger_time は
time.monotonic と同じ時計なので、動きの時刻とそのまま比べられる。測るもの:
  - 遅れ: play() を呼んでから、実際に音が流れ始めるまで
  - 時計のずれ: 動きの時計（talk_t、周期 dt の積み上げ）と、実際に送り出した声の位置の差
  - 着地のずれ: 狙う時刻のある動き（うなずきの底など）が着いた時刻と、狙った句の終わりの差（声の時間で）
記録は ~/.local/state/srr-robot/sync-*.json に残す。
"""
from __future__ import annotations

import json
import os
import statistics
import time

from .audio import Player
from .axis import Axis
from .explain import build_steps, load_plan
from .hx711 import Scale
from .servo import Servo, make_pwm
from .unit import State, Unit

TICK_HZ = 50


def run_check(cfg, ucfg, out_dir: str | None = None) -> dict:
    if ucfg.explain is None:
        raise SystemExit(f'{ucfg.name} に explain（台本と声）の設定がない')
    player = Player(cfg.audio_device, cfg.speech_fallback_s)
    if player.proc_dir is None:
        raise SystemExit(f'audio.device（{cfg.audio_device}）から ALSA の記録を読めない。plughw:CARD=… にする')

    def servo(sc, label):
        return Servo(sc, make_pwm(cfg.pwm_backend, sc, cfg.pwm_frequency_hz, cfg.pca9685_address, label),
                     cfg.servo_range_deg, label)
    unit = Unit(cfg, ucfg, Axis('昇降', cfg.lift, servo(ucfg.lift, 'lift')),
                Axis('ヨー', cfg.yaw, servo(ucfg.yaw, 'yaw'), ratio=cfg.yaw_gear_ratio), Scale(0, 1, 0), player)
    unit.use_scale = False
    plan = load_plan(ucfg.explain.plan)
    from . import settings as settings_mod
    st = settings_mod.load(settings_mod.path_for(cfg))
    amp, tempo = settings_mod.effective(cfg, st)
    steps = build_steps(cfg, plan, ucfg.explain.voices, ucfg.explain.dance, amp=amp, tempo=tempo)
    unit.lift.accel_scale = unit.yaw.accel_scale = tempo   # 組み立ての計算と同じ加速度で動かす

    dt = 1.0 / TICK_HZ
    sentences: list[dict] = []
    cur: dict | None = None
    moving: dict[str, dict] = {}   # 軸ごとに、今動いている狙いつきの動き
    try:
        unit.begin_homing(None, cfg.homing_speed_scale)   # 姿勢が分からないので原点からゆっくり
        nxt = time.monotonic()
        started = False
        while True:
            unit.tick(dt)
            now = time.monotonic()
            if unit.state == State.IDLE and not started:
                unit.start_explain(steps)
                started = True
            elif started and unit.state == State.IDLE:
                break
            st = unit.step
            if st is not None and st.kind == 'talk':
                if cur is None or cur['label'] != st.label:
                    cur = {'label': st.label, 'called': None, 'trigger': None, 'samples': [], 'events': [],
                           'cues': [list(c) for c in st.cues], 'fired_n': 0}
                    sentences.append(cur)
                    moving.clear()
                if unit.talk_phase == 'speak':
                    if cur['called'] is None:
                        cur['called'] = player.called_at
                    o = player.output_status()
                    if o is not None:
                        cur['trigger'] = o['trigger']
                        cur['samples'].append((now, unit.talk_t, o['pos']))
                    # 取り出された動きを順番で特定し、狙いつきなら覚えて、着いたら記録する
                    order = sorted(st.cues, key=lambda c: c[0])
                    while cur['fired_n'] < len(order) - len(unit.talk_cues):
                        c = order[cur['fired_n']]
                        cur['fired_n'] += 1
                        if len(c) > 4:
                            moving[c[1]] = {'axis': c[1], 'aim': c[4], 'cue_t': c[0], 'fired': now, 'target': c[2]}
                for axis, ev in list(moving.items()):
                    ax = getattr(unit, axis)
                    if not ax.moving and ax.pos == ev['target'] or ax.target != ev['target']:
                        ev['reached'] = now if not ax.moving else None
                        cur['events'].append(ev)
                        del moving[axis]
            nxt += dt
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()
    finally:
        player.stop()
        for ax in (unit.lift, unit.yaw):
            ax.servo.close()
    result = analyze(sentences)
    out_dir = out_dir or os.path.dirname(cfg.state_file)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime('sync-%Y%m%d-%H%M%S.json'))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'sentences': sentences, 'result': result}, f, ensure_ascii=False)
    result['log'] = path
    return result


def analyze(sentences: list[dict]) -> dict:
    rows, offsets, latencies, drifts = [], [], [], []
    for s in sentences:
        if s['trigger'] is None or s['called'] is None:
            rows.append({'label': s['label'], 'note': '声の記録が取れなかった'})
            continue
        latency = s['trigger'] - s['called']
        # 動きの時計と実際の声の位置の差（文の最後の記録で）
        drift = s['samples'][-1][1] - s['samples'][-1][2] if s['samples'] else None
        lands = []
        for ev in s['events']:
            if ev.get('reached') is None:
                continue
            audio_t = ev['reached'] - s['trigger']   # 声の時間（流れ始めからの秒）で、着いた時刻
            lands.append({'axis': ev['axis'], 'aim': ev['aim'], 'off': audio_t - ev['aim']})
        rows.append({'label': s['label'], 'latency': latency, 'drift': drift, 'lands': lands})
        latencies.append(latency)
        if drift is not None:
            drifts.append(drift)
        offsets += [l['off'] for l in lands]

    def stat(xs):
        return None if not xs else {'mean': statistics.fmean(xs), 'min': min(xs), 'max': max(xs), 'n': len(xs)}
    return {'rows': rows, 'latency': stat(latencies), 'drift': stat(drifts), 'land': stat(offsets)}


def report(result: dict) -> None:
    ms = lambda x: '—' if x is None else f'{x * 1000:+.0f}'
    print(f'{"文":<26} {"音の遅れ":>8} {"時計のずれ":>10}  着地のずれ（ms、＋は遅い）')
    for r in result['rows']:
        if 'note' in r:
            print(f'{r["label"][:24]:<26} {r["note"]}')
            continue
        lands = ' '.join(f'{"昇" if l["axis"] == "lift" else "ヨ"}{ms(l["off"])}' for l in r['lands'])
        print(f'{r["label"][:24]:<26} {ms(r["latency"]):>8} {ms(r["drift"]):>10}  {lands}')
    for key, name in (('latency', '音の遅れ'), ('drift', '時計のずれ'), ('land', '着地のずれ')):
        st = result[key]
        if st:
            print(f'{name}: 平均 {ms(st["mean"])} ms（{ms(st["min"])}〜{ms(st["max"])}、{st["n"]} 件）')
    print(f'記録: {result.get("log")}')
