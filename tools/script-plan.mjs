#!/usr/bin/env node
// 台本（1 行 1 文、文頭の [お礼] や [V0.5 A0.2] で感情を指定）を、実機の説明動作用の JSON にする。
//   使い方: node tools/script-plan.mjs sim/scripts/neji-choco.txt > robot/scripts/neji-choco.json
// 2 基の掛け合いでは、行頭に話す基の番号を書く（「1: …」「2: …」、そろって話すなら「12: …」）。
// 番号のない行は 1 基目が話す。
// 感情の推定はシミュレータ（sim/motion-core.js の parseScript）と同じ。動きは実機側（robot/srr/explain.py）で作る。
import fs from 'node:fs';
import { parseScript } from '../sim/motion-core.js';

const src = process.argv[2];
if (!src) {
  console.error('使い方: node tools/script-plan.mjs 台本.txt > 出力.json');
  process.exit(1);
}
const presets = JSON.parse(fs.readFileSync(new URL('../sim/presets.json', import.meta.url), 'utf8')).motions;
const segments = [];
for (const line of fs.readFileSync(src, 'utf8').replace(/\r/g, '').split('\n')) {
  const m = line.match(/^\s*(\d+)\s*[:：]\s*(.*)$/);
  const speakers = m ? [...new Set(m[1].split('').map(Number))] : [1];
  for (const seg of parseScript(m ? m[2] : line, presets)) segments.push({ ...seg, speakers });
}
const plan = {
  source: src,
  sentences: segments.map((s, i) => ({
    n: i + 1,
    text: s.text,
    speakers: s.speakers,
    gesture: s.emotion.gesture,
    valence: s.emotion.v,
    arousal: s.emotion.a,
    voice: `${String(i + 1).padStart(2, '0')}.wav`,
  })),
};
for (const s of segments.filter((x) => x.tagError)) console.error(`読めないタグ: [${s.tag}] ${s.text}`);
process.stdout.write(JSON.stringify(plan, null, 2) + '\n');
