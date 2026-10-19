#!/usr/bin/env python3
"""VOICEVOX ENGINE で推薦の声（WAV）を作る。
  使い方: tools/make-voice.py -o robot/sounds/unit1.wav "こんにちは！…"
          tools/make-voice.py -o robot/sounds/unit1-full.wav --file sim/scripts/neji-choco.txt
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

ENGINE_DIR = os.path.expanduser('~/opt/voicevox')
URL = 'http://127.0.0.1:50021'
DEFAULT_SPEAKER = 3   # ずんだもん（ノーマル）


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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('text', nargs='?')
    p.add_argument('--file', help='台本ファイル（1 行 1 文）')
    p.add_argument('-o', '--out', help='出力 WAV')
    p.add_argument('--speaker', type=int, default=DEFAULT_SPEAKER)
    p.add_argument('--speed', type=float, default=1.0, help='話す速さ（1.0 が標準）')
    p.add_argument('--list', action='store_true', help='使える声の一覧')
    a = p.parse_args()

    proc = start_engine()
    try:
        if a.list:
            for s in json.loads(call('/speakers')):
                print(s['name'], ' '.join(f'{st["name"]}={st["id"]}' for st in s['styles']))
            return
        text = script_text(a.file) if a.file else a.text
        if not text or not a.out:
            p.error('文章（または --file）と -o を指定する')
        q = json.loads(call(f'/audio_query?text={urllib.parse.quote(text)}&speaker={a.speaker}', b''))
        q['speedScale'] = a.speed
        wav = call(f'/synthesis?speaker={a.speaker}', json.dumps(q).encode())
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, 'wb') as f:
            f.write(wav)
        print(f'{a.out}: {len(wav) / 1024:.0f} KB（{len(text)} 字）')
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == '__main__':
    main()
