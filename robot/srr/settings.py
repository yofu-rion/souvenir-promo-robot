"""リモコンで変える値（音量・動きの大きさ・速さ・動作の間隔と、選んだ台本）。~/.local/state/srr-robot/settings.json に残す。"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULTS = {'volume': 80, 'motion': 100, 'tempo': 100}            # %
# motion と tempo は config の sequence.motion_base / tempo_base を 100% とした割合
RANGES = {'volume': (0, 200),   # 100% が声のファイルそのまま。上はリミッターで割れないように上げる
           'motion': (30, 150), 'tempo': (50, 130),
          'interval': (0, 600)}   # 秒。台本が終わってから次を始めるまで。なければ config の sequence.auto_interval_s
STEPS = {'volume': 5, 'motion': 10, 'tempo': 10, 'interval': 5}


def path_for(cfg) -> str:
    return os.path.join(os.path.dirname(cfg.state_file), 'settings.json')


def load(path: str) -> dict:
    values = dict(DEFAULTS)
    try:
        with open(path) as f:
            data = json.load(f)
        for k, (lo, hi) in RANGES.items():
            if k in data:
                values[k] = int(max(lo, min(hi, data[k])))
        if isinstance(data.get('program'), str):   # 選んだ台本の名前（sequence.programs の name）
            values['program'] = data['program']
        if isinstance(data.get('swap'), bool):   # キャラの配置を入れ替えているか
            values['swap'] = data['swap']
    except (OSError, ValueError, TypeError):
        pass
    return values


def save(path: str, values: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(values, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning('設定を保存できない: %s', e)


def clamp(key: str, value: int) -> int:
    lo, hi = RANGES[key]
    return int(max(lo, min(hi, value)))


def effective(cfg, values: dict) -> tuple[float, float]:
    """設定の % を、説明の組み立てに渡す倍率（動きの大きさ amp、速さ tempo）にする。"""
    return values['motion'] / 100 * cfg.motion_base, values['tempo'] / 100 * cfg.tempo_base
