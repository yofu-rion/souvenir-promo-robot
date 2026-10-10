// 自己推薦ロボット（SRR）モーションの中核ロジック。
// ブラウザのシミュレータと Node のテストの両方から読み込む。DOM に依存しない。
//
// モーション形式:
// {
//   id, name, valence, arousal,          // 感情（ラッセルの円環モデル, -1..1）
//   duration,                             // 秒
//   loop,                                 // true なら繰り返し
//   tracks: { yaw: [[t, value, easing], ...], lift: [...], pitch: [...] },
//   speech, voice: { rate, pitch }        // 任意。読み上げ用
// }

export const AXIS_IDS = ['yaw', 'lift', 'pitch'];

// 実機の機構に合わせて変更する。vmax: 最大速度, amax: 最大加速度。
export const DEFAULT_AXES = {
  yaw:   { label: '回転', unit: '°',  min: -180, max: 180, neutral: 0,  vmax: 360, amax: 2400, enabled: true },
  lift:  { label: '昇降', unit: 'mm', min: 0,    max: 40,  neutral: 15, vmax: 200, amax: 2000, enabled: true },
  pitch: { label: '傾き', unit: '°',  min: -25,  max: 25,  neutral: 0,  vmax: 180, amax: 1800, enabled: false },
};

export const EASINGS = {
  linear: (u) => u,
  inOut: (u) => (u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2),
  in: (u) => u * u,
  out: (u) => 1 - (1 - u) * (1 - u),
  step: (u) => (u < 1 ? 0 : 1),
};

export const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

export function cloneAxes(axes = DEFAULT_AXES) {
  return JSON.parse(JSON.stringify(axes));
}

export function neutralPose(axes) {
  const pose = {};
  for (const id of AXIS_IDS) pose[id] = axes[id].neutral;
  return pose;
}

// キーフレーム列を時刻 t でサンプリングする。イージングは到達側キーの指定を使う。
export function sampleTrack(keys, t) {
  if (!keys || keys.length === 0) return null;
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    const [t1, v1, ease] = keys[i];
    if (t <= t1) {
      const [t0, v0] = keys[i - 1];
      const span = t1 - t0;
      const u = span > 0 ? (t - t0) / span : 1;
      const fn = EASINGS[ease] || EASINGS.inOut;
      return v0 + (v1 - v0) * fn(u);
    }
  }
  return keys[keys.length - 1][1];
}

// モーション時刻 t の指令値。intensity は中立姿勢からの振れ幅の倍率。
export function commandAt(motion, t, axes, { intensity = 1 } = {}) {
  const tt = motion.loop && motion.duration > 0 ? t % motion.duration : t;
  const pose = {};
  for (const id of AXIS_IDS) {
    const cfg = axes[id];
    if (!cfg.enabled) {
      pose[id] = cfg.neutral;
      continue;
    }
    const raw = sampleTrack(motion.tracks?.[id], tt);
    const v = raw == null ? cfg.neutral : cfg.neutral + (raw - cfg.neutral) * intensity;
    pose[id] = clamp(v, cfg.min, cfg.max);
  }
  return pose;
}

// 速度・加速度上限つきで目標値に追従する 1 軸アクチュエータの 1 ステップ。
export function stepAxis(state, target, cfg, dt) {
  const err = target - state.pos;
  const vDes = Math.sign(err) * Math.min(cfg.vmax, Math.sqrt(2 * cfg.amax * Math.abs(err)));
  const dvMax = cfg.amax * dt;
  state.vel += clamp(vDes - state.vel, -dvMax, dvMax);
  state.pos += state.vel * dt;
  if (Math.abs(target - state.pos) < 1e-3 && Math.abs(state.vel) < cfg.amax * dt) {
    state.pos = target;
    state.vel = 0;
  }
  if (state.pos <= cfg.min || state.pos >= cfg.max) {
    state.pos = clamp(state.pos, cfg.min, cfg.max);
    state.vel = 0;
  }
  return state;
}

// モーション全体を事前に計算する。指令値（cmd）と実機を模した応答（act）を返す。
// init には直前の姿勢 { yaw: {pos, vel}, ... } を渡すと、そこから滑らかに遷移する。
export function simulate(motion, axes, { intensity = 1, tempo = 1, dt = 0.01, init = null, settle = 0.4 } = {}) {
  const playDuration = motion.duration / tempo;
  const total = playDuration + (motion.loop ? 0 : settle);
  const steps = Math.max(1, Math.ceil(total / dt));
  const state = {};
  for (const id of AXIS_IDS) {
    const s = init?.[id];
    state[id] = { pos: s ? s.pos : axes[id].neutral, vel: s ? s.vel : 0 };
  }
  const t = new Float32Array(steps + 1);
  const cmd = {};
  const act = {};
  const vel = {};
  for (const id of AXIS_IDS) {
    cmd[id] = new Float32Array(steps + 1);
    act[id] = new Float32Array(steps + 1);
    vel[id] = new Float32Array(steps + 1);
  }
  for (let i = 0; i <= steps; i++) {
    const time = i * dt;
    t[i] = time;
    const c = commandAt(motion, Math.min(time, playDuration) * tempo, axes, { intensity });
    for (const id of AXIS_IDS) {
      if (i > 0) stepAxis(state[id], c[id], axes[id], dt);
      cmd[id][i] = c[id];
      act[id][i] = state[id].pos;
      vel[id][i] = state[id].vel;
    }
  }
  return { dt, duration: total, playDuration, t, cmd, act, vel, report: buildReport(cmd, act, axes, dt) };
}

// 実機で再現できるかの判定。指令速度が上限を超える、または追従誤差が大きい軸を警告する。
export function buildReport(cmd, act, axes, dt) {
  const axesReport = {};
  const warnings = [];
  for (const id of AXIS_IDS) {
    const cfg = axes[id];
    if (!cfg.enabled) {
      axesReport[id] = { enabled: false };
      continue;
    }
    let peakCmdVel = 0;
    let maxErr = 0;
    const c = cmd[id];
    const a = act[id];
    for (let i = 1; i < c.length; i++) {
      peakCmdVel = Math.max(peakCmdVel, Math.abs(c[i] - c[i - 1]) / dt);
      maxErr = Math.max(maxErr, Math.abs(c[i] - a[i]));
    }
    const range = cfg.max - cfg.min;
    axesReport[id] = { enabled: true, peakCmdVel, maxErr };
    if (peakCmdVel > cfg.vmax * 1.05) {
      warnings.push({ axis: id, kind: 'velocity', message: `${cfg.label}: 指令速度 ${fmt(peakCmdVel)} ${cfg.unit}/s が上限 ${cfg.vmax} ${cfg.unit}/s を超えています` });
    }
    if (maxErr > range * 0.1) {
      warnings.push({ axis: id, kind: 'tracking', message: `${cfg.label}: 追従誤差が最大 ${fmt(maxErr)} ${cfg.unit}（可動域の ${Math.round((maxErr / range) * 100)}%）` });
    }
  }
  return { axes: axesReport, warnings, ok: warnings.length === 0 };
}

const fmt = (x) => (Math.abs(x) >= 10 ? Math.round(x).toString() : x.toFixed(1));

// 感情価（valence）と覚醒度（arousal）からモーションを自動生成する。
// 快 → 高い位置・上向き・弾む、不快 → 低い位置・うつむく・背を向ける。
// 高覚醒 → 速く大きく、低覚醒 → ゆっくり小さく。不快かつ高覚醒 → 小刻みで硬い動き。
export function generateFromVA(valence, arousal, axes = DEFAULT_AXES, { duration = 3 } = {}) {
  const v = clamp(valence, -1, 1);
  const a = clamp(arousal, -1, 1);
  const ar = (a + 1) / 2;
  const tense = v < 0 && a > 0.2;
  const freq = 0.5 + 2.5 * ar;
  const half = 1 / (2 * freq);
  const ease = tense ? 'linear' : 'inOut';

  const n = { yaw: axes.yaw.neutral, lift: axes.lift.neutral, pitch: axes.pitch.neutral };
  const yawAmp = tense ? 4 + 10 * ar : 6 + 40 * ar * (0.6 + 0.4 * Math.max(v, 0));
  const yawOffset = v < 0 ? -30 * -v * (1 - ar) : 0;
  const liftSpan = axes.lift.max - axes.lift.min;
  const liftBase = n.lift + v * liftSpan * 0.35;
  const liftAmp = v >= 0 ? liftSpan * (0.05 + 0.25 * ar) : liftSpan * (0.02 + 0.08 * ar);
  const pitchBase = n.pitch + 14 * v;
  const pitchAmp = 2 + 6 * ar;

  // 速度・加速度上限内に収まるよう振れ幅を抑える。
  // inOut で距離 d を時間 half で動くと、ピーク速度 2d/half、ピーク加速度 4d/half²。
  const fit = (amp, cfg, swing) =>
    Math.min(amp, (0.9 * cfg.vmax * half) / (2 * swing), (0.8 * cfg.amax * half * half) / (4 * swing));
  const yawAmpFit = fit(yawAmp, axes.yaw, 2);
  const liftAmpFit = fit(liftAmp, axes.lift, 1.3);
  const pitchAmpFit = fit(pitchAmp, axes.pitch, 1);

  const attack = 0.3 + 0.4 * (1 - ar);
  const release = 0.4 + 0.4 * (1 - ar);
  const bodyEnd = duration - release;
  const tracks = { yaw: [[0, n.yaw]], lift: [[0, n.lift]], pitch: [[0, n.pitch]] };
  let i = 0;
  for (let time = attack; time <= bodyEnd + 1e-6; time += half, i++) {
    const s = i % 2 === 0 ? 1 : -1;
    const t = round3(time);
    tracks.yaw.push([t, round3(n.yaw + yawOffset + s * yawAmpFit), ease]);
    tracks.lift.push([t, round3(s > 0 ? liftBase + liftAmpFit : liftBase - liftAmpFit * 0.3), ease]);
    tracks.pitch.push([t, round3(pitchBase - s * pitchAmpFit * 0.5), ease]);
  }
  for (const id of AXIS_IDS) tracks[id].push([duration, n[id], 'inOut']);

  return {
    id: 'generated',
    name: `生成 V${signed(v)} A${signed(a)}`,
    valence: round3(v),
    arousal: round3(a),
    duration,
    loop: false,
    tracks,
    voice: { rate: round3(0.8 + 0.5 * ar), pitch: round3(1 + 0.4 * v) },
  };
}

const round3 = (x) => Math.round(x * 1000) / 1000;
const signed = (x) => (x >= 0 ? '+' : '') + x.toFixed(2);

// 読み込んだ JSON の検証。問題があればメッセージの配列を返す。
export function validateMotion(m) {
  const errors = [];
  if (!m || typeof m !== 'object') return ['モーションがオブジェクトではありません'];
  if (typeof m.name !== 'string' || !m.name) errors.push('name（名前）がありません');
  if (!(m.duration > 0)) errors.push('duration（秒）は正の数にしてください');
  if (!m.tracks || typeof m.tracks !== 'object') {
    errors.push('tracks がありません');
    return errors;
  }
  for (const [id, keys] of Object.entries(m.tracks)) {
    if (!AXIS_IDS.includes(id)) {
      errors.push(`未知の軸 "${id}"（使える軸: ${AXIS_IDS.join(', ')}）`);
      continue;
    }
    if (!Array.isArray(keys) || keys.length === 0) {
      errors.push(`${id}: キーフレームが空です`);
      continue;
    }
    let prev = -Infinity;
    keys.forEach((k, i) => {
      if (!Array.isArray(k) || typeof k[0] !== 'number' || typeof k[1] !== 'number') {
        errors.push(`${id}[${i}]: [時刻, 値, イージング] の形にしてください`);
        return;
      }
      if (k[0] < prev) errors.push(`${id}[${i}]: 時刻が前のキーより前です`);
      if (k[2] != null && !EASINGS[k[2]]) errors.push(`${id}[${i}]: 未知のイージング "${k[2]}"`);
      prev = k[0];
    });
  }
  return errors;
}

// 実機へ流すための等間隔サンプル（CSV）。act を使うので速度・加速度上限を満たす。
export function toCSV(sim, axes, hz = 50) {
  const ids = AXIS_IDS.filter((id) => axes[id].enabled);
  const lines = [['t_s', ...ids.map((id) => `${id}_${axes[id].unit === '°' ? 'deg' : axes[id].unit}`)].join(',')];
  const stride = Math.max(1, Math.round(1 / hz / sim.dt));
  for (let i = 0; i < sim.t.length; i += stride) {
    lines.push([sim.t[i].toFixed(3), ...ids.map((id) => sim.act[id][i].toFixed(2))].join(','));
  }
  return lines.join('\n');
}
