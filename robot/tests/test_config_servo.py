import os
import tempfile
import unittest

from srr.config import ConfigError, ServoConfig
from srr.servo import MockPwm, Servo, SysfsPwm, find_pwmchip, sysfs_location

from helpers import make_cfg


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_limits_follow_requirements(self):
        cfg = make_cfg(self.tmp)
        # 昇降：ホーン角 0〜−61.5°、両端 1° 余裕
        self.assertEqual((cfg.lift.lo, cfg.lift.hi, cfg.lift.home), (-60.5, -1.0, -1.0))
        self.assertAlmostEqual(cfg.lift.vmax, 61.5)  # 1 ストローク 1 s
        self.assertEqual((cfg.yaw.lo, cfg.yaw.hi), (-90.0, 90.0))
        self.assertEqual(cfg.yaw.vmax, 60.0)

    def test_hx711_can_be_disabled(self):
        self.assertFalse(make_cfg(self.tmp, hx711__enabled=False).hx711_enabled)
        self.assertTrue(make_cfg(self.tmp, hx711__enabled=True).hx711_enabled)

    def test_disabled_unit_is_skipped(self):
        cfg = make_cfg(self.tmp, units__1__enabled=False)
        self.assertEqual([(u.index, u.number, u.scale.dt_gpio) for u in cfg.units], [(0, 1, 6)])
        cfg = make_cfg(self.tmp, units__0__enabled=False)
        # 1 基目を外すと 2 基目が DT の並びの先頭になるが、番号は 2 のまま
        self.assertEqual([(u.index, u.number, u.scale.dt_gpio) for u in cfg.units], [(0, 2, 26)])
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, units__0__enabled=False, units__1__enabled=False)

    def test_180_degree_servo_limits_yaw_to_60(self):
        cfg = make_cfg(self.tmp, servo_range_deg=180)
        self.assertEqual((cfg.yaw.lo, cfg.yaw.hi), (-60.0, 60.0))

    def test_rejects_values_beyond_requirements(self):
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, yaw__max_speed_deg_s=90)
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, lift__min_stroke_s=0.5)
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, units__1__yaw__gpio=16)
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, units__0__lift__pulse_max_us=3000)


class ServoTest(unittest.TestCase):
    def servo(self, invert=False):
        sc = ServoConfig(gpio=16, pulse_min_us=600, pulse_center_us=1450, pulse_max_us=2400, invert=invert)
        return Servo(sc, MockPwm(), 270)

    def test_pulse_mapping_is_piecewise_around_center(self):
        s = self.servo()
        self.assertEqual(s.pulse_for(0), 1450)
        self.assertEqual(s.pulse_for(135), 2400)
        self.assertEqual(s.pulse_for(-135), 600)
        self.assertAlmostEqual(s.pulse_for(67.5), 1925)
        self.assertEqual(s.pulse_for(500), 2400)  # 可動域外は端に丸める

    def test_invert(self):
        s = self.servo(invert=True)
        self.assertEqual(s.pulse_for(135), 600)

    def test_raw_pulse_is_clamped_to_safe_range(self):
        s = self.servo()
        self.assertEqual(s.set_pulse_us(100), 400)
        self.assertEqual(s.set_pulse_us(9999), 2600)


class SysfsTest(unittest.TestCase):
    """/sys/class/pwm と同じ形の木を作って、device 名から pwmchip を引けることを確かめる。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        devices = os.path.join(self.root, 'devices')
        for chip, dev, nch in [('pwmchip0', '1f00098000.pwm', 4), ('pwmchip1', 'pwm_pio@17', 1),
                               ('pwmchip2', 'pwm_pio@10', 1)]:
            os.makedirs(os.path.join(devices, dev))
            c = os.path.join(self.root, 'class', chip)
            os.makedirs(c)
            os.symlink(os.path.join(devices, dev), os.path.join(c, 'device'))
            for ch in range(nch):
                d = os.path.join(c, f'pwm{ch}')
                os.makedirs(d)
                for attr in ('period', 'duty_cycle', 'enable'):
                    open(os.path.join(d, attr), 'w').close()
            for attr in ('export', 'unexport'):
                open(os.path.join(c, attr), 'w').close()
        self.cls = os.path.join(self.root, 'class')

    def read(self, chip, ch, attr):
        with open(os.path.join(self.cls, chip, f'pwm{ch}', attr)) as f:
            return f.read()

    def test_location_and_chip_lookup_ignore_chip_numbers(self):
        self.assertEqual(sysfs_location(13), ('1f00098000.pwm', 1))
        self.assertEqual(sysfs_location(16), ('pwm_pio@10', 0))
        self.assertEqual(sysfs_location(23), ('pwm_pio@17', 0))
        self.assertTrue(find_pwmchip('pwm_pio@10', self.cls).endswith('pwmchip2'))
        with self.assertRaises(FileNotFoundError):
            find_pwmchip('pwm_pio@4', self.cls)

    def test_writes_period_duty_enable(self):
        p = SysfsPwm(16, 50, root=self.cls)
        self.assertEqual(self.read('pwmchip2', 0, 'period'), '20000000')
        p.set_pulse_us(1500)
        self.assertEqual(self.read('pwmchip2', 0, 'duty_cycle'), '1500000')
        self.assertEqual(self.read('pwmchip2', 0, 'enable'), '1')
        p.disable()
        self.assertEqual(self.read('pwmchip2', 0, 'enable'), '0')


if __name__ == '__main__':
    unittest.main()
