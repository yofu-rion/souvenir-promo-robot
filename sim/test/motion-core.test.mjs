import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  AXIS_IDS, cloneAxes, sampleTrack, commandAt, simulate, generateFromVA, validateMotion, toCSV,
} from '../motion-core.js';

const presets = JSON.parse(fs.readFileSync(new URL('../presets.json', import.meta.url), 'utf8'));
const allAxes = () => {
  const axes = cloneAxes();
  axes.pitch.enabled = true;
  return axes;
};

test('sampleTrack はキーの間を補間し、範囲外では端の値を返す', () => {
  const keys = [[0, 0], [1, 10, 'linear'], [2, 10]];
  assert.equal(sampleTrack(keys, -1), 0);
  assert.equal(sampleTrack(keys, 0.5), 5);
  assert.equal(sampleTrack(keys, 3), 10);
  assert.equal(sampleTrack([], 1), null);
});

test('commandAt は無効な軸を中立に、値を可動域内に収める', () => {
  const axes = cloneAxes();
  const m = { duration: 1, tracks: { lift: [[0, 999]], pitch: [[0, 20]] } };
  const pose = commandAt(m, 0, axes);
  assert.equal(pose.lift, axes.lift.max);
  assert.equal(pose.pitch, axes.pitch.neutral);
  assert.equal(pose.yaw, axes.yaw.neutral);
});

test('simulate の応答は速度上限を超えない', () => {
  const axes = allAxes();
  const step = { name: 'step', duration: 1, tracks: { yaw: [[0, 0], [0.01, 180, 'step']] } };
  const sim = simulate(step, axes);
  const maxVel = Math.max(...sim.vel.yaw.map(Math.abs));
  assert.ok(maxVel <= axes.yaw.vmax + 1e-6, `max vel ${maxVel}`);
  assert.equal(sim.report.ok, false);
});

test('同梱プリセットはすべて妥当で、既定の軸設定で警告が出ない', () => {
  const axes = allAxes();
  for (const m of presets.motions) {
    assert.deepEqual(validateMotion(m), [], m.id);
    assert.deepEqual(simulate(m, axes).report.warnings, [], m.id);
  }
  for (const id of Object.values(presets.events)) {
    assert.ok(presets.motions.some((m) => m.id === id), `event target ${id}`);
  }
});

test('generateFromVA は感情空間全体で実機の上限内に収まる', () => {
  const axes = allAxes();
  for (let v = -1; v <= 1.001; v += 0.25) {
    for (let a = -1; a <= 1.001; a += 0.25) {
      const m = generateFromVA(v, a, axes);
      assert.deepEqual(validateMotion(m), []);
      assert.deepEqual(simulate(m, axes).report.warnings, [], `V${v} A${a}`);
    }
  }
});

test('generateFromVA は快で高く、不快で低い位置をとる', () => {
  const axes = allAxes();
  const mean = (m) => {
    const sim = simulate(m, axes);
    return sim.act.lift.reduce((s, x) => s + x, 0) / sim.act.lift.length;
  };
  assert.ok(mean(generateFromVA(0.8, 0, axes)) > mean(generateFromVA(-0.8, 0, axes)));
});

test('validateMotion は不正な入力を指摘する', () => {
  const errors = validateMotion({ name: 'x', duration: 1, tracks: { roll: [[0, 0]], yaw: [[1, 0], [0, 1, 'bounce']] } });
  assert.equal(errors.length, 3);
});

test('toCSV は有効な軸だけを指定周波数で出力する', () => {
  const axes = cloneAxes();
  const sim = simulate(presets.motions[0], axes);
  const lines = toCSV(sim, axes, 50).split('\n');
  assert.equal(lines[0], 't_s,yaw_deg,lift_mm');
  assert.equal(lines[1].split(',').length, 3);
  assert.equal(lines[2].split(',')[0], '0.020');
  assert.ok(AXIS_IDS.length === 3);
});
