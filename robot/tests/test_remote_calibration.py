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
