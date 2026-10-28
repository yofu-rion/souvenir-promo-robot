"""台本による説明動作。文ごとの感情（tools/script-plan.mjs が sim と同じ規則で推定）と、声の句の区切りの時刻
（tools/make-voice.py --plan が 01.json … に書く）から、動きと声の手順を作る。

本家の自己推薦ロボットの動画（docs/research/srr-video-analysis.md）と、話し手の頭部動作の研究
（docs/research/explain-motion.md）に合わせる。
  - ポーズからポーズへ：文の頭で向き（客側の 25°・45°・65° など）と高さ（快いほど高く）を素早く変え、
    話している間はそこで止まる。同じ向きのまま小さく揺れ続けない
  - 話し始めに上がり（発話のサイン）、文末の最後の音節でうなずく。読点ではうなずきと向き変えを交互に入れる
  - 形・回すなど見た目の話をする文では、商品を回して横を見せる
  - 喜びの文の後に、音楽に合わせた短いダンス（回転と跳ねを拍ごとに交互に）を入れ、決めポーズで終える
  - しぐさ：呼びかけは話す前に大きく 2 回跳ねる、お礼は文末で深くお辞儀、喜びは文末で弾む
昇降とヨーは同時に動かさない（要件）。テーブルは 60°/s 以下。動きにかかる時間は速度・加速度の上限から計算し、
狙った句の終わりや拍に着くよう前もって始める。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
import re
import wave

from .unit import Barrier, Step

GAP_S = 0.1               # 文と文の間（テンポのため短く）
MIN_LIFT_CHANGE = 0.05    # 文ごとの高さの違いがこれ（行程の割合）未満なら昇降しない
SIGN_RAISE = 0.15         # 話し始めに上がる量（行程の割合）
NOD_END = 0.25            # 文末のうなずきの深さ（覚醒度で最大 1.4 倍）
NOD_MID = 0.15            # 読点の息継ぎでのうなずき
BOW = 0.55                # お礼のお辞儀の深さ
HOP_JOY = 0.3             # 喜びの文末で弾む高さ
HOP_BECKON = 0.4          # 呼びかけで跳ねる高さ
POSE_STEP_DEG = 15.0      # 文ごとのポーズの向きの幅（正面 ± この角度）
LOOK_DEG = 20.0           # 聞き役が相手の方を向く角度
AWAY_DEG = 25.0           # 不快・照れの文で、外側（相手と反対）へそっぽを向く角度
PRE_LEAD_MAX = 0.5        # ポーズを変え始めてから話し始めるまで（動いている途中で話し出す）
GLANCE_DEG = 15.0         # 読点で向きを変える量
SHOW_DEG = 35.0           # 回して見せるときの振れ幅
SHOW_WORDS = ('回し', 'くるくる', '形', '再現', '見た目', 'ボルト', 'ナット')
SHORT_S = 1.6             # これより短い文は話し始めに上がらない（文末の動きが間に合わない）
SLACK_S = 0.0             # 計算と実際の着地の差を詰める補正（sync-check で確かめる）
DANCE_BPM = 120.0         # tools/make-dance-music.py の既定と合わせる
DANCE_BARS = 4
DANCE_SWING_DEG = 30.0    # ダンスで左右に向きを振る量（客側から）


def tempo_cfg(cfg, tempo: float):
    """速さの倍率をかけた設定（動きにかかる時間の計算用）と、出す速さにかける倍率を返す。

    最高速度は要件の上限（100%）を超えないので min(1, tempo) をかけ、加速度には tempo をそのままかける。
    実機では Axis.accel_scale に tempo を入れ、出す速さに min(1, tempo) をかける（scale_speeds）。
    """
    vs = min(1.0, tempo)
    c = copy.copy(cfg)
    c.lift = dataclasses.replace(cfg.lift, vmax=cfg.lift.vmax * vs, amax=cfg.lift.amax * tempo)
    c.yaw = dataclasses.replace(cfg.yaw, vmax=cfg.yaw.vmax * vs, amax=cfg.yaw.amax * tempo)
    return c, vs


def scale_speeds(steps: list, vs: float) -> list:
    """手順の速さ（Step.speed、cues と pre の速さ）に vs をかける。"""
    for st in steps:
        st.speed *= vs
        st.cues = [(c[0], c[1], c[2], c[3] * vs) + tuple(c[4:]) for c in st.cues]
        st.pre = [(ax, t, sp * vs) for ax, t, sp in st.pre]
    return steps


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def load_plan(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_timing(wav_path: str, text: str) -> dict:
    """句の区切りの時刻。make-voice.py の JSON がなければ、WAV の長さと句読点の位置から見積もる。"""
    try:
        with open(os.path.splitext(wav_path)[0] + '.json', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        with wave.open(wav_path) as w:
            dur = w.getnframes() / w.getframerate()
    except (OSError, wave.Error):
        dur = max(1.0, len(text) / 5.5)
    body = re.sub(r'\s+', '', text)
    cuts = [m.end() for m in re.finditer(r'[、，,]', body)]
    phrases = [{'end': round(dur * c / len(body), 3), 'pause': 0.3} for c in cuts]
    phrases.append({'end': round(dur * 0.95, 3), 'pause': 0.0})
    return {'duration': dur, 'phrases': phrases}


class Heights:
    """昇降の論理角を「行程の割合」（0 = 最下、1 = 最上）で扱う。"""

    def __init__(self, cfg):
        self.lim = cfg.lift
        self.home = cfg.lift.home
        self.top = cfg.lift.clamp(cfg.raw['lift']['top_deg'])

    def at(self, frac: float) -> float:
        return self.lim.clamp(self.home + (self.top - self.home) * clamp(frac, 0.0, 1.0))


def height_frac(valence: float, gesture: str | None, amp: float = 1.0) -> float:
    # 上にうなずき・弾みの余裕を残すため、文ごとの高さは行程の 10〜70% に収める
    # amp（動きの大きさ）を上げるほど、快い文と不快な文の高さの差が開く
    return 0.75 if gesture == 'surprise' else clamp(0.35 + 0.5 * (valence - 0.2) * amp, 0.1, 0.75)


def height_for(cfg, valence: float, gesture: str | None) -> float:
    return Heights(cfg).at(height_frac(valence, gesture))


def travel(dist: float, vmax: float, amax: float, dt: float = 0.02) -> float:
    """dist 動くのにかかる秒数。axis.py の Axis.update と同じ規則で 1 周期ずつ進めて数える。"""
    target, pos, vel, t = abs(dist), 0.0, 0.0, 0.0
    while pos < target and t < 10.0:
        v_des = min(vmax, math.sqrt(2 * amax * (target - pos)))
        dv = amax * dt
        vel += max(-dv, min(dv, v_des - vel))
        pos = min(target, pos + vel * dt)
        t += dt
    return t


class Timeline:
    """動きを、かかる時間を見積もって重ならないように並べる。動きは「休みの姿勢から出て、着いたら戻る」形。

    必ず入れる動きを先に置き、省いてよい動き（optional）は空いている時間に収まるときだけ置く。
    """

    def __init__(self, cfg, H: Heights, rest_yaw: float, rest_frac: float, t_free: float = 0.0):
        self.cfg, self.H, self.rest_yaw, self.rest_frac = cfg, H, rest_yaw, rest_frac
        self.t_free, self.busy, self.cues = t_free, [], []

    def lift_time(self, f0, f1, sp):
        return travel(self.H.at(f1) - self.H.at(f0), self.cfg.lift.vmax * sp, self.cfg.lift.amax) + SLACK_S

    def yaw_time(self, y0, y1, sp):
        return travel(y1 - y0, self.cfg.yaw.vmax * sp, self.cfg.yaw.amax) + SLACK_S

    def free(self, t0, t1):
        return t0 >= self.t_free and all(t1 + 0.05 <= b0 or t0 >= b1 + 0.05 for b0, b1 in self.busy)

    def lift(self, aim, peak, sp, optional=False, back=None):
        """休みの高さから peak へ、aim に着いたら back（既定は休みの高さ）へ戻る。"""
        back = self.rest_frac if back is None else back
        out, ret = self.lift_time(self.rest_frac, peak, sp), self.lift_time(peak, back, sp)
        start = aim - out
        if optional and not self.free(start, aim + ret):
            return False
        start = max(start, self.t_free)
        self.busy.append((start, start + out + ret))
        self.cues += [(start, 'lift', self.H.at(peak), sp, aim), (start + 0.01, 'lift', self.H.at(back), sp)]
        return True

    def yaw(self, aim, to, sp, optional=False, hold=0.0):
        """休みの向きから to へ、aim に着いたら hold 秒止まってから休みの向きへ戻る。"""
        to = self.cfg.yaw.clamp(to)
        out, ret = self.yaw_time(self.rest_yaw, to, sp), self.yaw_time(to, self.rest_yaw, sp)
        start = aim - out
        if optional and not self.free(start, aim + hold + ret):
            return False
        start = max(start, self.t_free)
        self.busy.append((start, start + out + hold + ret))
        self.cues += [(start, 'yaw', to, sp, aim), (start + out + hold, 'yaw', self.rest_yaw, sp)]
        return True

    def sorted(self):
        return sorted(self.cues, key=lambda c: c[0])


def pose_pre(cfg, H: Heights, yaw0: float, frac0: float, yaw1: float, frac1: float) -> tuple[list, float, float]:
    """話す前のポーズの変更（向き → 高さ）。(pre の列, 話し始めるまでの秒, 話し始めた後も動いている秒) を返す。"""
    pre, total = [], 0.0
    if abs(yaw1 - yaw0) > 0.5:
        pre.append(('yaw', cfg.yaw.clamp(yaw1), 1.0))
        total += travel(yaw1 - yaw0, cfg.yaw.vmax, cfg.yaw.amax)
    if abs(frac1 - frac0) >= MIN_LIFT_CHANGE:
        pre.append(('lift', H.at(frac1), 1.0))
        total += travel(H.at(frac1) - H.at(frac0), cfg.lift.vmax, cfg.lift.amax)
    lead = min(PRE_LEAD_MAX, total * 0.5)
    return pre, lead, max(0.0, total - lead)


def talk_cues(cfg, H: Heights, base_frac: float, base_yaw: float, timing: dict, v: float, a: float,
              gesture: str | None, glance_sign: int, show: bool = False, busy: float = 0.0,
              amp: float = 1.0) -> list:
    """1 文の声に合わせた動き。(開始時刻, 軸, 目標, 速さ[, 狙う時刻]) の列（時刻は声の始まりから）。

    狙う時刻は、その動きを着かせたい句の終わり（うなずきの底・お辞儀の底・弾みの頂点・向き変え）。sync-check で使う。
    busy は、話し始めた後もポーズの変更で軸が動いている時間（その間は次の動きを入れない）。
    amp は動きの大きさの倍率（リモコンの「設定」で変える）。
    """
    lively = (a + 1) / 2                         # 0（落ち着き）〜 1（興奮）
    calm = 0.7 if v < 0 else 1.0                 # 不快な文は小さく遅く
    speed = clamp((0.75 + 0.25 * lively) * calm, 0.4, 1.0)
    phrases, last = timing['phrases'], timing['phrases'][-1]
    short = timing['duration'] < SHORT_S         # 短い文は話し始めに上がらず、すぐ文末の動きにする
    talk = base_frac if short else base_frac + SIGN_RAISE * amp
    tl = Timeline(cfg, H, base_yaw, talk, t_free=busy)
    if not short:
        tl.cues.append((busy + 0.05, 'lift', H.at(talk), 0.4))
        tl.t_free = busy + 0.05 + tl.lift_time(base_frac, talk, 0.4)
    # 文末：お礼は深いお辞儀、喜びは弾む、それ以外はうなずく（必ず入れる）
    if gesture == 'thanks':
        tl.lift(last['end'], base_frac - BOW * amp, 1.0 if short else 0.7, back=base_frac)
    elif gesture == 'joy':
        tl.lift(last['end'], talk + HOP_JOY * amp, 1.0)
    else:
        tl.lift(last['end'], talk - NOD_END * amp * (0.9 + 0.5 * lively) * calm, speed)
    mids = phrases[:-1]
    # 見た目の話：句の区切りで、右・左と回して横を見せる
    if show:
        for k, p in enumerate(m for m in mids if m['end'] > 0.8):
            if k >= 2:
                break
            tl.yaw(p['end'], base_yaw + (SHOW_DEG if k == 0 else -SHOW_DEG) * amp, 0.9, optional=True, hold=0.3)
    # 読点の息継ぎ：うなずきと小さな向き変えを交互に入れて、同じ動きを続けない
    turn = glance_sign
    for i, p in enumerate(m for m in mids if m['pause'] >= 0.1):
        depth = NOD_MID * amp * (0.8 + 0.5 * lively) * calm
        if i % 2 == 0:
            tl.lift(p['end'], talk - depth, speed, optional=True)
        else:
            tl.yaw(p['end'], base_yaw + turn * GLANCE_DEG * amp, 0.7, optional=True, hold=0.4)
            turn = -turn
    # 長い文で、まだ何も入っていなければ、弱い区切りで 1 回向きを変える（首傾げの代わり）
    if timing['duration'] >= 4.5 and not any(c[1] == 'yaw' for c in tl.cues):
        for p in sorted((m for m in mids if m['pause'] < 0.1), key=lambda m: abs(m['end'] - timing['duration'] / 2)):
            if tl.yaw(p['end'], base_yaw + glance_sign * GLANCE_DEG * amp, 0.6, optional=True, hold=0.5):
                break
    return tl.sorted()


def dance_cues(cfg, H: Heights, base_yaw: float, base_frac: float, bpm: float = DANCE_BPM,
               bars: int = DANCE_BARS, mirror: bool = False, amp: float = 1.0) -> list:
    """音楽の拍に合わせた短いダンス。右へ振る → 沈んで跳ねる → 左へ振る → 沈んで跳ねる…とつなぎ、決めポーズで終える。

    回転と昇降は同時に動かさないので 1 つずつつなぎ、どの動きも「間に合ういちばん近い拍」に着くよう並べる。
    最後は正面（客側）に戻り、決めの和音（最後の小節の次の拍）で高い位置に着く。
    mirror なら左右を逆に振る（2 基で左右対称に踊る）。
    """
    beat = 60.0 / bpm
    end = bars * 4 * beat
    yaw, frac, t_free = base_yaw, base_frac, 0.0
    cues = []

    def lift_t(f0, f1):
        return travel(H.at(f1) - H.at(f0), cfg.lift.vmax, cfg.lift.amax) + SLACK_S

    def yaw_t(y0, y1):
        return travel(y1 - y0, cfg.yaw.vmax, cfg.yaw.amax) + SLACK_S

    def on_beat(t):
        return math.ceil(t / beat - 1e-6) * beat

    def add(axis, target, dur):
        nonlocal t_free
        aim = on_beat(t_free + dur)
        cues.append((aim - dur, axis, target, 1.0, aim))
        t_free = aim
        return aim

    # 決めポーズに要る時間（中央へ戻る＋高く上がる）を残して、振りと跳ねを繰り返す
    swing = DANCE_SWING_DEG * amp
    finale = yaw_t(base_yaw + swing, base_yaw) + lift_t(base_frac - 0.25 * amp, 0.95) + beat
    side = -1 if mirror else 1
    while True:
        to = cfg.yaw.clamp(base_yaw + side * swing)
        dip, hop = base_frac - 0.25 * amp, base_frac + 0.2 * amp
        need = yaw_t(yaw, to) + lift_t(frac, dip) + lift_t(dip, hop)
        if on_beat(t_free + need) + finale > end:
            break
        add('yaw', to, yaw_t(yaw, to))
        yaw = to
        add('lift', H.at(dip), lift_t(frac, dip))
        add('lift', H.at(hop), lift_t(dip, hop))
        frac = hop
        side = -side
    if yaw != base_yaw:
        add('yaw', base_yaw, yaw_t(yaw, base_yaw))
    dur = lift_t(frac, 0.95)
    cues.append((max(t_free, end - dur), 'lift', H.at(0.95), 1.0, end))
    return cues


def _base_yaw(present: float, partner_side: int, g, v, pose_i: int, solo_pose: bool, amp: float = 1.0) -> float:
    """その文で向く角度。お客さんは正面（present）。不快・照れは外側へ、お願い・お礼・呼びかけは正対。"""
    if v < 0 or g in ('sad', 'shy', 'sulk'):
        return present - partner_side * AWAY_DEG * amp
    if g in ('beg', 'thanks', 'beckon') or not solo_pose:
        return present
    return present + (0.0, POSE_STEP_DEG, -POSE_STEP_DEG)[pose_i % 3] * amp


def build_steps(cfg, plan: dict, voices_dir: str, dance: str | None = None, amp: float = 1.0,
                tempo: float = 1.0) -> list[Step]:
    """1 基で台本を説明する手順。amp は動きの大きさ、tempo は速さの倍率（リモコンの「設定」）。"""
    cfg, vs = tempo_cfg(cfg, tempo)
    H = Heights(cfg)
    present = cfg.yaw_present_deg
    sentences = plan['sentences']
    steps: list[Step] = []
    cur_yaw, cur_frac = 0.0, 0.0
    glance, pose_i, danced, beckoned = 1, 0, False, False
    for s in sentences:
        v, a, g = float(s['valence']), float(s['arousal']), s.get('gesture')
        if g == 'beckon':   # 呼びかけのしぐさは最初の 1 回だけ
            g = None if beckoned else g
            beckoned = True
        frac = height_frac(v, g, amp)
        base = cfg.yaw.clamp(_base_yaw(present, 1, g, v, pose_i, True, amp))
        if not (v < 0 or g in ('sad', 'shy', 'sulk', 'beg', 'thanks', 'beckon')):
            pose_i += 1
        if g == 'beckon':     # 呼びかけ：大きく振り向いてから正対して、大きく 2 回跳ねる
            hop = H.at(frac + HOP_BECKON * amp)
            steps += [Step('yaw', cfg.yaw.clamp(present + 30.0 * amp)), Step('yaw', base), Step('lift', H.at(frac)),
                      Step('lift', hop), Step('lift', H.at(frac)), Step('lift', hop), Step('lift', H.at(frac))]
            cur_yaw, cur_frac = base, frac
        pre, lead, busy = pose_pre(cfg, H, cur_yaw, cur_frac, base, frac)
        if pre and pre[-1][0] == 'lift' or abs(frac - cur_frac) >= MIN_LIFT_CHANGE:
            cur_frac = frac
        cur_yaw = base
        wav = os.path.join(voices_dir, s['voice'])
        timing = load_timing(wav, s['text'])
        show = any(w in s['text'] for w in SHOW_WORDS)
        steps.append(Step('talk', base, file=wav, label=f'{s["n"]}/{len(sentences)} {s["text"]}',
                          cues=talk_cues(cfg, H, cur_frac, base, timing, v, a, g, glance, show, busy=busy, amp=amp),
                          end_lift=H.at(cur_frac), pre=pre, pre_lead=lead))
        glance = -glance
        steps.append(Step('wait', GAP_S))
        if g == 'joy' and dance and not danced:   # 喜びの文の後に、音楽に合わせて踊る
            danced = True
            mid = 0.45
            steps += [Step('yaw', present), Step('lift', H.at(mid))]
            steps.append(Step('talk', present, file=dance, label=f'{s["n"]}/{len(sentences)} ♪ ダンス',
                              cues=dance_cues(cfg, H, present, mid, amp=amp), end_lift=H.at(0.95)))
            cur_yaw, cur_frac = present, 0.95
            steps.append(Step('wait', 0.2))
    if cur_yaw != 0.0:
        steps.append(Step('yaw', 0.0))
    steps.append(Step('lift', H.home))
    return scale_speeds(steps, vs)


LISTEN_NOD = 0.12         # 聞き役が相手の文末で返す相づちのうなずき


class _Pose:
    """build_duo_steps で基ごとに持つ、今の姿勢とポーズの順番。"""

    def __init__(self, cfg, ucfg):
        self.ucfg, self.name = ucfg, ucfg.name
        self.present = cfg.yaw.clamp(ucfg.present_deg if ucfg.present_deg is not None else cfg.yaw_present_deg)
        self.side = ucfg.partner_side   # +1: ヨーの＋が相手の方、−1: −が相手の方
        self.yaw, self.frac, self.pose_i, self.glance = 0.0, 0.0, 0, 1
        self.steps: list[Step] = []

    def go(self, cfg, H, yaw=None, frac=None, speed=1.0):
        """新しいポーズへ（向き → 高さの順。変わらない軸は動かさない）。"""
        if yaw is not None and self.yaw != yaw:
            self.steps.append(Step('yaw', cfg.yaw.clamp(yaw)))
            self.yaw = cfg.yaw.clamp(yaw)
        if frac is not None and abs(frac - self.frac) >= MIN_LIFT_CHANGE:
            self.steps.append(Step('lift', H.at(frac), speed))
            self.frac = frac


def build_duo_steps(cfg, units, plan: dict, voices_dir: str, dance: str | None = None, amp: float = 1.0,
                    tempo: float = 1.0, swap: bool = False) -> dict:
    """2 基の掛け合い。基ごとの手順を {基の名前: [Step…]} で返す。

    お客さんは正面にいる。文ごとに待ち合わせ（sync）を置き、話す基は正面のまわりでポーズを変えながら話し出す
    （動いている途中で話し始めてテンポを上げる）。聞き役は相手の方（partner_side）を少し向いて聞き、相手の文末で
    小さく相づちのうなずきをする。そろって話す文は 2 基とも正面で、1 基目が左右に分けた声を流す。
    最初の喜びの文の後に、2 基で真ん中を挟んで左右対称に踊る（1 基目が音楽を流す）。最後はそろって正面・最下へ。
    swap なら、台本の話者 1 と 2 を入れ替えて、2 基目が話者 1 のせりふを話す（キャラの配置の入れ替え）。
    """
    cfg, vs = tempo_cfg(cfg, tempo)
    H = Heights(cfg)
    by_no = {u.number: _Pose(cfg, u) for u in units}
    poses = list(by_no.values())
    nums = sorted(by_no)
    cast = {k: k for k in nums}          # 台本の話者の番号 → 話す基の番号
    if swap and len(nums) >= 2:
        cast[nums[0]], cast[nums[1]] = nums[1], nums[0]
    role = {v: k for k, v in cast.items()}   # 基の番号 → その基が受け持つ話者の番号
    # そろって話す声の WAV は左＝話者 1、右＝話者 2。話者 1 を受け持つ基のスピーカーが右なら左右を入れ替える
    unison = 'stereo-swap' if by_no[cast[nums[0]]].ucfg.sound.channel == 'right' else 'stereo'
    names = [p.name for p in poses]
    total = len(plan['sentences'])
    danced = False

    def sync():
        bar = Barrier(names)
        for p in poses:
            p.steps.append(Step('sync', barrier=bar))

    beckoned = False
    for s in plan['sentences']:
        v, a, g = float(s['valence']), float(s['arousal']), s.get('gesture')
        if g == 'beckon':   # 呼びかけのしぐさは最初の 1 回だけ（何度も跳ねるとテンポが落ちる）
            g = None if beckoned else g
            beckoned = True
        speakers = [by_no[cast[k]] for k in s.get('speakers', [1]) if k in cast] or poses[:1]
        listeners = [p for p in poses if p not in speakers]
        frac = height_frac(v, g, amp)
        label = f'{s["n"]}/{total} {s["text"]}'
        sync()
        for i, p in enumerate(speakers):
            base = cfg.yaw.clamp(_base_yaw(p.present, p.side, g, v, p.pose_i, len(speakers) == 1, amp))
            if len(speakers) == 1 and not (v < 0 or g in ('sad', 'shy', 'sulk', 'beg', 'thanks', 'beckon')):
                p.pose_i += 1
            if g == 'beckon':   # 呼びかけ：外側へ大きく振り向いてから正対して、大きく 2 回跳ねる
                p.go(cfg, H, yaw=p.present - p.side * 30.0 * amp)
                p.go(cfg, H, yaw=base, frac=frac)
                hop = H.at(frac + HOP_BECKON * amp)
                p.steps += [Step('lift', hop), Step('lift', H.at(frac)), Step('lift', hop), Step('lift', H.at(frac))]
            pre, lead, busy = pose_pre(cfg, H, p.yaw, p.frac, base, frac)
            p.yaw = base
            if abs(frac - p.frac) >= MIN_LIFT_CHANGE:
                p.frac = frac
            if len(speakers) == 1:
                wav = os.path.join(voices_dir, s['voice'])
                timing = load_timing(wav, s['text'])
            else:
                part = os.path.join(voices_dir, f'{os.path.splitext(s["voice"])[0]}-{role[p.ucfg.number]}.wav')
                timing = load_timing(part, s['text'])
                wav = os.path.join(voices_dir, s['voice'])
            show = any(w in s['text'] for w in SHOW_WORDS)
            cues = talk_cues(cfg, H, p.frac, base, timing, v, a, g, p.glance, show, busy=busy, amp=amp)
            p.glance = -p.glance
            leader = i == 0
            p.steps.append(Step('talk', base, file=wav if leader else None, label=label, cues=cues,
                                end_lift=H.at(p.frac),
                                channel=unison if len(speakers) > 1 else None,
                                pre=pre, pre_lead=lead))
        if listeners:   # 聞き役：相手の方を少し向いて聞き、相手の文末で小さくうなずく
            timing = load_timing(os.path.join(voices_dir, s['voice']), s['text'])
            for p in listeners:
                look = cfg.yaw.clamp(p.present + p.side * LOOK_DEG * amp)
                cues = []
                t_free = 0.2
                if abs(p.yaw - look) > 0.5:
                    cues.append((0.2, 'yaw', look, 0.6))
                    t_free += travel(look - p.yaw, cfg.yaw.vmax * 0.6, cfg.yaw.amax)
                    p.yaw = look
                tl = Timeline(cfg, H, p.yaw, p.frac, t_free=t_free)
                tl.lift(timing['phrases'][-1]['end'] + 0.15, p.frac - LISTEN_NOD * amp, 0.7, optional=True)
                p.steps.append(Step('talk', p.yaw, file=None, cues=sorted(cues + tl.cues, key=lambda c: c[0]),
                                    end_lift=H.at(p.frac)))
        for p in poses:
            p.steps.append(Step('wait', GAP_S))
        if g == 'joy' and dance and not danced:   # 2 基で真ん中を挟んで左右対称に踊る
            danced = True
            sync()
            mid = 0.45
            for p in poses:
                p.go(cfg, H, yaw=p.present, frac=mid)
            sync()
            for i, p in enumerate(poses):
                # 振り始めは真ん中（相手の方）へ。partner_side が −1 の基は左右を逆に振る
                p.steps.append(Step('talk', p.present, file=dance if i == 0 else None, label=f'{s["n"]}/{total} ♪ ダンス',
                                    cues=dance_cues(cfg, H, p.present, mid, mirror=p.side < 0, amp=amp),
                                    end_lift=H.at(0.95)))
                p.frac = 0.95
                p.steps.append(Step('wait', 0.2))
    sync()
    for p in poses:
        p.go(cfg, H, yaw=0.0)
        p.steps.append(Step('lift', H.home))
    return {p.name: scale_speeds(p.steps, vs) for p in poses}
