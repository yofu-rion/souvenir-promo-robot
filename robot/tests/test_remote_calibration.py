import os
import tempfile
import unittest

import yaml

from srr import config as config_mod
from srr.calibration import MAX_DEV_US, derive_pulses
from srr.config import ServoConfig
from srr.remote import MockRemote
from srr.unit import State

from helpers import BASE_RAW, make_cfg, make_controller, run_for


class DerivePulsesTest(unittest.TestCase):
    def test_both_sides_measured(self):
        sc = ServoConfig(gpio=16, pulse_min_us=500, pulse_center_us=1500, pulse_max_us=2500)
        # ヨー：テーブル +90° = サーボ −135°、−90° = +135°（270° 版、half = 135）
        self.assertEqual(derive_pulses(sc, 135, 1520, [(-135, 540), (135, 2460)]), (540, 1520, 2460))

    def test_one_side_is_extrapolated_and_other_keeps_span(self):
        sc = ServoConfig(gpio=12, pulse_min_us=500, pulse_center_us=1500, pulse_max_us=2500)
        # 昇降：−60.5° で 1080 µs → 中心から −420 µs → 端（−135°）では −420 × 135/60.5
        mn, ce, mx = derive_pulses(sc, 135, 1510, [(-60.5, 1090)])
        self.assertAlmostEqual(mn, 1510 - 420 * 135 / 60.5)
        self.assertEqual((ce, mx), (1510, 2510))

    def test_invert_swaps_sides(self):
        sc = ServoConfig(gpio=12, pulse_min_us=500, pulse_center_us=1500, pulse_max_us=2500, invert=True)
        mn, ce, mx = derive_pulses(sc, 135, 1500, [(-67.5, 2000)])
        self.assertEqual((mn, ce, mx), (500, 1500, 2500))


class CalibrationConfigTest(unittest.TestCase):
    def test_calibration_overrides_pulses(self):
        raw = dict(BASE_RAW)
        cal = {'units': {'1基目': {'yaw': {'pulse_min_us': 520, 'pulse_center_us': 1490, 'pulse_max_us': 2470}}}}
        cfg = config_mod.parse(raw, calibration=cal)
        y = cfg.units[0].yaw
        self.assertEqual((y.pulse_min_us, y.pulse_center_us, y.pulse_max_us), (520, 1490, 2470))
        self.assertEqual(cfg.units[0].lift.pulse_center_us, 1500)


class RemoteControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp, sequence__auto_interval_s=5.0, units__1__enabled=False,
                            remote__enabled=True)
        self.cfg.calibration_file = os.path.join(self.tmp, 'calibration.yaml')
        self.remote = MockRemote()
        self.ctl, self.units, _, _ = make_controller(self.cfg, grams=(200,), remote=self.remote)
        self.ctl.tare(timeout=1, sleep=lambda s: None)
        self.ctl.start()
        run_for(self.ctl, 3)   # 原点移動
        self.u = self.units[0]
        self.assertEqual(self.u.state, State.IDLE)

    def test_waits_for_start_then_runs(self):
        run_for(self.ctl, 8)
        self.assertEqual(self.u.state, State.IDLE)        # 開始を押すまで動かない
        self.assertEqual(self.remote.sent[-1]['m'], 'remote')
        self.assertFalse(self.remote.sent[-1]['run'])
        self.remote.push('start')
        run_for(self.ctl, 0.1)
        self.assertEqual(self.u.state, State.ACTIVE)     # すぐ 1 回目
        self.assertTrue(self.remote.sent[-1]['run'])

    def test_stop_returns_home_and_pauses(self):
        self.remote.push('start')
        run_for(self.ctl, 1.0)
        self.remote.push('stop')
        run_for(self.ctl, 4)
        self.assertEqual(self.u.state, State.IDLE)
        self.assertTrue(self.u.at_home)
        run_for(self.ctl, 8)
        self.assertEqual(self.u.state, State.IDLE)       # 止めたまま

    def test_once_runs_a_single_cycle_while_paused(self):
        self.remote.push('once')
        run_for(self.ctl, 0.1)
        self.assertEqual(self.u.state, State.ACTIVE)
        run_for(self.ctl, 15)
        self.assertEqual(self.u.state, State.IDLE)
        run_for(self.ctl, 8)
        self.assertEqual(self.u.state, State.IDLE)

    def enter_calib(self):
        self.remote.push('mode', 'calib')
        run_for(self.ctl, 0.1)
        self.assertIsNotNone(self.ctl.calib)
        run_for(self.ctl, 3)   # 最初の点（昇降の下）へ
        return self.ctl.calib

    def test_calibration_moves_slowly_saves_and_returns(self):
        cal = self.enter_calib()
        self.assertEqual(self.remote.sent[-1]['m'], 'calib')
        lift = self.u.lift.servo.pwm
        self.remote.push('adj', 10)
        self.remote.push('adj', 10)
        run_for(self.ctl, 0.5)
        self.assertAlmostEqual(lift.pulse_us, 1520)
        # 上の点へ：パルス幅は毎周期 400 µs/s × 0.02 s = 8 µs までしか変わらない
        self.remote.push('next')
        hist_from = len(lift.history)
        run_for(self.ctl, 3)
        steps = [abs(b - a) for a, b in zip(lift.history[hist_from:], lift.history[hist_from + 1:])]
        self.assertLessEqual(max(steps), 8.0 + 1e-6)
        top_us = lift.pulse_us
        self.remote.push('adj', -10)
        run_for(self.ctl, 0.2)
        self.remote.push('save')
        run_for(self.ctl, 0.1)
        self.assertIn('保存しました', self.remote.sent[-1]['t'][3])
        with open(self.cfg.calibration_file, encoding='utf-8') as f:
            saved = yaml.safe_load(f)['units']['1基目']['lift']
        self.assertEqual(saved['pulse_center_us'], 1520)
        # 保存した値で、上の点（−60.5°）が合わせた値になる
        sc = self.u.ucfg.lift
        self.assertEqual(sc.pulse_center_us, 1520)
        self.assertAlmostEqual(self.u.lift.servo.pulse_for(-60.5), top_us - 10, places=3)
        # 終わると原点へ戻り、待機に戻る
        self.remote.push('mode', 'remote')
        run_for(self.ctl, 4)
        self.assertIsNone(self.ctl.calib)
        self.assertEqual(self.u.state, State.IDLE)
        self.assertTrue(self.u.at_home)
        self.assertAlmostEqual(lift.pulse_us, self.u.lift.servo.pulse_for(self.cfg.lift.home))

    def test_yaw_point_lowers_lift_first_and_limits_adjustment(self):
        cal = self.enter_calib()
        self.remote.push('next')   # 上
        run_for(self.ctl, 3)
        self.remote.push('next')   # ヨー正面：先に昇降を下へ戻す
        run_for(self.ctl, 0.1)
        self.assertEqual(cal.point.key, 'front')
        run_for(self.ctl, 3)
        self.assertAlmostEqual(self.u.lift.servo.pwm.pulse_us, 1500)
        for _ in range(40):
            self.remote.push('adj', 10)
        run_for(self.ctl, 1)
        self.assertAlmostEqual(cal.us[id(cal.point)], 1500 + MAX_DEV_US)
        self.assertIn('まで', cal.lines()[3])

    def test_unsaved_changes_need_second_press(self):
        self.enter_calib()
        self.remote.push('adj', 5)
        run_for(self.ctl, 0.2)
        self.remote.push('mode', 'remote')
        run_for(self.ctl, 0.2)
        self.assertIsNotNone(self.ctl.calib)
        self.assertIn('破棄', self.remote.sent[-1]['t'][3])
        self.remote.push('mode', 'remote')
        run_for(self.ctl, 4)
        self.assertIsNone(self.ctl.calib)
        self.assertEqual(self.u.ucfg.lift.pulse_center_us, 1500)   # 保存していないので元のまま

    def test_volume_from_remote_is_saved_and_used(self):
        self.assertEqual(self.ctl.volume, 80)               # 初期値
        self.remote.push('mode', 'volume')
        for _ in range(3):
            self.remote.push('vol', 5)
        run_for(self.ctl, 0.3)
        self.assertEqual(self.ctl.volume, 95)
        self.assertAlmostEqual(self.ctl.player.master, 0.95)
        self.assertEqual(self.remote.sent[-1]['m'], 'settings')
        self.assertTrue(self.remote.sent[-1]['t'][0].startswith('音量 95%'))
        for _ in range(25):
            self.remote.push('vol', 5)
        run_for(self.ctl, 0.3)
        self.assertEqual(self.ctl.volume, 200)              # 200% より上にはしない
        self.remote.push('voltest')
        run_for(self.ctl, 0.1)
        self.assertEqual(self.ctl.player.played[-1][0], self.u.ucfg.sound.file)
        # 作り直しても残っている
        ctl2, _, _, player2 = make_controller(self.cfg, grams=(200,), remote=MockRemote())
        self.assertEqual(ctl2.volume, 200)
        self.assertAlmostEqual(player2.master, 2.0)
        self.remote.push('mode', 'remote')
        run_for(self.ctl, 0.3)
        self.assertEqual(self.remote.sent[-1]['m'], 'remote')

    def test_motion_size_from_remote_scales_the_explanation(self):
        from srr.explain import NOD_END, Heights, talk_cues
        self.assertEqual(self.ctl.motion, 100)              # 初期値（config の motion_base が 100%）
        self.assertEqual(self.ctl._factors(), (self.cfg.motion_base, self.cfg.tempo_base))
        self.remote.push('mode', 'settings')
        for _ in range(10):
            self.remote.push('amp', 10)
        run_for(self.ctl, 0.3)
        self.assertEqual(self.ctl.motion, 150)              # 150% より上にはしない
        self.assertTrue(self.remote.sent[-1]['t'][1].startswith('動きの大きさ 150%'))
        self.assertAlmostEqual(self.ctl._factors()[0], 1.5 * self.cfg.motion_base)
        H = Heights(self.cfg)
        timing = {'duration': 3.0, 'phrases': [{'end': 2.9, 'pause': 0.0}]}
        nod = lambda amp: [c for c in talk_cues(self.cfg, H, 0.3, 0.0, timing, 0.3, 0.0, None, 1, amp=amp)
                           if len(c) > 4][0][2]
        self.assertGreater(nod(2.0) - nod(1.0), 0)          # 大きくすると、うなずきが深くなる（論理角は下ほど大きい）

    def test_interval_from_remote_sets_the_wait_between_runs(self):
        self.assertEqual(self.ctl.interval, self.cfg.auto_interval_s)   # 初期値は config
        self.remote.push('mode', 'settings')
        for _ in range(4):
            self.remote.push('itv', 5)
        run_for(self.ctl, 0.3)
        self.assertEqual(self.ctl.interval, self.cfg.auto_interval_s + 20)
        self.assertIn(f'間隔 {self.ctl.interval} 秒', self.remote.sent[-1]['t'][2])
        for _ in range(200):
            self.remote.push('itv', -5)
        run_for(self.ctl, 0.3)
        self.assertEqual(self.ctl.interval, 0)                           # 0 秒より短くはしない
        ctl2, _, _, _ = make_controller(self.cfg, grams=(200,), remote=MockRemote())
        self.assertEqual(ctl2.interval, 0)                               # 次の起動でも残る

    def test_manual_mode_moves_one_axis_at_a_time_within_limits(self):
        self.remote.push('mode', 'manual')
        run_for(self.ctl, 0.1)
        self.assertIsNotNone(self.ctl.manual)
        self.assertEqual(self.remote.sent[-1]['m'], 'manual')
        man = self.ctl.manual
        self.assertEqual(man.target, (self.u, 'lift'))
        self.remote.push('adj', 10)                          # 高さ ＋10%
        run_for(self.ctl, 0.1)
        self.assertTrue(self.u.lift.moving)
        self.remote.push('next')                             # ヨーへ
        self.remote.push('adj', 10)                          # 昇降が動いている間は受け付けない
        run_for(self.ctl, 0.05)
        self.assertFalse(self.u.yaw.moving)
        self.assertIn('動いている間は動かせない', man.message)
        run_for(self.ctl, 2)
        self.assertAlmostEqual(man.lift_percent(self.u.lift.pos), 10, delta=0.5)
        self.remote.push('adj', 10)
        run_for(self.ctl, 2)
        self.assertAlmostEqual(self.u.yaw.pos, 10.0)
        for _ in range(12):                                  # 可動範囲の端で止まる
            self.remote.push('adj', 10)
        run_for(self.ctl, 0.1)
        run_for(self.ctl, 3)
        self.assertEqual(self.u.yaw.pos, self.cfg.yaw.hi)
        self.assertIn('向き', self.remote.sent[-1]['t'][1])
        self.remote.push('mode', 'remote')                   # 終えると原点へ
        run_for(self.ctl, 6)
        self.assertIsNone(self.ctl.manual)
        self.assertTrue(self.u.at_home)
        self.assertEqual(self.u.state, State.IDLE)
        self.assertEqual(self.remote.sent[-1]['m'], 'remote')

    def test_shutdown_during_calibration_returns_home_first(self):
        self.enter_calib()
        self.remote.push('next')
        run_for(self.ctl, 1)
        self.ctl.request_stop()
        run_for(self.ctl, 5)
        self.assertIsNone(self.ctl.calib)
        self.assertTrue(self.ctl.stopped)
        self.assertAlmostEqual(self.u.yaw.servo.pwm.pulse_us, 1500)


if __name__ == '__main__':
    unittest.main()
