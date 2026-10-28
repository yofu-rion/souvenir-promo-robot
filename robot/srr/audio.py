"""音声の再生。WAV を読み、音量と左右（基ごとのスピーカー）を付けて aplay に流す。"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
import wave

import numpy as np

log = logging.getLogger(__name__)


MAX_VOLUME = 2.0          # 音量の上限（200%）。1 より上は limit() で割れないように抑える
LIMIT_LEVEL = 0.95 * 32767   # リミッターで抑える高さ
LIMIT_BLOCK = 128         # リミッターの区切り（サンプル）
LIMIT_RELEASE_S = 0.15    # 抑えた後、元の大きさへ戻るまでの時間


def limit(st: np.ndarray, rate: int) -> np.ndarray:
    """ピークリミッター。大きい音の手前から音量を下げ、LIMIT_LEVEL を超えないようにする（波形を切らない）。

    st は (サンプル数, 2) の float。短い区切りごとのピークから下げる量を決め、前後の区切りも見て早めに下げ、
    ゆっくり戻す。制御ループの中で呼ぶので、Python のループを使わずに計算する。
    """
    n = len(st)
    if n == 0:
        return st
    b = LIMIT_BLOCK
    peak = np.abs(st).max(axis=1)
    peak = np.pad(peak, (0, -n % b)).reshape(-1, b).max(axis=1)
    want = np.minimum(1.0, LIMIT_LEVEL / np.maximum(peak, 1.0))
    want = np.minimum(want, np.minimum(np.r_[want[1:], 1.0], np.r_[1.0, want[:-1]]))   # 前後の区切りも見る
    step = b / rate / LIMIT_RELEASE_S   # 1 区切りで戻せる量
    i = np.arange(len(want))
    gain = np.minimum(1.0, i * step + np.minimum.accumulate(want - i * step))   # 下げるのはすぐ、戻すのはゆっくり
    g = np.interp(np.arange(n), i * b + b / 2, gain)
    return st * g[:, None]


def prepare(path: str, volume: float = 1.0, channel: str = 'both') -> tuple[bytes, int]:
    """16 bit WAV を読み、ステレオ 16 bit に直して音量と左右を掛ける。（PCM, サンプルレート）を返す。

    channel: left / right / both（モノラルにして片側か両側へ）、stereo（左右をそのまま）、
    stereo-swap（左右を入れ替える。キャラの配置を入れ替えたとき）。
    volume が 1 より大きいときは、割れないようにリミッターを通す。
    """
    with wave.open(path, 'rb') as w:
        if w.getsampwidth() != 2:
            raise ValueError(f'{path}: 16 bit WAV にする')
        rate, nch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').reshape(-1, nch)
    gain = max(0.0, min(MAX_VOLUME, volume))
    if channel in ('stereo', 'stereo-swap'):   # 左右に別々の声が入った WAV（2 基がそろって話す）
        st = data[:, :2].astype(float) if nch > 1 else np.repeat(data[:, :1].astype(float), 2, axis=1)
        st = st * gain
        if channel == 'stereo-swap':
            st = st[:, ::-1]
    else:
        mono = data.mean(axis=1) if nch > 1 else data[:, 0].astype(float)
        st = np.stack([mono * gain * (channel in ('left', 'both')), mono * gain * (channel in ('right', 'both'))],
                      axis=1)
    if gain > 1.0:
        st = limit(st, rate)
    pcm = np.clip(st, -32768, 32767).astype('<i2')
    return pcm.tobytes(), rate


class Player:
    """aplay で 1 本ずつ再生する。2 基で共有するので、再生中に次は始めない。"""

    def __init__(self, device: str = 'default', fallback_s: float = 3.0):
        self.device, self.fallback_s = device, fallback_s
        self.proc: subprocess.Popen | None = None
        self.until = 0.0
        self.called_at = 0.0   # play() を呼んだ時刻（time.monotonic）
        self.master = 1.0      # 全体の音量（0〜2）。リモコンの「音量」で変える
        m = re.search(r'CARD=([^,]+)', device)
        self.proc_dir = f'/proc/asound/{m.group(1)}/pcm0p/sub0' if m else None

    def output_status(self) -> dict | None:
        """ALSA の記録から、実際に流れ始めた時刻（trigger、time.monotonic と同じ時計）と、送り出した秒数（pos）を返す。

        今の aplay が使っているときだけ返す。default（PipeWire 経由）など、読めないデバイスでは None。
        """
        if self.proc_dir is None or self.proc is None:
            return None
        try:
            with open(f'{self.proc_dir}/status') as f:
                st = dict(re.findall(r'^(\w+)\s*:\s*(\S+)', f.read(), re.M))
            with open(f'{self.proc_dir}/hw_params') as f:
                rate = int(re.search(r'^rate:\s*(\d+)', f.read(), re.M).group(1))
        except (OSError, AttributeError):
            return None
        if st.get('state') != 'RUNNING' or int(st.get('owner_pid', -1)) != self.proc.pid:
            return None
        return {'trigger': float(st['trigger_time']), 'pos': int(st['hw_ptr']) / rate}

    def play(self, path: str | None, volume: float = 1.0, channel: str = 'both') -> None:
        self.stop()
        self.called_at = time.monotonic()
        try:
            pcm, rate = prepare(path, volume * self.master, channel)
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
        self.master = 1.0

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
