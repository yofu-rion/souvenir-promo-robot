import json
import os
import tempfile
import unittest

from srr.explain import (DANCE_BPM, DANCE_SWING_DEG, GLANCE_DEG, SHOW_DEG, SIGN_RAISE, Heights, build_steps,
                         dance_cues, height_for, load_timing, talk_cues)
from srr.remote import MockRemote
from srr.unit import State

from helpers import make_cfg, make_controller, run_for
from test_unit_controller import InterlockWatcher

PLAN = {'sentences': [
    {'n': 1, 'text': 'こんにちは！', 'gesture': 'beckon', 'valence': 0.5, 'arousal': 0.75, 'voice': '01.wav'},
    {'n': 2, 'text': '北九州から来た、ネジチョコです。', 'gesture': None, 'valence': 0.3, 'arousal': 0.0, 'voice': '02.wav'},
    {'n': 3, 'text': 'でも、暑いところはちょっと苦手なんだ。', 'gesture': None, 'valence': -0.3, 'arousal': -0.2, 'voice': '03.wav'},
    {'n': 4, 'text': 'ありがとう！', 'gesture': 'thanks', 'valence': 0.6, 'arousal': 0.05, 'voice': '04.wav'},
]}
PLAN_DANCE = {'sentences': [
    {'n': 1, 'text': 'ナットを回して、締められるよ。', 'gesture': None, 'valence': 0.3, 'arousal': 0.0, 'voice': '02.wav'},
    {'n': 2, 'text': '楽しいよ！', 'gesture': 'joy', 'valence': 0.8, 'arousal': 0.85, 'voice': '01.wav'},
    {'n': 3, 'text': 'ありがとう！', 'gesture': 'thanks', 'valence': 0.6, 'arousal': 0.05, 'voice': '04.wav'},
]}
TIMING = {  # make-voice.py が書く形（声の長さ・句末の時刻・息継ぎ）
    '01': {'duration': 1.1, 'phrases': [{'end': 1.0, 'pause': 0.0}]},
    '02': {'duration': 3.0, 'phrases': [{'end': 1.2, 'pause': 0.35}, {'end': 2.9, 'pause': 0.0}]},
    '03': {'duration': 5.0, 'phrases': [{'end': 0.8, 'pause': 0.3}, {'end': 2.5, 'pause': 0.0},
                                        {'end': 3.6, 'pause': 0.0}, {'end': 4.9, 'pause': 0.0}]},
    '04': {'duration': 1.1, 'phrases': [{'end': 1.0, 'pause': 0.0}]},
}


class TimedPlayer:
    """声ごとに TIMING の長さだけ再生中になる。"""

    def __init__(self):
        self.left, self.played = 0.0, []

    def play(self, path, volume=1.0, channel='both'):
        self.played.append((path, volume, channel))
        self.left = 9.0 if path.endswith('dance.wav') else TIMING.get(os.path.basename(path)[:2], {'duration': 2.0})['duration']

    def step(self, dt):
        self.left = max(0.0, self.left - dt)

    @property
    def playing(self):
        return self.left > 0

    def stop(self):
        self.left = 0.0


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        plan = os.path.join(self.tmp, 'plan.json')
        with open(plan, 'w', encoding='utf-8') as f:
            json.dump(PLAN, f, ensure_ascii=False)
        for k, t in TIMING.items():
            with open(os.path.join(self.tmp, f'{k}.json'), 'w') as f:
                json.dump(t, f)
        self.cfg = make_cfg(self.tmp, sequence__mode='explain', units__1__enabled=False,
                            units__0__explain={'plan': plan, 'voices': self.tmp})

    def test_happier_sentences_are_higher(self):
        self.assertLess(height_for(self.cfg, 0.8, None), height_for(self.cfg, 0.3, None))   # 上ほど負
        self.assertLess(height_for(self.cfg, 0.3, None), height_for(self.cfg, -0.3, None))

    def test_nods_aim_at_phrase_ends_and_yaw_does_not_sway(self):
        H = Heights(self.cfg)
        cues = talk_cues(self.cfg, H, 0.5, 45.0, TIMING['02'], 0.3, 0.0, None, 1)
        talk = H.at(0.5 + SIGN_RAISE)
        self.assertEqual(cues[0][:3], (0.05, 'lift', talk))   # 話し始めに上がる
        aimed = [c for c in cues if len(c) > 4]
        self.assertEqual([c[4] for c in aimed], [1.2, 2.9])   # 読点の息継ぎと文末を狙う
        for c in aimed:
            self.assertLess(c[0], c[4])                        # 着くまでの時間だけ前に始める
            self.assertGreater(c[2], talk)                     # 下がる＝うなずき（論理角は下ほど大きい）
        self.assertGreater(aimed[1][2] - talk, aimed[0][2] - talk)   # 文末のほうが深い
        self.assertGreater(aimed[1][2] - talk, 10.0)                  # 見て分かる大きさ（10° 以上）
        self.assertFalse([c for c in cues if c[1] == 'yaw'])          # 短い文では向きを変えない
        long = talk_cues(self.cfg, H, 0.5, 45.0, TIMING['03'], -0.3, -0.2, None, 1)
        yaws = [c[2] for c in long if c[1] == 'yaw']
        self.assertEqual(yaws, [45.0 + GLANCE_DEG, 45.0])   # 長い文は 1 回だけ向きを変えて戻す

    def test_short_thanks_bows_without_raising(self):
        H = Heights(self.cfg)
        cues = talk_cues(self.cfg, H, 0.55, 45.0, TIMING['04'], 0.6, 0.05, 'thanks', 1)
        self.assertEqual(len(cues), 2)                       # 上がらず、お辞儀して戻るだけ
        self.assertEqual(cues[0][4], 1.0)

    def test_poses_change_between_sentences_and_face_customer_for_requests(self):
        steps = build_steps(self.cfg, PLAN, self.tmp)
        yaw_steps = []   # 向きの変更（手順としての yaw と、話す前の動き pre の yaw）
        for st in steps:
            if st.kind == 'yaw':
                yaw_steps.append(st.value)
            elif st.kind == 'talk':
                yaw_steps += [t for ax, t, _ in st.pre if ax == 'yaw']
        p = self.cfg.yaw_present_deg
        # 呼びかけで大きく振り向いて正対 → 2 文目はポーズの順番で正面 → 不快な文で外側へ → お礼は正対 → 最後に正面
        self.assertEqual(yaw_steps, [p + 30.0, p, p - 25.0, p, 0.0])
        self.assertEqual([os.path.basename(s.file) for s in steps if s.kind == 'talk'],
                         ['01.wav', '02.wav', '03.wav', '04.wav'])
        self.assertEqual((steps[-1].kind, steps[-1].value), ('lift', self.cfg.lift.home))
        poses = [s.value for s in build_steps(self.cfg, {'sentences': [dict(PLAN['sentences'][1], n=i, voice='02.wav')
                                                                       for i in range(1, 4)]}, self.tmp) if s.kind == 'talk']
        self.assertEqual(len(set(poses)), 3)   # ふつうの文が続いても、文ごとに向きが変わる

    def test_show_sentence_turns_the_product(self):
        H = Heights(self.cfg)
        cues = talk_cues(self.cfg, H, 0.4, 45.0, TIMING['03'], 0.3, 0.0, None, 1, show=True)
        yaws = [c[2] for c in cues if c[1] == 'yaw' and len(c) > 4]
        self.assertIn(45.0 + SHOW_DEG, yaws)

    def test_dance_follows_the_beat(self):
        H = Heights(self.cfg)
        cues = dance_cues(self.cfg, H, 45.0, 0.45)
        beat = 60.0 / DANCE_BPM
        swings = [c for c in cues if c[1] == 'yaw' and c[2] != 45.0]
        self.assertGreaterEqual(len(swings), 2)                # 左右に振る
        self.assertEqual(swings[0][2], 45 + DANCE_SWING_DEG)
        self.assertEqual(swings[1][2], 45 - DANCE_SWING_DEG)
        self.assertGreaterEqual(len([c for c in cues if c[1] == 'lift']), 5)   # 跳ねも入る
        for c in cues:
            self.assertAlmostEqual(c[4] / beat, round(c[4] / beat))            # どの動きも拍に着く
            self.assertGreaterEqual(c[0], 0.0)                                 # 音楽より前には動かない
        for a, b in zip(cues, cues[1:]):
            self.assertLessEqual(a[4], b[0] + 1e-9)                            # 前の動きが着いてから次が始まる
        self.assertEqual(cues[-1][2], H.at(0.95))              # 決めポーズは高く
        steps = build_steps(self.cfg, PLAN_DANCE, self.tmp, dance='/x/dance.wav')
        files = [os.path.basename(s.file) for s in steps if s.kind == 'talk']
        self.assertEqual(files, ['02.wav', '01.wav', 'dance.wav', '04.wav'])   # 喜びの文の後に踊る

    def test_timing_falls_back_to_punctuation(self):
        t = load_timing('/nonexistent/05.wav', 'あいう、えお。')
        self.assertEqual(len(t['phrases']), 2)
        self.assertLess(t['phrases'][0]['end'], t['phrases'][1]['end'])

    def test_runs_on_controller_without_simultaneous_motion(self):
        plan = os.path.join(self.tmp, 'plan.json')
        with open(plan, 'w', encoding='utf-8') as f:
            ss = PLAN['sentences'][:2] + PLAN_DANCE['sentences'][1:2] + PLAN['sentences'][2:]
            json.dump({'sentences': [dict(x, n=i + 1) for i, x in enumerate(ss)]}, f, ensure_ascii=False)
        self.cfg.units[0].explain.dance = os.path.join(self.tmp, 'dance.wav')
        remote = MockRemote()
        ctl, units, _, _ = make_controller(self.cfg, grams=(200,), remote=remote)
        player = TimedPlayer()
        ctl.player = player
        for u in units:
            u.player = player
        ctl.tare(timeout=1, sleep=lambda s: None)
        ctl.start()
        run_for(ctl, 3)
        u = units[0]
        watch = InterlockWatcher(self, units, front_only=False)
        remote.push('once')
        run_for(ctl, 0.1, each=watch)
        self.assertEqual(u.state, State.ACTIVE)
        seen, yaw_turns, prev = set(), 0, None
        lift_at_side = False

        def each():
            nonlocal yaw_turns, prev, lift_at_side
            watch()
            if u.progress:
                seen.add(u.progress.split(' ')[0])
            if u.yaw.vel and prev is not None and (u.yaw.vel > 0) != (prev > 0):
                yaw_turns += 1
            if u.yaw.vel:
                prev = u.yaw.vel
            if u.lift.moving and abs(u.yaw.pos) > 10:
                lift_at_side = True
        lands = []          # (狙った時刻, 着いた時刻)：声の始まりから
        track = {'label': None, 'n': 0, 'wait': None}

        def each2():
            each()
            st = u.step
            if st is None or st.kind != 'talk' or u.talk_phase != 'speak':
                return
            if track['label'] != st.label:
                track.update(label=st.label, n=0, wait=None)
            order = sorted(st.cues, key=lambda c: c[0])
            while track['n'] < len(order) - len(u.talk_cues):   # 取り出された順番で動きを特定する
                c = order[track['n']]
                track['n'] += 1
                if len(c) > 4:
                    track['wait'] = c
            c = track['wait']
            if c is not None and c[1] == 'lift' and not u.lift.moving and u.lift.pos == c[2]:
                lands.append((c[4], u.talk_t))
                track['wait'] = None
        run_for(ctl, 60, each=each2)
        offs = [t - aim for aim, t in lands]
        self.assertTrue(offs)
        self.assertLess(max(abs(o) for o in offs), 0.08, offs)   # うなずきは狙った句末から 80 ms 以内に着く
        self.assertEqual([os.path.basename(p) for p, _, _ in player.played],
                         ['01.wav', '02.wav', '01.wav', 'dance.wav', '03.wav', '04.wav'])
        self.assertEqual(seen, {'1/5', '2/5', '3/5', '4/5', '5/5'})
        self.assertTrue(lift_at_side)            # 客側を向いたままうなずいた
        self.assertLessEqual(yaw_turns, 20)      # 左右に揺らし続けない（ダンスの振りを含めて）
        self.assertEqual(u.state, State.IDLE)
        self.assertTrue(u.at_home)

    def test_landings_stay_on_time_at_other_tempos(self):
        for tempo in (60, 130):
            remote = MockRemote()
            ctl, units, _, _ = make_controller(self.cfg, grams=(200,), remote=remote)
            player = TimedPlayer()
            ctl.player = player
            for u in units:
                u.player = player
            ctl.change_setting('tempo', tempo)
            ctl.tare(timeout=1, sleep=lambda s: None)
            ctl.start()
            run_for(ctl, 4)
            u = units[0]
            remote.push('once')
            lands, track = [], {'label': None, 'n': 0, 'wait': None}

            def each():
                st = u.step
                if st is None or st.kind != 'talk' or u.talk_phase != 'speak':
                    return
                if track['label'] != st.label:
                    track.update(label=st.label, n=0, wait=None)
                order = sorted(st.cues, key=lambda c: c[0])
                while track['n'] < len(order) - len(u.talk_cues):
                    c = order[track['n']]
                    track['n'] += 1
                    if len(c) > 4:
                        track['wait'] = c
                c = track['wait']
                if c is not None and c[1] == 'lift' and not u.lift.moving and u.lift.pos == c[2]:
                    lands.append(u.talk_t - c[4])
                    track['wait'] = None
            run_for(ctl, 60, each=each)
            self.assertAlmostEqual(u.lift.accel_scale, tempo / 100 * self.cfg.tempo_base)
            self.assertTrue(lands, tempo)
            self.assertLess(max(abs(x) for x in lands), 0.08, (tempo, lands))   # 速さを変えても狙いに着く
            self.assertEqual(u.state, State.IDLE)

    def test_falls_back_to_cycle_when_plan_is_missing(self):
        cfg = make_cfg(self.tmp, sequence__mode='explain', units__1__enabled=False,
                       units__0__explain={'plan': '/nonexistent.json', 'voices': self.tmp})
        ctl, units, _, player = make_controller(cfg, grams=(200,), remote=MockRemote())
        ctl.tare(timeout=1, sleep=lambda s: None)
        ctl.start()
        run_for(ctl, 3)
        ctl.remote.push('once')
        run_for(ctl, 20)
        self.assertEqual(len(player.played), 1)
        self.assertEqual(player.played[0][0], cfg.units[0].sound.file)   # 短い推薦を 1 回


if __name__ == '__main__':
    unittest.main()
