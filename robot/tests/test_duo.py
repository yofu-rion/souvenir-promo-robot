import json
import os
import tempfile
import unittest

from srr.explain import LOOK_DEG, build_duo_steps
from srr.remote import MockRemote
from srr.unit import State

from helpers import make_cfg, make_controller, run_for
from test_unit_controller import InterlockWatcher

PLAN = {'sentences': [
    {'n': 1, 'text': 'こんにちは！', 'speakers': [1], 'gesture': 'beckon', 'valence': 0.5, 'arousal': 0.75, 'voice': '01.wav'},
    {'n': 2, 'text': 'わたしも、ネジチョコ！', 'speakers': [2], 'gesture': None, 'valence': 0.3, 'arousal': 0.3, 'voice': '02.wav'},
    {'n': 3, 'text': '楽しいよ！', 'speakers': [2], 'gesture': 'joy', 'valence': 0.8, 'arousal': 0.85, 'voice': '03.wav'},
    {'n': 4, 'text': 'ありがとう！', 'speakers': [1, 2], 'gesture': 'thanks', 'valence': 0.6, 'arousal': 0.05, 'voice': '04.wav'},
]}
DUR = {'01': 1.2, '02': 2.5, '03': 1.6, '04': 1.1, 'da': 9.0}


def timing(d):
    return {'duration': d, 'phrases': [{'end': round(d * 0.5, 2), 'pause': 0.3}, {'end': round(d - 0.1, 2), 'pause': 0.0}]}


class TimedPlayer:
    def __init__(self):
        self.left, self.played = 0.0, []

    def play(self, path, volume=1.0, channel='both'):
        self.played.append((os.path.basename(path), channel))
        self.left = DUR[os.path.basename(path)[:2]]

    def step(self, dt):
        self.left = max(0.0, self.left - dt)

    @property
    def playing(self):
        return self.left > 0

    def stop(self):
        self.left = 0.0


class DuoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for k in ('01', '02', '03'):
            with open(os.path.join(self.tmp, f'{k}.json'), 'w') as f:
                json.dump(timing(DUR[k]), f)
        for k in ('04-1', '04-2', '04'):
            with open(os.path.join(self.tmp, f'{k}.json'), 'w') as f:
                json.dump(timing(DUR['04']), f)
        plan = os.path.join(self.tmp, 'duo.json')
        with open(plan, 'w', encoding='utf-8') as f:
            json.dump(PLAN, f, ensure_ascii=False)
        solo = os.path.join(self.tmp, 'solo.json')   # 2 基目だけが話す台本
        with open(solo, 'w', encoding='utf-8') as f:
            json.dump({'sentences': [dict(s, speakers=[2], gesture=None) for s in PLAN['sentences'][1:3]]}, f,
                      ensure_ascii=False)
        self.cfg = make_cfg(self.tmp, sequence__mode='explain', sequence__programs=[
            {'name': '掛け合い', 'plan': plan, 'voices': self.tmp, 'dance': os.path.join(self.tmp, 'dance.wav'),
             'cast': ['缶', '六角箱']},
            {'name': 'ひとり', 'plan': solo, 'voices': self.tmp}])

    def _controller(self):
        remote = MockRemote()
        ctl, units, _, _ = make_controller(self.cfg, remote=remote)
        player = TimedPlayer()
        ctl.player = player
        for u in units:
            u.player = player
        ctl.tare(timeout=1, sleep=lambda s: None)
        ctl.start()
        run_for(ctl, 6)
        return ctl, units, remote, player

    def test_steps_share_barriers_and_only_speakers_play(self):
        steps = build_duo_steps(self.cfg, self.cfg.units, PLAN, self.tmp, os.path.join(self.tmp, 'dance.wav'))
        a, b = steps['1基目'], steps['2基目']
        syncs_a = [s.barrier for s in a if s.kind == 'sync']
        syncs_b = [s.barrier for s in b if s.kind == 'sync']
        self.assertEqual(len(syncs_a), len(syncs_b))
        self.assertTrue(all(x is y for x, y in zip(syncs_a, syncs_b)))   # 同じ待ち合わせを同じ順番で通る
        talk_a = [(os.path.basename(s.file) if s.file else None, s.channel) for s in a if s.kind == 'talk']
        talk_b = [(os.path.basename(s.file) if s.file else None, s.channel) for s in b if s.kind == 'talk']
        # 1 基目：話す・聞く・聞く・ダンスの音楽を流す・そろって話す（左右に分けた声）
        self.assertEqual(talk_a, [('01.wav', None), (None, None), (None, None), ('dance.wav', None), ('04.wav', 'stereo')])
        self.assertEqual(talk_b, [(None, None), ('02.wav', None), ('03.wav', None), (None, None), (None, 'stereo')])
        # ダンスは左右対称に振る
        dance_a = [s for s in a if s.kind == 'talk' and s.file and s.file.endswith('dance.wav')][0]
        dance_b = [s for s in b if s.kind == 'talk' and 'ダンス' in s.label][0]
        sw_a = [c[2] - dance_a.value for c in dance_a.cues if c[1] == 'yaw' and c[2] != dance_a.value][0]
        sw_b = [c[2] - dance_b.value for c in dance_b.cues if c[1] == 'yaw' and c[2] != dance_b.value][0]
        self.assertAlmostEqual(sw_a, -sw_b)

    def test_runs_together_on_controller(self):
        ctl, units, remote, player = self._controller()
        self.assertEqual([u.state for u in units], [State.IDLE, State.IDLE])
        watch = InterlockWatcher(self, units, front_only=False)
        remote.push('once')
        run_for(ctl, 0.1, each=watch)
        self.assertEqual([u.state for u in units], [State.ACTIVE, State.ACTIVE])
        u1, u2 = units
        wrong_look = []   # 聞き役が、相手の方へ LOOK_DEG 向く以外の向き変えをしたか

        def each():
            watch()
            for me, other in ((u1, u2), (u2, u1)):
                # 聞き役（ラベルのない talk）のときだけ見る。そろって話す文・ダンスでは 2 基とも動く
                st = me.step
                listening = st is not None and st.kind == 'talk' and not st.label
                look = self.cfg.yaw_present_deg + me.ucfg.partner_side * LOOK_DEG * ctl._factors()[0]
                if other.speaking and listening and me.yaw.moving and abs(me.yaw.target - look) > 0.5:
                    wrong_look.append((me.name, me.yaw.target))
        run_for(ctl, 60, each=each)
        self.assertEqual(player.played, [('01.wav', 'left'), ('02.wav', 'right'), ('03.wav', 'right'),
                                         ('dance.wav', 'left'), ('04.wav', 'stereo')])
        self.assertFalse(wrong_look)   # 聞き役は相手の方を少し向くだけ（話している方だけが大きく動く）
        self.assertEqual(u1.ucfg.partner_side, -u2.ucfg.partner_side)   # 2 基は向かい合う
        self.assertEqual([u.state for u in units], [State.IDLE, State.IDLE])
        self.assertTrue(all(u.at_home for u in units))

    def test_remote_selects_program_and_remembers_it(self):
        ctl, units, remote, player = self._controller()
        self.assertEqual(ctl.program.name, '掛け合い')
        remote.push('prog', 1)
        run_for(ctl, 0.3)
        self.assertEqual(ctl.program.name, 'ひとり')
        self.assertIn('2/2 ひとり', remote.sent[-1]['t'][0])
        watch = InterlockWatcher(self, units, front_only=False)
        remote.push('once')
        run_for(ctl, 30, each=watch)
        # 2 基目だけが話し、1 基目は聞き役（声を流さない）
        self.assertEqual(player.played, [('02.wav', 'right'), ('03.wav', 'right')])
        self.assertEqual([u.state for u in units], [State.IDLE, State.IDLE])
        self.assertTrue(all(u.at_home for u in units))
        # 選んだ台本は settings.json に残り、次の起動でも使う
        ctl2, _, _, _ = make_controller(self.cfg)
        self.assertEqual(ctl2.program.name, 'ひとり')
        remote.push('prog', 1)   # 端まで行ったら最初へ戻る
        run_for(ctl, 0.3)
        self.assertEqual(ctl.program.name, '掛け合い')

    def test_swap_moves_characters_to_the_other_unit(self):
        ctl, units, remote, player = self._controller()
        self.assertIn('1基目 待機 缶', remote.sent[-1]['t'])
        remote.push('swap')
        run_for(ctl, 0.3)
        self.assertTrue(ctl.swap)
        self.assertIn('1基目 待機 六角箱', remote.sent[-1]['t'])   # 画面にも受け持つキャラを出す
        self.assertIn('2基目 待機 缶', remote.sent[-1]['t'])
        watch = InterlockWatcher(self, units, front_only=False)
        remote.push('once')
        run_for(ctl, 60, each=watch)
        # 話者 1 のせりふは 2 基目（右）、話者 2 は 1 基目（左）。そろって話す声は左右を入れ替える
        self.assertEqual(player.played, [('01.wav', 'right'), ('02.wav', 'left'), ('03.wav', 'left'),
                                         ('dance.wav', 'left'), ('04.wav', 'stereo-swap')])
        self.assertEqual([u.state for u in units], [State.IDLE, State.IDLE])
        ctl2, _, _, _ = make_controller(self.cfg)   # 次の起動でも入れ替えたまま
        self.assertTrue(ctl2.swap)


if __name__ == '__main__':
    unittest.main()
