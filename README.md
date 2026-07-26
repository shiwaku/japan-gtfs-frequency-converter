# japan-gtfs-frequency-converter

全国のバス GTFS から**区間ごとの運行本数**を集計し、1枚の PMTiles にするツール群です。

**地図: https://shiwaku.github.io/japan-gtfs-frequency-converter/**

[gtfs-data.jp（GTFSデータリポジトリ）](https://gtfs-data.jp/) の全フィードを取得 → 平日1日の
便数を停留所間の区間単位で集計 → tippecanoe でベクタータイル化、までを3つのスクリプトで行います。

生成物の `build/bus_frequency.pmtiles` はこのリポジトリに含めてあるので、clone すればすぐ地図が見られます。

表示だけを試したい場合は [japan-gtfs-frequency-viewer](https://github.com/shiwaku/japan-gtfs-frequency-viewer)
（ブラウザ内で GTFS を処理するビューア）もあわせてどうぞ。こちらは「全国分を事前に焼く」側の実装です。

## 成果物

`build/bus_frequency.pmtiles` — 543フィード / 62,391区間 / 54,467停留所、19MB、z4–14。

| レイヤー | 地物 | 主なプロパティ |
|---|---|---|
| `routes` | 停留所間の区間（LineString） | `frequency`（両方向計の便数/日）、`frequency_ab` / `frequency_ba`（方向別）、`stop_a` / `stop_b`、`agency`、`operators`（事業者数）、`routes`（系統数）、`pref` |
| `stops` | 停留所（Point） | `name`、`count`（延べ停車回数/日）、`agency`、`operators`、`pref` |

便数の分布は 1–5便:25% / 6–11:33% / 12–23:26% / 24–47:11% / 48–95:3.6% / 96便以上:1.3%。
階級を切るならこのあたりが目安です。

## 使い方

必要なもの: Python 3.10+（標準ライブラリのみ）、[tippecanoe](https://github.com/felt/tippecanoe)、
確認用に [pmtiles CLI](https://github.com/protomaps/go-pmtiles)。

```sh
python3 src/fetch.py              # gtfs-data.jp から全フィード取得 → data/
python3 src/aggregate.py --jobs 4 # 集計 → build/routes.geojsonl, build/stops.geojsonl
bash src/tiles.sh                 # → build/bus_frequency.pmtiles
```

ビューア（PMTiles は HTTP Range を使うので、`file://` では開けません。同梱のサーバ経由で）:

```sh
python3 src/serve.py              # → http://127.0.0.1:8787/
```

`index.html` が GitHub Pages のトップページを兼ねているので、ローカルで見えるものと
公開されているものは同一です。

### スクリプト

| ファイル | 役割 |
|---|---|
| `src/fetch.py` | フィード取得。`gtfs_file_uid` を前回の manifest と比較して**変わったものだけ**再取得する。廃止フィードの除外と ZIP の健全性検証つき |
| `src/aggregate.py` | 集計本体。`--jobs` で並列。`build/aggregate_report.json` にフィードごとの採用日・件数・スキップ理由を出力 |
| `src/tiles.sh` | tippecanoe 呼び出し。z4–14 |
| `index.html` | MapLibre ビューア。ホバーで区間の方向別内訳が出る。GitHub Pages のトップページ |
| `src/serve.py` | Range リクエストに応答する最小のローカルサーバ（`index.html` の確認用） |
| `src/jpholidays.py` | 祝日判定（平日ダイヤの選択に使う） |

## 集計の仕様

全国・多フィードを1枚の地図にまとめる都合で、単一フィードを扱う一般的な集計とは
いくつか異なる判断をしています。

**対象日はフィードごとに自動で選ぶ。** 543フィードは有効期間がばらばらで、全国共通の1日を
指定するとその日が期間外のフィードが丸ごと欠落します。各フィードについて「有効期間内・平日
（祝日を除く）・実際に便がある」最初の日を選びます。採用日は `aggregate_report.json` に記録。

**事業者をまたぐ同一停留所をまとめる。** フィードは事業者ごとに分かれているため、同じ区間が
事業者ごとに別地物になります。例えば熊本の「市役所前（熊本）⇄桜町バスターミナル」は九州産交バス
951便と熊本都市バス681便が別地物で、地図上では太い方しか見えず実際の1,632便が過小に見えます。
同名かつ50m以内の停留所を同一とみなして合算します（`--no-merge-operators` で無効、
`--merge-threshold` で距離変更）。

**区間は既定で無向・系統統合。** A→B と B→A は完全に重なるので、方向別に持つと地物数が倍になる
だけです。合算値を `frequency`、方向別を `frequency_ab` / `frequency_ba` として持ちます
（`--directional` / `--by-route` で分離可）。

**長すぎる区間は落とす。** 区間は停留所どうしの直線なので、途中停車のない高速バスは
「バスタ新宿→徳島駅前」が502kmの直線として地図を横切ります。区間長は中央値0.42km、30km超は
39本（全体の0.06%）でいずれも1〜2便の高速バスでした。既定で除外します
（`--max-segment-km 0` で無効）。

**最小ズームは地物ごとに指定する。** tippecanoe の `--drop-densest-as-needed` に任せると密度だけで
間引かれ、全国ズームで「たまたま残った区間」が見える絵になります。便数の多い幹線から順に現れるよう
明示し、各ズームで見える区間数を z4:797 / z5:2,917 / z6:9,537 / z7:26,487 / z8:46,799 / z9以降:全件
としています。

**線幅や色はタイルに焼き込まない。** MapLibre の式で `frequency` から描けば、スタイル調整だけで
見た目を変えられます。

## 既知の制約

- **東京23区がほぼ空白。** 都営バスや大手私鉄系バスは gtfs-data.jp に無く、
  [ODPT](https://developer.odpt.org/) 側にあります。第2のソースとして追加するのが今後の課題です。
- **全国ズーム（z4–5）は点の散布に見える。** 1区間が数百mなので、線として読ませるには
  同頻度の連続区間を1本のポリラインに結合する処理が要ります。
- **平日1日のみ。** 土休日ダイヤや時間帯別（`--begin-time` / `--end-time` はあるが未検証）は
  現状スコープ外です。
- 543フィード中、集計できたのは513件。29件はバス以外のモードや平日運行日なしでスキップ、
  1件（瑞穂町デマンド交通）は `stop_times.txt` に `stop_id` 列が無くエラーです。

## ライセンスと出典

- コード: MIT License（`LICENSE`）
- データ: [GTFSデータリポジトリ](https://gtfs-data.jp/)（フィードごとのライセンスは
  `data/manifest.json` および `build/aggregate_report.json` の `license` を参照。多くは CC BY 4.0）
- `build/bus_frequency.pmtiles` は上記データの派生物です。利用時は出典を表示してください。
