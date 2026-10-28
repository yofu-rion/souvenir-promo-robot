#!/usr/bin/env python3
"""説明動作のダンス用の短い音楽（WAV）を作る。外部の音源を使わず、その場で合成する。
  使い方: tools/make-dance-music.py robot/sounds/dance.wav [--bpm 120] [--bars 4]
  中身: 1 拍目と 3 拍目にキック、裏拍にハイハット、小節ごとに明るい和音（C → G → Am → F）のアルペジオ。
  最後の小節の次の拍に決めの和音を 1 拍鳴らす（ダンスの決めポーズに合わせる）。
"""
import argparse
import wave

import numpy as np

RATE = 24000


def env(n, attack=0.005, release=0.1):
    t = np.arange(n) / RATE
    return np.minimum(1, t / attack) * np.exp(-t / release)


def kick(n):
    t = np.arange(n) / RATE
    f = 120 * np.exp(-t * 18) + 45
    return np.sin(2 * np.pi * np.cumsum(f) / RATE) * env(n, 0.002, 0.12)


def hat(n, rng):
    x = rng.standard_normal(n)
    x = np.diff(x, prepend=0)   # 高い音だけ残す
    return x * env(n, 0.001, 0.025) * 0.25


def pluck(freq, n):
    t = np.arange(n) / RATE
    tone = np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(4 * np.pi * freq * t) + 0.2 * np.sin(6 * np.pi * freq * t)
    return tone * env(n, 0.004, 0.18) * 0.18


CHORDS = [(261.6, 329.6, 392.0), (196.0, 246.9, 392.0), (220.0, 261.6, 329.6), (174.6, 220.0, 349.2)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('out')
    p.add_argument('--bpm', type=float, default=120.0)
    p.add_argument('--bars', type=int, default=4)
    a = p.parse_args()
    beat = 60.0 / a.bpm
    total = int(RATE * beat * (a.bars * 4 + 2))
    y = np.zeros(total)
    rng = np.random.default_rng(1)

    def add(sig, t):
        i = int(t * RATE)
        y[i:i + len(sig)] += sig[:max(0, total - i)]

    for b in range(a.bars * 4):
        t = b * beat
        if b % 2 == 0:
            add(kick(int(RATE * 0.3)), t)
        add(hat(int(RATE * 0.05), rng), t + beat / 2)
        chord = CHORDS[(b // 4) % len(CHORDS)]
        for k in range(2):   # 8 分音符のアルペジオ
            add(pluck(chord[(b * 2 + k) % 3] * 2, int(RATE * 0.3)), t + k * beat / 2)
    end = a.bars * 4 * beat   # 決めの和音
    add(kick(int(RATE * 0.3)), end)
    for f in CHORDS[0]:
        add(pluck(f * 2, int(RATE * 0.9)) * 1.6, end)
    y = y / np.max(np.abs(y)) * 0.8
    with wave.open(a.out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((y * 32767).astype('<i2').tobytes())
    print(f'{a.out}: {total / RATE:.1f} 秒（{a.bpm:g} BPM、{a.bars} 小節 + 決め）')


if __name__ == '__main__':
    main()
