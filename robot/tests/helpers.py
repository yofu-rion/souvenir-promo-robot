"""テスト用のモックと設定。ハードがなくても状態機械まで通して動かす。"""
import copy
import logging
import os

import yaml

from srr import config as config_mod
from srr.audio import MockPlayer
from srr.controller import Controller, build_units

logging.disable(logging.CRITICAL)  # テスト中の状態遷移ログを出さない

ROBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(ROBOT_DIR, 'config.yaml'), encoding='utf-8') as f:
    BASE_RAW = yaml.safe_load(f)


def make_cfg(tmpdir, **patch):
    raw = copy.deepcopy(BASE_RAW)
    raw['state_file'] = os.path.join(tmpdir, 'pose.json')
    for u, ch in zip(raw['units'], ('left', 'right')):  # テストは 2 基構成で動かす（config.yaml で無効にした基も有効にする）
        u['enabled'] = True
        u['sound']['channel'] = ch   # テストは 1 基目＝左、2 基目＝右で確かめる（実機の配線とは別）
    raw['sequence']['mode'] = 'cycle'   # 既定は短い推薦。説明のテストは sequence__mode で変える
    raw['yaw']['present_deg'] = 45.0     # 通常のサイクルは「客側へ旋回」を確かめる（実機の config は正面＝0°）
    for path, value in patch.items():
        node = raw
        keys = path.split('__')
        for k in keys[:-1]:
            node = node[int(k)] if k.isdigit() else node[k]
        node[keys[-1]] = value
    return config_mod.parse(raw)


class FakeBus:
    """HX711Bus の代わり。grams[i] をそのまま生値にして返す（offset 0）。"""

    def __init__(self, cfg, grams):
        self.cfg, self.grams = cfg, list(grams)
        self.powered_down = False

    def ready(self):
        return True

    def read(self, timeout=0.5):
        return [int(g * u.scale.counts_per_g + u.scale.offset) for g, u in zip(self.grams, self.cfg.units)]

    def power_down(self):
        self.powered_down = True


def make_controller(cfg, grams=(200, 200), speech_s=2.0, use_bus=True, remote=None):
    player = MockPlayer(speech_s)
    units = build_units(cfg, player, backend='mock')
    bus = FakeBus(cfg, grams) if use_bus else None
    ctl = Controller(cfg, units, bus, player, remote)
    return ctl, units, bus, player


def run_for(ctl, seconds, dt=0.02, each=None):
    for _ in range(int(round(seconds / dt))):
        ctl.tick(dt)
        if each:
            each()
