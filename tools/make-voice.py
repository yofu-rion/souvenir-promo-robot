#!/usr/bin/env python3
"""VOICEVOX ENGINE で推薦の声（WAV）を作る。
  使い方: tools/make-voice.py -o robot/sounds/unit1.wav "こんにちは！…"
          tools/make-voice.py -o robot/sounds/unit1-full.wav --file sim/scripts/neji-choco.txt
          tools/make-voice.py --plan robot/scripts/neji-choco.json --out-dir robot/sounds/neji-choco
            （説明動作用。tools/script-plan.mjs が作った文の一覧から、1 文ずつ 01.wav, 02.wav … を作る。
              句の区切りの時刻を 01.json, 02.json … に書き、うなずきの時刻に使う）
          tools/make-voice.py --plan robot/scripts/neji-choco-duo.json --out-dir robot/sounds/neji-choco-duo --voices 1=3,2=8
            （2 基の掛け合い。基ごとに声を変える。そろって話す文は 2 人分を作って左右に分けた 1 本の WAV にまとめ、
              時刻は 01-1.json, 01-2.json のように基ごとに書く）
  エンジン: ~/opt/voicevox/run（起動していなければ、このスクリプトが起動して終わったら止める）
  声: 既定は「ずんだもん（ノーマル）」。--speaker で ID を変える（--list で一覧）
VOICEVOX の音声を公開・配布するときは、キャラクターの利用規約に従ってクレジット（例「VOICEVOX:ずんだもん」）を付ける。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave

ENGINE_DIR = os.path.expanduser('~/opt/voicevox')
URL = 'http://127.0.0.1:50021'
DEFAULT_SPEAKER = 3   # ずんだもん（ノーマル）
# VOICEVOX が読み違える語。声を作るときだけ置き換える（台本・画面の表示はそのまま）
READINGS = {'角打ち': 'かくうち'}


def call(path: str, data: bytes | None = None, timeout: float = 120):
    req = urllib.request.Request(URL + path, data=data, method='POST' if data is not None else 'GET',
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def engine_up() -> bool:
    try:
        call('/version', timeout=2)
        return True
    except OSError:
        return False


def start_engine() -> subprocess.Popen | None:
    if engine_up():
        return None
    run = os.path.join(ENGINE_DIR, 'run')
    if not os.path.exists(run):
        sys.exit(f'VOICEVOX ENGINE がない: {run}')
    proc = subprocess.Popen([run, '--host', '127.0.0.1', '--port', '50021'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        if engine_up():
            return proc
        time.sleep(1)
    proc.terminate()
    sys.exit('VOICEVOX ENGINE が起動しない')


def script_text(path: str) -> str:
    """シミュレータ用の台本（1 行 1 文、文頭の [お礼] や [V0.5 A0.2] は感情の指定）から本文だけを取り出す。"""
    with open(path, encoding='utf-8') as f:
        lines = [re.sub(r'^\s*\[[^\]]*\]\s*', '', l).strip() for l in f]
    return ''.join(l for l in lines if l)


def phrase_timing(q: dict) -> dict:
    """audio_query の結果から、アクセント句ごとの終わりの時刻（秒）と、その後の息継ぎの長さを出す。"""
    speed = q.get('speedScale', 1.0) or 1.0
    t = q.get('prePhonemeLength', 0.0)
    phrases = []
    for ap in q['accent_phrases']:
        t += sum((m.get('consonant_length') or 0.0) + m['vowel_length'] for m in ap['moras'])
        pause = ap['pause_mora']['vowel_length'] if ap.get('pause_mora') else 0.0
        phrases.append({'end': round(t / speed, 3), 'pause': round(pause / speed, 3)})
        t += pause
    t += q.get('postPhonemeLength', 0.0)
    return {'duration': round(t / speed, 3), 'phrases': phrases}


def reading(text: str) -> str:
    for word, kana in READINGS.items():
        text = text.replace(word, kana)
    return text


def synth(text: str, out: str, speaker: int, speed: float, timing: str | None = None) -> None:
    text = reading(text)
    q = json.loads(call(f'/audio_query?text={urllib.parse.quote(text)}&speaker={speaker}', b''))
    q['speedScale'] = speed
    wav = call(f'/synthesis?speaker={speaker}', json.dumps(q).encode())
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, 'wb') as f:
        f.write(wav)
    if timing:
        with open(timing, 'w', encoding='utf-8') as f:
            json.dump(phrase_timing(q), f)
    print(f'{out}: {len(wav) / 1024:.0f} KB（{len(text)} 字）')


def mix_stereo(parts: list[str], out: str) -> None:
    """2 本のモノラル WAV を、1 本目を左、2 本目を右にしたステレオ WAV にする（頭をそろえ、短い方は無音で埋める）。"""
    import numpy as np
    chans, rate = [], None
    for p in parts:
        with wave.open(p) as w:
            rate = w.getframerate()
            chans.append(np.frombuffer(w.readframes(w.getnframes()), dtype='<i2'))
    n = max(len(c) for c in chans)
    stereo = np.zeros((n, 2), dtype='<i2')
    for i, c in enumerate(chans):
        stereo[:len(c), i] = c
    with wave.open(out, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(stereo.tobytes())
    print(f'{out}: 左右にまとめた（{n / rate:.1f} 秒）')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('text', nargs='?')
    p.add_argument('--file', help='台本ファイル（1 行 1 文）')
    p.add_argument('-o', '--out', help='出力 WAV')
    p.add_argument('--speaker', type=int, default=DEFAULT_SPEAKER)
    p.add_argument('--speed', type=float, default=1.0, help='話す速さ（1.0 が標準）')
    p.add_argument('--plan', help='文の一覧（tools/script-plan.mjs の出力）。--out-dir に 1 文ずつ作る')
    p.add_argument('--out-dir', help='--plan の出力先')
    p.add_argument('--voices', help='基ごとの声の ID（例 1=3,2=8）。--plan の speakers に使う')
    p.add_argument('--list', action='store_true', help='使える声の一覧')
    a = p.parse_args()

    proc = start_engine()
    try:
        if a.list:
            for s in json.loads(call('/speakers')):
                print(s['name'], ' '.join(f'{st["name"]}={st["id"]}' for st in s['styles']))
            return
        if a.plan:
            if not a.out_dir:
                p.error('--plan には --out-dir を付ける')
            with open(a.plan, encoding='utf-8') as f:
                plan = json.load(f)
            voices = dict((int(k), int(v)) for k, v in (x.split('=') for x in a.voices.split(','))) if a.voices else {}
            for s in plan['sentences']:
                wav = os.path.join(a.out_dir, s['voice'])
                who = s.get('speakers', [1])
                if len(who) == 1:
                    synth(s['text'], wav, voices.get(who[0], a.speaker), a.speed,
                          timing=os.path.splitext(wav)[0] + '.json')
                    continue
                parts = []   # そろって話す：基ごとに作って、左右に分けた 1 本にまとめる
                for k in who:
                    part = f'{os.path.splitext(wav)[0]}-{k}.wav'
                    synth(s['text'], part, voices.get(k, a.speaker), a.speed, timing=os.path.splitext(part)[0] + '.json')
                    parts.append(part)
                mix_stereo(parts[:2], wav)
            return
        text = script_text(a.file) if a.file else a.text
        if not text or not a.out:
            p.error('文章（または --file）と -o を指定する')
        synth(text, a.out, a.speaker, a.speed)
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == '__main__':
    main()
