import json
import tempfile
import unittest

from srr.unit import State

from helpers import make_cfg, make_controller, run_for


class InterlockWatcher:
    """毎周期、昇降とヨーが同時に動いていないか、昇降がヨー正面のときだけ動くかを見る。"""

    def __init__(self, tc, units):
        self.tc, self.units = tc, units
        self.prev = {u.name: u.pose() for u in units}
        self.max_jump_us = 0.0
        self.prev_us = {}

    def __call__(self):
        for u in self.units:
            p, q = self.prev[u.name], u.pose()
            if None not in (p['lift'], q['lift']):
                lift_moved = q['lift'] != p['lift']
                yaw_moved = q['yaw'] != p['yaw']
                self.tc.assertFalse(lift_moved and yaw_moved, f'{u.name}: 昇降とヨーが同時に動いた')
                if lift_moved:
                    self.tc.assertLessEqual(abs(q['yaw']), 0.5, f'{u.name}: ヨーが正面にないまま昇降した')
                cfg = u.cfg
                self.tc.assertTrue(cfg.lift.lo <= q['lift'] <= cfg.lift.hi)
                self.tc.assertTrue(cfg.yaw.lo <= q['yaw'] <= cfg.yaw.hi)
            for ax in (u.lift, u.yaw):
                us = ax.servo.pwm.pulse_us
                if us is not None and self.prev_us.get(ax.name) is not None:
                    self.max_jump_us = max(self.max_jump_us, abs(us - self.prev_us[ax.name]))
                self.prev_us[ax.name] = us
            self.prev[u.name] = q


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp, sequence__auto_interval_s=5.0)

    def boot(self, **kw):
        ctl, units, bus, player = make_controller(self.cfg, **kw)
        ctl.tare(timeout=1, sleep=lambda s: None)
        ctl.start()
        return ctl, units, bus, player

    def test_homing_starts_one_unit_at_a_time(self):
        ctl, (u1, u2), _, _ = self.boot()
        ctl.tick(0.02)
        self.assertEqual(u1.state, State.HOMING)
        self.assertIsNone(u2.lift.pos)          # 2 基目はまだ PWM を出していない
        self.assertFalse(u2.lift.servo.pwm.enabled)
        run_for(ctl, 0.5)
        self.assertIsNone(u2.lift.pos)          # 1 基目の原点移動が終わるまで待つ
        run_for(ctl, 1.5)
        self.assertIsNotNone(u2.lift.pos)
        self.assertEqual((u1.state, u2.state), (State.IDLE, State.IDLE))

    def test_homing_ramps_slowly_from_saved_pose(self):
        with open(self.cfg.state_file, 'w') as f:
            json.dump({'1基目': {'lift': -60.5, 'yaw': 45.0}}, f)
        ctl, units, _, _ = self.boot()
        w = InterlockWatcher(self, units)
        run_for(ctl, 0.02)
        u1 = units[0]
        # 保存した姿勢から出力を始める（最初のパルスがその姿勢のもの）
        self.assertEqual(u1.yaw.servo.pwm.history[0], u1.yaw.servo.pulse_for(-1.5 * 45.0))
        self.assertEqual(u1.lift.servo.pwm.history[0], u1.lift.servo.pulse_for(-60.5))
        t = 0.0
        while u1.state == State.HOMING and t < 15:
            ctl.tick(0.02)
            w()
            t += 0.02
        self.assertTrue(u1.at_home)
        self.assertGreater(t, 5)                 # 通常速度の 30% でゆっくり戻る
        self.assertLess(w.max_jump_us, 15)       # 原点移動中にパルスが跳ばない（通常速度の 30%）

    def test_full_cycle_order_and_interlock(self):
        ctl, units, _, player = self.boot()
        w = InterlockWatcher(self, units)
        run_for(ctl, 3, each=w)                  # 原点移動
        seen_top = seen_present = False
        u1 = units[0]
        for _ in range(int(20 / 0.02)):
            ctl.tick(0.02)
            w()
            if u1.lift.pos == u1.lift_top:
                seen_top = True
            if u1.yaw.pos == self.cfg.yaw_present_deg:
                self.assertTrue(seen_top)        # 上昇してから旋回する
                seen_present = True
        self.assertTrue(seen_present)
        self.assertEqual(player.played[0][0], self.cfg.units[0].sound.file)
        self.assertEqual(player.played[0][2], 'left')
        self.assertTrue(u1.at_home)

    def test_units_alternate(self):
        ctl, units, _, player = self.boot()
        run_for(ctl, 60)
        files = [p[0] for p in player.played]
        self.assertGreaterEqual(len(files), 3)
        self.assertEqual(files[:2], [self.cfg.units[0].sound.file, self.cfg.units[1].sound.file])
        self.assertTrue(all(a != b for a, b in zip(files, files[1:])))

    def test_product_removed_then_returned(self):
        ctl, (u1, u2), bus, _ = self.boot()
        run_for(ctl, 3)
        run_for(ctl, 5.5)                        # 1 基目が動き出す
        self.assertEqual(u1.state, State.ACTIVE)
        bus.grams[0] = 0                         # 商品を持ち上げた
        w = InterlockWatcher(self, [u1, u2])
        run_for(ctl, 6, each=w)
        self.assertEqual(u1.state, State.NO_PRODUCT)
        self.assertTrue(u1.at_home)              # 下降位置で待つ
        run_for(ctl, 20)
        self.assertEqual(u1.state, State.NO_PRODUCT)  # 商品がない間は動かない
        bus.grams[0] = 200
        run_for(ctl, 3)
        self.assertIn(u1.state, (State.IDLE, State.ACTIVE))

    def test_weight_surge_stops_motion(self):
        ctl, (u1, _), bus, _ = self.boot()
        run_for(ctl, 3 + 5.5)
        self.assertEqual(u1.state, State.ACTIVE)
        bus.grams[0] = 900                       # 上から押された
        run_for(ctl, 0.5)
        self.assertIsNotNone(u1.after_abort)
        run_for(ctl, 6)
        self.assertTrue(u1.at_home)
        self.assertEqual(u1.state, State.IDLE)

    def test_stop_returns_home_then_disables_pwm(self):
        ctl, units, bus, _ = self.boot()
        run_for(ctl, 3 + 6.5)                    # サイクルの途中
        self.assertEqual(units[0].state, State.ACTIVE)
        ctl.request_stop()
        for _ in range(1000):
            if ctl.stopped:
                break
            ctl.tick(0.02)
        self.assertTrue(ctl.stopped)
        self.assertTrue(all(u.at_home and u.state == State.STOPPED for u in units))
        ctl.close()
        self.assertTrue(all(not ax.servo.pwm.enabled for u in units for ax in (u.lift, u.yaw)))
        with open(self.cfg.state_file) as f:
            saved = json.load(f)
        self.assertEqual(saved['1基目'], {'lift': self.cfg.lift.home, 'yaw': 0.0})
        self.assertTrue(bus.powered_down)

    def test_runs_without_scale(self):
        ctl, units, _, player = self.boot(use_bus=False)
        run_for(ctl, 15)
        self.assertGreaterEqual(len(player.played), 1)


if __name__ == '__main__':
    unittest.main()
