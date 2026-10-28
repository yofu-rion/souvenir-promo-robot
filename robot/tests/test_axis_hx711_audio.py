import os
import tempfile
import unittest
import wave

import numpy as np

from srr.audio import prepare
from srr.axis import Axis
from srr.config import AxisLimits, ServoConfig
from srr.hx711 import HX711Bus, Scale, to_signed24
from srr.servo import MockPwm, Servo


def make_axis(lo=-60.5, hi=-1.0, vmax=61.5, amax=240.0, ratio=1.0):
    sc = ServoConfig(gpio=12, pulse_min_us=500, pulse_center_us=1500, pulse_max_us=2500)
    return Axis('t', AxisLimits(lo, hi, hi, vmax, amax), Servo(sc, MockPwm(), 270), ratio)


class AxisTest(unittest.TestCase):
    def test_respects_speed_and_accel_and_arrives(self):
        ax = make_axis()
        ax.start_at(-1.0)
        ax.move_to(-60.5)
        dt, t, prev_v = 0.02, 0.0, 0.0
        while ax.moving:
            ax.update(dt)
            t += dt
            self.assertLessEqual(abs(ax.vel), 61.5 + 1e-9)
            if ax.moving:
                self.assertLessEqual(abs(ax.vel - prev_v), 240 * dt + 1e-9)
            prev_v = ax.vel
            self.assertLess(t, 5)
        self.assertEqual(ax.pos, -60.5)
        self.assertGreaterEqual(t, 1.0)  # 1 ストローク 1 s 以上

    def test_target_is_clamped(self):
        ax = make_axis()
        ax.start_at(-1.0)
        ax.move_to(10)
        self.assertEqual(ax.target, -1.0)

    def test_yaw_ratio_drives_servo_backwards(self):
        ax = make_axis(lo=-90, hi=90, vmax=60, ratio=-1.5)
        ax.start_at(90)
        self.assertEqual(ax.servo.angle, -135)
        self.assertEqual(ax.servo.pwm.pulse_us, 500)

    def test_refuses_to_move_from_unknown_position(self):
        with self.assertRaises(RuntimeError):
            make_axis().move_to(-30)


class FakeGpio:
    """SCK を共有する 2 個の HX711 を模擬する。SCK の立ち上がりで次のビットを出す。"""

    def __init__(self, values, sck=5, dts=(6, 26)):
        self.sck, self.dts = sck, list(dts)
        self.words = {dt: v & 0xFFFFFF for dt, v in zip(dts, values)}
        self.bit = {dt: 0 for dt in dts}
        self.level = 0
        self.pulses = 0

    def output(self, pin, level=0):
        pass

    def input(self, pin):
        pass

    def write(self, pin, level):
        if pin == self.sck and level == 1 and self.level == 0:
            self.pulses += 1
            for dt in self.dts:
                self.bit[dt] += 1
        self.level = level

    def read(self, pin):
        n = self.bit[pin]
        if n == 0:
            return 0  # 変換完了（DT Low）
        if n > 24:
            return 1
        return (self.words[pin] >> (24 - n)) & 1


class HX711Test(unittest.TestCase):
    def test_reads_both_channels_with_shared_clock(self):
        g = FakeGpio([123456, -54321])
        bus = HX711Bus(g, 5, [6, 26])
        self.assertEqual(bus.read(timeout=0.1), [123456, -54321])
        self.assertEqual(g.pulses, 25)  # 24 bit + ゲイン 128 の 1 発

    def test_signed24(self):
        self.assertEqual(to_signed24(0xFFFFFF), -1)
        self.assertEqual(to_signed24(0x7FFFFF), 8388607)

    def test_scale_average_tare_and_outlier(self):
        s = Scale(offset=1000, counts_per_g=10, present_g=30, window=4)
        for _ in range(4):
            s.add_raw(1000 + 2000)
        self.assertAlmostEqual(s.grams, 200)
        self.assertTrue(s.present)
        s.add_raw(5_000_000)        # 1 点だけの化けは捨てる
        self.assertAlmostEqual(s.grams, 200)
        s.add_raw(1000)             # 本物の変化（商品を持ち上げた）は平均に入る
        self.assertLess(s.grams, 200)
        self.assertFalse(s.tare_if_empty())  # 商品が載っていそうならゼロを取り直さない

    def test_tare_when_empty(self):
        s = Scale(offset=0, counts_per_g=10, present_g=30, window=2)
        s.add_raw(150)
        s.add_raw(150)
        self.assertTrue(s.tare_if_empty())
        self.assertAlmostEqual(s.grams, 0)


class AudioTest(unittest.TestCase):
    def test_prepare_pans_and_scales(self):
        path = os.path.join(tempfile.mkdtemp(), 'a.wav')
        with wave.open(path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(np.full(10, 10000, dtype='<i2').tobytes())
        pcm, rate = prepare(path, volume=0.5, channel='left')
        frames = np.frombuffer(pcm, dtype='<i2').reshape(-1, 2)
        self.assertEqual(rate, 48000)
        self.assertTrue((frames[:, 0] == 5000).all())
        self.assertTrue((frames[:, 1] == 0).all())

    def test_stereo_swap(self):
        path = os.path.join(tempfile.mkdtemp(), 's.wav')
        with wave.open(path, 'wb') as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(np.tile(np.array([1000, -2000], dtype='<i2'), 10).tobytes())
        frames = np.frombuffer(prepare(path, channel='stereo-swap')[0], dtype='<i2').reshape(-1, 2)
        self.assertTrue((frames[:, 0] == -2000).all() and (frames[:, 1] == 1000).all())

    def test_volume_above_100_is_limited_without_clipping(self):
        rate = 24000
        t = np.arange(rate * 2) / rate
        # 前半は小さい声、後半は大きい声（そのまま 2 倍にすると割れる）
        amp = np.where(t < 1.0, 4000, 30000)
        wav = (amp * np.sin(2 * np.pi * 300 * t)).astype('<i2')
        path = os.path.join(tempfile.mkdtemp(), 'v.wav')
        with wave.open(path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(wav.tobytes())
        import time
        t0 = time.perf_counter()
        pcm, _ = prepare(path, volume=2.0, channel='both')
        self.assertLess(time.perf_counter() - t0, 0.2)   # 制御ループの中で呼ぶので速く
        out = np.frombuffer(pcm, dtype='<i2').reshape(-1, 2)[:, 0].astype(float)
        self.assertAlmostEqual(np.abs(out[:int(rate * 0.8)]).max(), 8000, delta=10)   # 小さい声は 2 倍
        self.assertLessEqual(np.abs(out).max(), 0.95 * 32767 + 1)                      # 大きい声は抑える
        self.assertGreater(np.abs(out[int(rate * 1.5):]).max(), 30000)                 # 小さくしすぎない


if __name__ == '__main__':
    unittest.main()
