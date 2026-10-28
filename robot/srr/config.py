"""config.yaml を読み、要件の上限を超える値を弾いて、各軸の可動範囲と速度を決める。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

ROBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROBOT_DIR, 'config.yaml')
CALIBRATION_NAME = 'calibration.yaml'  # config.yaml と同じ場所。角度調整で保存したパルス幅
PULSE_KEYS = ('pulse_min_us', 'pulse_center_us', 'pulse_max_us')

# 要件「性能・制約要件」の上限。設定でこれを超えたらエラーにする
MAX_TABLE_SPEED_DEG_S = 60.0
MIN_LIFT_STROKE_S = 1.0
PULSE_SAFE_US = (400, 2600)


class ConfigError(ValueError):
    pass


@dataclass
class ServoConfig:
    gpio: int
    pulse_min_us: float
    pulse_center_us: float
    pulse_max_us: float
    invert: bool = False
    pca9685_channel: int | None = None


@dataclass
class AxisLimits:
    """論理角（昇降：ホーン角、ヨー：テーブル角）での可動範囲と速度。"""
    lo: float
    hi: float
    home: float
    vmax: float
    amax: float

    def clamp(self, x: float) -> float:
        return min(self.hi, max(self.lo, x))


@dataclass
class ScaleConfig:
    dt_gpio: int
    offset: float
    counts_per_g: float
    present_g: float


@dataclass
class SoundConfig:
    file: str | None
    volume: float = 1.0
    channel: str = 'both'


@dataclass
class ExplainConfig:
    plan: str      # tools/script-plan.mjs が作った文の一覧（JSON）
    voices: str    # 文ごとの声（01.wav, 02.wav …）のフォルダ
    dance: str | None = None   # 喜びの文の後に流すダンスの音楽（tools/make-dance-music.py）
    name: str = ''             # 台本の名前（リモコンで選ぶときに出す）
    cast: list[str] = field(default_factory=list)   # 話者 1, 2 のキャラの名前（リモコンの画面に出す）


@dataclass
class UnitConfig:
    index: int      # 有効な基の中での位置（HX711 の DT の並び）
    number: int     # 設定ファイルでの番号（1 から）。CLI で基を選ぶときに使う
    name: str
    product: str
    lift: ServoConfig
    yaw: ServoConfig
    scale: ScaleConfig
    sound: SoundConfig
    explain: ExplainConfig | None = None
    present_deg: float | None = None   # この基の客側の向き（テーブル角）。なければ yaw.present_deg
    partner_side: int = 1              # ヨーの＋が相手の基の方なら 1、−が相手の方なら −1


@dataclass
class Config:
    servo_range_deg: float
    pwm_backend: str
    pwm_frequency_hz: float
    pca9685_address: int
    lift: AxisLimits
    yaw: AxisLimits
    yaw_gear_ratio: float
    yaw_present_deg: float
    units: list[UnitConfig]
    hx711_sck_gpio: int
    hx711_enabled: bool
    average_window: int
    absent_hold_s: float
    return_hold_s: float
    surge_g: float
    auto_interval_s: float
    sequence_mode: str
    hold_top_s: float
    after_speech_s: float
    homing_gap_s: float
    homing_speed_scale: float
    audio_device: str
    speech_fallback_s: float
    state_file: str
    remote_enabled: bool = False
    remote_name: str = 'SRR-Remote'
    remote_start_paused: bool = True
    remote_transport: str = 'ble'   # ble（Bluetooth LE）／serial（USB ケーブル）
    remote_serial_port: str = '/dev/ttyACM0'
    calibration_file: str = ''
    programs: list[ExplainConfig] = field(default_factory=list)   # リモコンで選ぶ 2 基の台本（sequence.programs）
    motion_base: float = 1.0   # リモコンの「動きの大きさ 100%」にあたる倍率
    tempo_base: float = 1.0    # リモコンの「速さ 100%」にあたる倍率（最高速度は要件の上限のまま、加速度にかかる）
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def servo_half_range(self) -> float:
        return self.servo_range_deg / 2

    @property
    def duo(self) -> ExplainConfig | None:
        """最初の台本（台本を選んでいないときに使う）。"""
        return self.programs[0] if self.programs else None


def _servo(d: dict, where: str, default_channel: int) -> ServoConfig:
    try:
        s = ServoConfig(
            gpio=int(d['gpio']),
            pulse_min_us=float(d['pulse_min_us']),
            pulse_center_us=float(d['pulse_center_us']),
            pulse_max_us=float(d['pulse_max_us']),
            invert=bool(d.get('invert', False)),
            pca9685_channel=int(d.get('pca9685_channel', default_channel)),
        )
    except KeyError as e:
        raise ConfigError(f'{where}: {e.args[0]} がない') from None
    lo, hi = PULSE_SAFE_US
    if not (lo <= s.pulse_min_us < s.pulse_center_us < s.pulse_max_us <= hi):
        raise ConfigError(f'{where}: パルス幅は {lo} ≤ min < center < max ≤ {hi} µs にする')
    return s


def _explain(d: dict | None, base_dir: str, name: str = '') -> ExplainConfig | None:
    if not d:
        return None
    return ExplainConfig(plan=os.path.join(base_dir, d['plan']), voices=os.path.join(base_dir, d['voices']),
                         dance=os.path.join(base_dir, d['dance']) if d.get('dance') else None,
                         name=str(d.get('name') or name), cast=[str(c) for c in d.get('cast') or []])


def _programs(sq: dict, base_dir: str) -> list[ExplainConfig]:
    """sequence.programs（台本の一覧）。古い設定の sequence.duo だけなら、それを 1 本目にする。"""
    items = sq.get('programs')
    if items is None:
        items = [sq['duo']] if sq.get('duo') else []
    progs = [_explain(d, base_dir, name=f'台本{i + 1}') for i, d in enumerate(items)]
    names = [p.name for p in progs]
    if len(set(names)) != len(names):
        raise ConfigError('sequence.programs の name が重複している')
    return progs


def _transport(t: str) -> str:
    if t not in ('ble', 'serial'):
        raise ConfigError('remote.transport は ble か serial')
    return t


def _mode(m: str) -> str:
    if m not in ('cycle', 'explain'):
        raise ConfigError('sequence.mode は cycle か explain')
    return m


def parse(raw: dict, base_dir: str = ROBOT_DIR, calibration: dict | None = None) -> Config:
    """calibration（calibration.yaml の中身）があれば、基ごとのパルス幅をそれで上書きする。"""
    cal_units = (calibration or {}).get('units') or {}
    rng = float(raw.get('servo_range_deg', 270))
    if rng not in (180.0, 270.0):
        raise ConfigError('servo_range_deg は 270 か 180')
    half = rng / 2

    lf = raw['lift']
    bottom, top, margin = float(lf['bottom_deg']), float(lf['top_deg']), float(lf.get('margin_deg', 1.0))
    stroke = abs(top - bottom)
    if float(lf['min_stroke_s']) < MIN_LIFT_STROKE_S:
        raise ConfigError(f'lift.min_stroke_s は {MIN_LIFT_STROKE_S} s 以上（要件）')
    if max(abs(bottom), abs(top)) > half:
        raise ConfigError('昇降の範囲がサーボの可動域を超える')
    lift = AxisLimits(
        lo=min(bottom, top) + margin, hi=max(bottom, top) - margin,
        home=bottom + (margin if top > bottom else -margin),
        vmax=stroke / float(lf['min_stroke_s']), amax=float(lf['accel_deg_s2']),
    )

    yw = raw['yaw']
    ratio = float(yw.get('gear_ratio', -1.5))
    speed = float(yw['max_speed_deg_s'])
    if speed > MAX_TABLE_SPEED_DEG_S:
        raise ConfigError(f'yaw.max_speed_deg_s は {MAX_TABLE_SPEED_DEG_S}°/s 以下（要件）')
    # 180° 版のサーボならテーブル角は ±90/1.5 = ±60° に絞られる
    table_limit = min(float(yw['table_limit_deg']), half / abs(ratio))
    yaw = AxisLimits(lo=-table_limit, hi=table_limit, home=0.0, vmax=speed, amax=float(yw['accel_deg_s2']))
    present = float(yw.get('present_deg', 45.0))

    units = []
    for i, u in enumerate(raw['units']):
        # enabled: false の基は読み飛ばす（配線前の 2 基目など）
        if not u.get('enabled', True):
            continue
        name = u.get('name', f'unit{i + 1}')
        sc, so = u['scale'], u.get('sound', {})
        f = so.get('file')
        cal = cal_units.get(name) or {}
        lift_raw = {**u['lift'], **{k: v for k, v in (cal.get('lift') or {}).items() if k in PULSE_KEYS}}
        yaw_raw = {**u['yaw'], **{k: v for k, v in (cal.get('yaw') or {}).items() if k in PULSE_KEYS}}
        units.append(UnitConfig(
            index=len(units), number=i + 1, name=name, product=u.get('product', ''),
            lift=_servo(lift_raw, f'{name}.lift', 2 * i),
            yaw=_servo(yaw_raw, f'{name}.yaw', 2 * i + 1),
            scale=ScaleConfig(dt_gpio=int(sc['dt_gpio']), offset=float(sc.get('offset', 0)),
                              counts_per_g=float(sc['counts_per_g']), present_g=float(sc['present_g'])),
            sound=SoundConfig(file=os.path.join(base_dir, f) if f else None,
                              volume=float(so.get('volume', 1.0)), channel=so.get('channel', 'both')),
            explain=ExplainConfig(plan=os.path.join(base_dir, u['explain']['plan']),
                                  voices=os.path.join(base_dir, u['explain']['voices']),
                                  dance=os.path.join(base_dir, u['explain']['dance']) if u['explain'].get('dance') else None,
                                  ) if u.get('explain') else None,
            present_deg=float(u['present_deg']) if u.get('present_deg') is not None else None,
            partner_side=-1 if int(u.get('partner_side', 1)) < 0 else 1,
        ))
        if units[-1].sound.channel not in ('left', 'right', 'both'):
            raise ConfigError(f'{name}.sound.channel は left / right / both')
    if not units:
        raise ConfigError('有効な基がない（units の enabled を確認）')
    gpios = [g for u in units for g in (u.lift.gpio, u.yaw.gpio)]
    if len(set(gpios)) != len(gpios):
        raise ConfigError('サーボの GPIO が重複している')

    hx, sq, au, pw = raw['hx711'], raw['sequence'], raw.get('audio', {}), raw.get('pwm', {})
    rm = raw.get('remote') or {}
    return Config(
        servo_range_deg=rng,
        pwm_backend=pw.get('backend', 'sysfs'),
        pwm_frequency_hz=float(pw.get('frequency_hz', 50)),
        pca9685_address=int(pw.get('pca9685_address', 0x40)),
        lift=lift, yaw=yaw, yaw_gear_ratio=ratio, yaw_present_deg=yaw.clamp(present),
        units=units,
        hx711_sck_gpio=int(hx['sck_gpio']),
        hx711_enabled=bool(hx.get('enabled', True)),
        average_window=int(hx.get('average_window', 8)),
        absent_hold_s=float(hx.get('absent_hold_s', 0.5)),
        return_hold_s=float(hx.get('return_hold_s', 2.0)),
        surge_g=float(hx.get('surge_g', 300.0)),
        auto_interval_s=float(sq['auto_interval_s']),
        sequence_mode=_mode(sq.get('mode', 'cycle')),
        programs=_programs(sq, base_dir),
        motion_base=float(sq.get('motion_base', 1.0)),
        tempo_base=float(sq.get('tempo_base', 1.0)),
        hold_top_s=float(sq.get('hold_top_s', 0.5)),
        after_speech_s=float(sq.get('after_speech_s', 0.5)),
        homing_gap_s=float(sq.get('homing_gap_s', 1.0)),
        homing_speed_scale=float(sq.get('homing_speed_scale', 0.3)),
        audio_device=au.get('device', 'default'),
        speech_fallback_s=float(au.get('speech_fallback_s', 3.0)),
        state_file=os.path.expanduser(raw.get('state_file', '~/.local/state/srr-robot/pose.json')),
        remote_enabled=bool(rm.get('enabled', False)),
        remote_name=str(rm.get('name', 'SRR-Remote')),
        remote_start_paused=bool(rm.get('start_paused', True)),
        remote_transport=_transport(rm.get('transport', 'ble')),
        remote_serial_port=str(rm.get('serial_port', '/dev/ttyACM0')),
        calibration_file=os.path.join(base_dir, CALIBRATION_NAME),
        raw=raw,
    )


def load(path: str | None = None) -> Config:
    path = path or os.environ.get('SRR_CONFIG') or DEFAULT_CONFIG
    with open(path, encoding='utf-8') as f:
        raw = yaml.safe_load(f)
    base = os.path.dirname(os.path.abspath(path))
    calibration = None
    try:
        with open(os.path.join(base, CALIBRATION_NAME), encoding='utf-8') as f:
            calibration = yaml.safe_load(f)
    except FileNotFoundError:
        pass
    return parse(raw, base_dir=base, calibration=calibration)
