#!/usr/bin/env bash
# 動画からモーション解析用のコマ画像とコンタクトシートを作る。
#   使い方: tools/video-frames.sh 動画ファイル [出力先] [fps]
#   出力:   出力先/frames/000001.jpg …（タイムスタンプ入り）、出力先/sheet-*.jpg（5x4 の一覧）
# 一覧を見て、回転角・昇降量・傾きの変化と時刻をキーフレームに起こす（docs/research/srr-motion-catalog.md 参照）。
set -euo pipefail
src="${1:?動画ファイルを指定してください}"
out="${2:-video-frames}"
fps="${3:-5}"
mkdir -p "$out/frames"
ffmpeg -hide_banner -loglevel error -i "$src" \
  -vf "fps=${fps},scale=480:-2,drawtext=text='%{pts\:hms}':x=8:y=8:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.6" \
  "$out/frames/%06d.jpg"
ffmpeg -hide_banner -loglevel error -pattern_type glob -i "$out/frames/*.jpg" \
  -vf "tile=5x4:padding=4:margin=4" "$out/sheet-%03d.jpg"
echo "コマ: $(ls "$out/frames" | wc -l) 枚 → $out/frames"
echo "一覧: $(ls "$out"/sheet-*.jpg | wc -l) 枚 → $out/sheet-*.jpg"
