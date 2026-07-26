# japan-gtfs-frequency-converter

全国のバス GTFS から**区間ごとの運行本数**を集計し、1枚の PMTiles にするツール群です。

**地図: https://shiwaku.github.io/japan-gtfs-frequency-converter/**

[gtfs-data.jp（GTFSデータリポジトリ）](https://gtfs-data.jp/) と
[公共交通オープンデータセンター（ODPT）](https://ckan.odpt.org/) の両方からフィードを取得 →
平日1日の便数を停留所間の区間単位で集計 → tippecanoe でベクタータイル化、までを行います。

生成物の `build/bus_frequency.pmtiles` はこのリポジトリに含めてあるので、clone すればすぐ地図が見られます。

表示だけを試したい場合は [japan-gtfs-frequency-viewer](https://github.com/shiwaku/japan-gtfs-frequency-viewer)
（ブラウザ内で GTFS を処理するビューア）もあわせてどうぞ。こちらは「全国分を事前に焼く」側の実装です。

## 成果物

`build/bus_frequency.pmtiles` — 648フィード / 85,553区間 / 73,999停留所、31MB、z4–14。

| レイヤー | 地物 | 主なプロパティ |
|---|---|---|
| `routes` | 停留所間の区間（LineString） | `frequency`（両方向計の便数/日）、`frequency_ab` / `frequency_ba`（方向別）、`stop_a` / `stop_b`、`agency`、`operators`（事業者数）、`routes`（系統数）、`pref` |
| `stops` | 停留所（Point） | `name`、`count`（延べ停車回数/日）、`agency`、`operators`、`pref` |

便数の分布は 1–5便:21% / 6–11:27% / 12–23:24% / 24–47:14% / 48–95:6.8% / 96便以上:6.9%。
階級を切るならこのあたりが目安です。

### データソース

| ソース | フィード | 取得 |
|---|---|---|
| [gtfs-data.jp](https://gtfs-data.jp/) | 543 | `src/fetch.py`。API で全件取得できる |
| [ODPT](https://ckan.odpt.org/) | 105 | `src/fetch_odpt.py`。都営バス・京王バス・横浜市営バスなど大都市圏の主要事業者はこちらにしかない |

集計できたのは 565 フィード。69 はバス以外のモード等でスキップ、13 はソース間の重複、
1 はフィード側の不備（`stop_times.txt` に `stop_id` 列が無い）です。

## 使い方

必要なもの: Python 3.10+（標準ライブラリのみ）、[tippecanoe](https://github.com/felt/tippecanoe)、
確認用に [pmtiles CLI](https://github.com/protomaps/go-pmtiles)。

```sh
python3 src/fetch.py              # gtfs-data.jp から取得 → data/feeds/
python3 src/fetch_odpt.py         # ODPT から取得       → data/feeds_odpt/
python3 src/aggregate.py --jobs 4 # 集計 → build/routes.geojsonl, build/stops.geojsonl
bash src/tiles.sh                 # → build/bus_frequency.pmtiles
```

ODPT の 108 データセットのうち 60 は取得にアクセストークンが要ります
（[developer.odpt.org](https://developer.odpt.org/) で無料登録）。`--token`、環境変数
`ODPT_ACCESS_TOKEN`、`~/.odpt_token` のいずれかで渡します。無くても残り 48（都営バスを含む）は
取得でき、`src/aggregate.py --no-odpt` で ODPT 自体を外すこともできます。

ビューア（PMTiles は HTTP Range を使うので、`file://` では開けません。同梱のサーバ経由で）:

```sh
python3 src/serve.py              # → http://127.0.0.1:8787/
```

`index.html` が GitHub Pages のトップページを兼ねているので、ローカルで見えるものと
公開されているものは同一です。

### 主なオプション

`src/aggregate.py`

| オプション | 既定 | 意味 |
|---|---|---|
| `--data` / `--out` | `data` / `build` | 入出力ディレクトリ |
| `--jobs` | 4 | 並列プロセス数 |
| `--today` / `--date` | 実行日 / 自動 | 基準日 / 対象日の固定 |
| `--begin-time` / `--end-time` | なし | 時間帯で絞る（`HH:MM`。未検証） |
| `--no-odpt` | off | ODPT のフィードを使わない |
| `--dup-overlap` | 0.8 | 重複と判定する停留所名の重なり |
| `--no-shapes` | off | 経路を使わず全部直線にする |
| `--shape-tolerance` | 5 | 経路の折れ線を間引く許容誤差 [m] |
| `--no-unify-stops` | off | フィード内の同名停留所をまとめない |
| `--unify-threshold` | 200 | 同名停留所をまとめる距離の上限 [m] |
| `--delimiter` | なし | 停留所名をこの文字で切って前方部分でまとめる |
| `--no-merge-operators` | off | 事業者横断の停留所統合をしない |
| `--merge-threshold` | 50 | 事業者横断で同一とみなす距離 [m] |
| `--max-segment-km` | 30 | これを超える区間を落とす（0で無効） |
| `--directional` | off | 上り下りを別地物にする |
| `--by-route` | off | 系統ごとに別地物にする |

`src/fetch.py` / `src/fetch_odpt.py` は `--out` `--jobs` `--force`（変更が無くても再取得）が共通。
`src/fetch.py` には `--skip-feed-meta`（個別メタデータの取得を省いて前回の manifest を流用する。廃止判定が古くなる）、
`src/fetch_odpt.py` には `--token` と `--catalog-only`（取得せずカタログの一覧だけ出す）があります。

### スクリプト

| ファイル | 役割 |
|---|---|
| `src/fetch.py` | gtfs-data.jp から取得。`gtfs_file_uid` を前回の manifest と比較して**変わったものだけ**再取得する。廃止フィードの除外と ZIP の健全性検証つき |
| `src/fetch_odpt.py` | ODPT から取得。カタログAPIが無いので CKAN の HTML を辿る。版違いは最新だけ選ぶ |
| `src/aggregate.py` | 集計本体。`--jobs` で並列。`build/aggregate_report.json` にフィードごとの採用日・件数・スキップ理由を出力 |
| `src/tiles.sh` | tippecanoe 呼び出し。z4–14。低ズームの線の簡略化は tippecanoe に任せる |
| `index.html` | MapLibre ビューア。ホバーで区間の方向別内訳が出る。GitHub Pages のトップページ |
| `src/serve.py` | Range リクエストに応答する最小のローカルサーバ（`index.html` の確認用） |
| `src/jpholidays.py` | 祝日判定（平日ダイヤの選択に使う） |

## 集計の仕様

全国・多フィードを1枚の地図にまとめる都合で、単一フィードを扱う一般的な集計とは
いくつか異なる判断をしています。

**対象日はフィードごとに自動で選ぶ。** 543フィードは有効期間がばらばらで、全国共通の1日を
指定するとその日が期間外のフィードが丸ごと欠落します。各フィードについて「有効期間内・平日
（祝日を除く）・実際に便がある」最初の日を選びます。採用日は `aggregate_report.json` に記録。

**ソース間の重複フィードを落とす。** 同じ事業者が gtfs-data.jp と ODPT の両方で配信している
ことがあります（実測11者）。そのまま足すと下の停留所統合が両方を合算し、便数が倍になります。
「正規化した事業者名が一致」かつ「停留所名の重なりが80%以上」の両方を満たすものだけを重複と
判定します。名前だけでは足りません（日立自動車交通は gtfs-data.jp に葛飾さくら、ODPT に
文京Bーぐると千代田風ぐるまがあり、同じ事業者でも別サービス）。停留所の重なりだけでも足りません
（熊本の九州産交バスと熊本都市バスは別事業者だが市内の停留所をほぼ共有している）。除外した
13フィードは `aggregate_report.json` に `status: duplicate` として理由つきで残ります。

**同名停留所をまとめるときは距離に上限を置く。** 「中野」「四谷」「七日町」のような一般名は
1つのフィード内の離れた場所に別々に存在します。名前だけでまとめると重心が両者の中間に飛び、
実在しない長距離区間ができます（西東京バスの「中野」で8km・295便、都営バスの「富岡一丁目」で
25km・436便の直線が出ていました）。同名グループの広がりを実測すると200m以内が98.5%
（50m:77.5% / 100m:91.3%）なので、200m を上限に単連結でクラスタへ割ります
（`--unify-threshold`）。

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
明示し、各ズームで見える区間数を z4:5,634 / z5:11,369 / z6:22,350 / z7:43,889 / z8:67,228 /
z9:83,235 / z10:全件 としています。

**区間の形は shapes.txt があれば実走行経路にする。** 650フィード中 424 に `shapes.txt` があり、
そのうち `stop_times` 側にも `shape_dist_traveled` があって距離で切れるのは 46 だけです。残りは
停留所を経路の折れ線に投影して切り出します。投影は「前の停留所より先」に限ります。環状線や
折返しでは同じ地点を2度通るため、全体から最近傍を採ると順序が壊れて経路が飛びます。それでも
失敗することがあるので、切り出した経路長が直線距離の3倍かつ超過2km を超えるものは捨てて
直線に戻します（都営バスの「上町→仲町」が直線0.19kmに対し経路23.7kmになっていた）。

折れ線は Douglas-Peucker で 5m まで間引きます（`--shape-tolerance`）。z14 の1画素は緯度35度で
約7.8mなので、これは画素より細かい誤差です。この間引きで「ほぼ直線」の区間は2点に潰れ、
中間点を持つのは全体の47%になります。許容誤差0なら shapes のあるフィードの94%に経路が付くので、
残りの大半は本当に直線の区間で、投影が失敗しているのは5.6%です。`--no-shapes` で全部直線に
できます。

**線幅や色はタイルに焼き込まない。** MapLibre の式で `frequency` から描けば、スタイル調整だけで
見た目を変えられます。

## 既知の制約

- **ODPT 側の都道府県コードが無い。** カタログに含まれないため、ODPT 由来の地物は `pref` が 0 です。
- **全国ズーム（z4–5）は点の散布に見える。** 1区間が数百mなので、線として読ませるには
  同頻度の連続区間を1本のポリラインに結合する処理が要ります。
- **平日1日のみ。** 土休日ダイヤや時間帯別（`--begin-time` / `--end-time` はあるが未検証）は
  現状スコープ外です。
- **`shapes.txt` の無い179フィード（区間32,072本）は直線のまま。** 主に地方のコミュニティバスで、
  フィード側にデータが無いため補いようがありません。
- **途中停車のない区間は経路があっても直線に近い。** 30km超は除外していますが、
  松山駅前→松山空港（4.6km・69便）のような空港連絡バスは残ります。

## ライセンスと出典

- コード: MIT License（`LICENSE`）
- データ: [GTFSデータリポジトリ](https://gtfs-data.jp/) と
  [公共交通オープンデータセンター](https://ckan.odpt.org/)。ライセンスはフィードごとに異なります
  （gtfs-data.jp 分は `data/manifest.json` の `license`、ODPT 分は `data/manifest_odpt.json` の
  `dataset_url` から各データセットのページを参照。多くは CC BY 4.0）
- `build/bus_frequency.pmtiles` は上記データの派生物です。利用時は出典を表示してください。
