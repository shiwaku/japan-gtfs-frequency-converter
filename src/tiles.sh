#!/usr/bin/env bash
# 集計結果 (NDJSON) を PMTiles にする。
#
#   src/tiles.sh [build_dir]
#
# 最小ズームは aggregate.py が地物ごとに指定済み（便数の多い幹線から先に現れる）。
# ここで --drop-densest-as-needed を付けないのは、密度だけで間引かれると
# 全国ズームの絵が「たまたま残った区間」になってしまうため。
# 地物数は区間8.6万・停留所7.4万で、間引きなしでもタイル上限に収まる。
#
# 線の簡略化は tippecanoe に任せる（最大ズームでは元の形のまま）。区間が
# shapes.txt 由来の折れ線になり頂点が 17万→40万 に増えたので、低ズームで
# 頂点を落とさないとタイルが膨らむ。

set -euo pipefail

BUILD="${1:-build}"
ROUTES="$BUILD/routes.geojsonl"
STOPS="$BUILD/stops.geojsonl"
OUT="$BUILD/bus_frequency.pmtiles"

for f in "$ROUTES" "$STOPS"; do
  [ -s "$f" ] || { echo "ERROR: $f がありません。先に src/aggregate.py を実行してください。" >&2; exit 1; }
done
command -v tippecanoe >/dev/null || { echo "ERROR: tippecanoe が見つかりません。" >&2; exit 1; }

echo "入力: $(wc -l < "$ROUTES") 区間 / $(wc -l < "$STOPS") 停留所"

tippecanoe \
  -o "$OUT" \
  --force \
  --maximum-zoom=14 \
  --minimum-zoom=4 \
  --named-layer="routes:$ROUTES" \
  --named-layer="stops:$STOPS" \
  --preserve-input-order \
  --attribution='<a href="https://gtfs-data.jp/">GTFSデータリポジトリ</a> | <a href="https://ckan.odpt.org/">公共交通オープンデータセンター</a>' \
  --name="全国バス運行頻度図" \
  --description="gtfs-data.jp と ODPT の GTFS から集計した平日の運行頻度"

echo
ls -lh "$OUT"
pmtiles show "$OUT" 2>/dev/null | head -20 || true
