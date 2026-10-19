"""音声の再生。WAV を読み、音量と左右（基ごとのスピーカー）を付けて aplay に流す。"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
import wave

import numpy as np

log = logging.getLogger(__name__)


def prepare(path: str, volume: float = 1.0, channel: str = 'both') -> tuple[bytes, int]:
    """16 bit WAV を読み、ステレオ 16 bit に直して音量と左右を掛ける。（PCM, サンプルレート）を返す。"""
    with wave.open(path, 'rb') as w:
        if w.getsampwidth() != 2:
            raise ValueError(f'{path}: 16 bit WAV にする')
        rate, nch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').reshape(-1, nch)
    mono = data.mean(axis=1) if nch > 1 else data[:, 0].astype(float)
    gain = max(0.0, min(1.0, volume))
    left = mono * gain * (channel in ('left', 'both'))
    right = mono * gain * (channel in ('right', 'both'))
    pcm = np.clip(np.stack([left, right], axis=1), -32768, 32767).astype('<i2')
    return pcm.tobytes(), rate


class Player:
    """aplay で 1 本ずつ再生する。2 基で共有するので、再生中に次は始めない。"""

    def __init__(self, device: str = 'default', fallback_s: float = 3.0):
        self.device, self.fallback_s = device, fallback_s
        self.proc: subprocess.Popen | None = None
        self.until = 0.0

    def play(self, path: str | None, volume: float = 1.0, channel: str = 'both') -> None:
        self.stop()
        try:
            pcm, rate = prepare(path, volume, channel)
        except (OSError, ValueError, TypeError, wave.Error) as e:
            log.warning('音声を再生できない（%s）。%.1f 秒待つだけにする', e, self.fallback_s)
            self.until = time.monotonic() + self.fallback_s
            return
        self.proc = subprocess.Popen(
            ['aplay', '-q', '-D', self.device, '-t', 'raw', '-f', 'S16_LE', '-c', '2', '-r', str(rate)],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # パイプへの書き込みは再生が進むまで詰まるので、制御ループを止めないよう別スレッドで流す
        threading.Thread(target=self._feed, args=(self.proc, pcm), daemon=True).start()

    @staticmethod
    def _feed(proc, pcm):
        try:
            proc.stdin.write(pcm)
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            log.warning('aplay が途中で終了した（デバイス %s を確認）', proc.args[3])

    @property
    def playing(self) -> bool:
        if self.proc is not None:
            if self.proc.poll() is None:
                return True
            self.proc = None
        return time.monotonic() < self.until

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        self.proc, self.until = None, 0.0


class MockPlayer:
    """テスト用。play してから duration 秒（step で進める）で終わる。"""

    def __init__(self, duration: float = 2.0):
        self.duration, self.remaining, self.played = duration, 0.0, []

    def play(self, path, volume=1.0, channel='both'):
        self.played.append((path, volume, channel))
        self.remaining = self.duration

    def step(self, dt):
        self.remaining = max(0.0, self.remaining - dt)

    @property
    def playing(self):
        return self.remaining > 0

    def stop(self):
        self.remaining = 0.0
