#!/usr/bin/env python3
"""全国のGTFSフィードから運行頻度を集計し、tippecanoe 用の NDJSON を書き出す。

  python3 src/aggregate.py [--data data] [--out build] [--jobs 4]

入力は2つのソース。gtfs-data.jp（data/feeds/, src/fetch.py）を主とし、そこに無い
都営バス・京王バス・横浜市営バスなどを ODPT（data/feeds_odpt/, src/fetch_odpt.py）で
補う。両方に同じ事業者がいることがあるので、重複フィードは dedupe_feeds() で落とす。

集計ロジックは QGIS プラグイン GTFS-GO / japan-gtfs-frequency-viewer を踏襲しつつ、
全国・多フィードを1枚の地図にする都合で以下を変えている。

  * 対象日はフィードごとに自動選択する
      フィードごとに有効期間がばらばらなので、全国共通の1日を指定すると
      その日が有効期間外のフィードは丸ごと欠落する。そこで各フィードについて
      「有効期間内・平日（祝日を除く）・実際に便がある」最初の日を選ぶ。

  * 区間は既定で無向（上り下りを合算）
      A→B と B→A は完全に重なる線になるため、方向別に持つと地物数が倍になり
      タイル上では太い方が見えるだけで情報が増えない。合算した便数を frequency とし、
      方向別の値も frequency_ab / frequency_ba として残す。--directional で分離可。

  * 区間は既定で系統をまとめる
      同じ停留所間を複数系統が走る場合、系統ごとに地物を分けると全国では地物数が
      膨らむ。既定では合算して「その区間を1日に何便通るか」を表す。--by-route で分離可。

  * line_width / circle_radius は焼き込まない
      タイル側に持たせると式を変えるたびに再ビルドが必要になる。MapLibre の
      式で frequency から描画すれば、スタイル調整だけで見た目を変えられる。

  * 通過扱いの停車は停留所カウントに含めない
      pickup_type=1 かつ drop_off_type=1 の行は乗降できない通過なので、
      「延べ停車回数」としては数えない（区間の通過本数には影響しない）。

  * 事業者をまたぐ同一停留所をまとめる（既定で有効, --no-merge-operators で無効）
      フィードは事業者ごとに分かれているため、同じ停留所・同じ区間が事業者ごとに
      別地物になる。例えば熊本の「市役所前（熊本）⇄桜町バスターミナル」は
      九州産交バス951便と熊本都市バス681便が別地物で、地図上では太い方しか見えず
      実際の1,632便が過小に見える。
      同名かつ MERGE_THRESHOLD_M 以内の停留所を同一とみなして合算する。
      同名・異フィードの停留所ペアの距離は二極分布（20m以内が9.9%、50m以内が10.7%、
      500m以内が11.7%、その先は p25 で104km）で、しきい値の選び方には鈍感。
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import math
import re
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jpholidays  # noqa: E402

# GTFS route_type のうちバスとして扱うもの（拡張タイプ含む）
BUS_TYPES = {
    "3",  # Bus
    "11",  # Trolleybus
    "200", "201", "202", "203", "204", "205", "206", "207", "208", "209",  # Coach
    "700", "701", "702", "703", "704", "705", "706", "707", "708", "709",  # Bus service
    "710", "711", "712", "713", "714", "715", "716",  # Local/Demand and more
    "800",  # Trolleybus service
    "1500", "1501", "1502",  # Taxi / Shared taxi
}
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------------------
# ZIP からのテキスト読み出し
# ---------------------------------------------------------------------------
def read_table(z: zipfile.ZipFile, filename: str) -> list[dict[str, str]]:
    member = next((n for n in z.namelist() if n.split("/")[-1] == filename), None)
    if member is None:
        return []
    raw = z.read(member)
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    # csv モジュールを使うことで、引用符内の改行を含むフィールドも正しく読める
    return list(csv.DictReader(io.StringIO(text)))


def read_rows(z: zipfile.ZipFile, filename: str) -> tuple[list[str], list[list[str]]]:
    """大きなファイル（stop_times.txt）用。dict を作らず列インデックスで扱う。"""
    member = next((n for n in z.namelist() if n.split("/")[-1] == filename), None)
    if member is None:
        return [], []
    raw = z.read(member)
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    r = csv.reader(io.StringIO(text))
    try:
        header = [h.strip() for h in next(r)]
    except StopIteration:
        return [], []
    return header, list(r)


# ---------------------------------------------------------------------------
# 日付ユーティリティ
# ---------------------------------------------------------------------------
def parse_date(s: str) -> datetime.date | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def active_service_ids(
    d: datetime.date, calendar: list[dict], calendar_dates: list[dict]
) -> set[str]:
    ymd = d.strftime("%Y%m%d")
    day = DAY_NAMES[d.weekday()]
    active: set[str] = set()
    for c in calendar:
        start, end = (c.get("start_date") or "").strip(), (c.get("end_date") or "").strip()
        if start <= ymd <= end and (c.get(day) or "").strip() == "1":
            active.add((c.get("service_id") or "").strip())
    for cd in calendar_dates:
        if (cd.get("date") or "").strip() != ymd:
            continue
        sid = (cd.get("service_id") or "").strip()
        et = (cd.get("exception_type") or "").strip()
        if et == "1":
            active.add(sid)
        elif et == "2":
            active.discard(sid)
    active.discard("")
    return active


def time_to_seconds(t: str) -> int:
    """GTFS の時刻。24:xx:xx 以降もあり得るので時は正規化しない。"""
    t = (t or "").strip()
    if not t:
        return -1
    parts = t.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return -1
    return h * 3600 + m * 60 + s


def filter_time_to_seconds(v: str) -> int:
    v = (v or "").strip()
    if not v:
        return -1
    if ":" in v:
        parts = v.split(":")
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60
        except (ValueError, IndexError):
            return -1
    if len(v) >= 4:
        try:
            return int(v[:2]) * 3600 + int(v[2:4]) * 60
        except ValueError:
            return -1
    return -1


# ---------------------------------------------------------------------------
# 1フィードの集計
# ---------------------------------------------------------------------------
def aggregate_feed(task: dict) -> dict:
    key = task["key"]
    entry = task["entry"]
    opts = task["opts"]
    result: dict = {
        "key": key,
        "source": task.get("source", ""),
        "organization_name": entry["organization_name"],
        "feed_name": entry["feed_name"],
        "pref_id": entry["pref_id"],
        "license": entry["license"],
        "target_date": None,
        "status": "ok",
        "note": "",
        "routes": [],
        "stops": [],
    }

    try:
        z = zipfile.ZipFile(task["zip_path"])
    except Exception as e:
        result["status"] = "error"
        result["note"] = f"ZIPを開けない: {e}"
        return result

    with z:
        routes_raw = read_table(z, "routes.txt")
        if not routes_raw:
            result["status"] = "skip"
            result["note"] = "routes.txt が無い/空"
            return result

        # --- モード判定：バスを含まないフィードは除外 ---
        types = {(r.get("route_type") or "").strip() for r in routes_raw}
        if not (types & BUS_TYPES):
            result["status"] = "skip"
            result["note"] = f"バス以外のモード (route_type={sorted(types)})"
            return result

        bus_route_ids = {
            (r.get("route_id") or "").strip()
            for r in routes_raw
            if (r.get("route_type") or "").strip() in BUS_TYPES
        }

        stops_raw = read_table(z, "stops.txt")
        trips_raw = read_table(z, "trips.txt")
        calendar = read_table(z, "calendar.txt")
        calendar_dates = read_table(z, "calendar_dates.txt")
        agency_raw = read_table(z, "agency.txt")
        st_header, st_rows = read_rows(z, "stop_times.txt")

        if not trips_raw or not st_rows:
            result["status"] = "skip"
            result["note"] = "trips.txt / stop_times.txt が無い/空"
            return result

        # --- 事業者名 ---
        agency_by_id: dict[str, str] = {}
        for a in agency_raw:
            agency_by_id[(a.get("agency_id") or "").strip()] = (
                a.get("agency_name") or ""
            ).strip()
        default_agency = agency_raw[0].get("agency_name", "").strip() if agency_raw else ""
        # ソース間の重複判定に使う（gtfs-data.jp と ODPT に同じ事業者が両方いる）
        result["agencies"] = sorted({v for v in agency_by_id.values() if v})

        route_meta = {
            (r.get("route_id") or "").strip(): (
                agency_by_id.get((r.get("agency_id") or "").strip()) or default_agency
            )
            for r in routes_raw
        }

        # --- trip → (route_id, service_id) ---
        trip_route: dict[str, str] = {}
        trip_service: dict[str, str] = {}
        for t in trips_raw:
            tid = (t.get("trip_id") or "").strip()
            rid = (t.get("route_id") or "").strip()
            if rid not in bus_route_ids:
                continue
            trip_route[tid] = rid
            trip_service[tid] = (t.get("service_id") or "").strip()
        if not trip_route:
            result["status"] = "skip"
            result["note"] = "バス系統に属する trip が無い"
            return result

        # --- stop_times を trip ごとに並べる ---
        try:
            i_trip = st_header.index("trip_id")
            i_stop = st_header.index("stop_id")
            i_seq = st_header.index("stop_sequence")
        except ValueError as e:
            result["status"] = "error"
            result["note"] = f"stop_times.txt の必須列が無い: {e}"
            return result
        i_dep = st_header.index("departure_time") if "departure_time" in st_header else -1
        i_arr = st_header.index("arrival_time") if "arrival_time" in st_header else -1
        i_pick = st_header.index("pickup_type") if "pickup_type" in st_header else -1
        i_drop = st_header.index("drop_off_type") if "drop_off_type" in st_header else -1

        trip_times: dict[str, list[tuple[int, str, str, bool]]] = defaultdict(list)
        ncol = len(st_header)
        for row in st_rows:
            if len(row) < ncol:
                row = row + [""] * (ncol - len(row))
            tid = row[i_trip].strip()
            if tid not in trip_route:
                continue
            try:
                seq = int(row[i_seq])
            except ValueError:
                continue
            dep = row[i_dep].strip() if i_dep >= 0 else ""
            arr = row[i_arr].strip() if i_arr >= 0 else ""
            t = dep or arr
            pick = row[i_pick].strip() if i_pick >= 0 else ""
            drop = row[i_drop].strip() if i_drop >= 0 else ""
            # 乗降不可（通過）の停車は停留所カウントに含めない
            served = not (pick == "1" and drop == "1")
            trip_times[tid].append((seq, row[i_stop].strip(), t, served))
        for v in trip_times.values():
            v.sort(key=lambda x: x[0])

        # --- 対象日の自動選択 ---
        from_d = parse_date(entry.get("from_date") or "") or opts["today"]
        to_d = parse_date(entry.get("to_date") or "") or (
            opts["today"] + datetime.timedelta(days=365)
        )
        if opts["date"]:
            candidates = [parse_date(opts["date"])]
        else:
            start = max(opts["today"], from_d)
            candidates = []
            d = start
            # 有効期間の終わりまで、最大400日ぶん探索する
            while d <= to_d and len(candidates) < 400:
                if jpholidays.is_weekday(d):
                    candidates.append(d)
                d += datetime.timedelta(days=1)
            if not candidates:
                # 有効期間が既に過ぎている等。期間内の平日を後ろ向きに探す
                d = min(to_d, opts["today"])
                while d >= from_d:
                    if jpholidays.is_weekday(d):
                        candidates.append(d)
                        break
                    d -= datetime.timedelta(days=1)

        target: datetime.date | None = None
        active: set[str] = set()
        for cand in candidates:
            if cand is None:
                continue
            act = active_service_ids(cand, calendar, calendar_dates)
            if not act:
                continue
            # その日に実際に2停留所以上ある便があるか
            if any(
                trip_service.get(tid) in act and len(times) >= 2
                for tid, times in trip_times.items()
            ):
                target, active = cand, act
                break

        if target is None:
            result["status"] = "skip"
            result["note"] = (
                f"有効期間 {entry.get('from_date')}〜{entry.get('to_date')} 内に"
                "平日ダイヤの運行日が見つからない"
            )
            return result
        result["target_date"] = target.isoformat()

        # --- 停留所統合（同名を重心にまとめる） ---
        delim = opts["delimiter"]
        groups: dict[str, list[dict]] = defaultdict(list)
        for s in stops_raw:
            name = (s.get("stop_name") or "").strip()
            if delim and delim in name:
                name = name.split(delim)[0]
            groups[name].append(s)

        similar: dict[str, tuple[str, str, float, float]] = {}
        rep_info: dict[str, tuple[str, float, float]] = {}
        unify_m = opts["unify_threshold"]
        for name, group in groups.items():
            pts: list[tuple[str, float, float]] = []
            for s in group:
                sid = (s.get("stop_id") or "").strip()
                try:
                    pts.append((sid, float(s["stop_lat"]), float(s["stop_lon"])))
                except (KeyError, ValueError, TypeError):
                    continue
            if not pts:
                continue

            if not opts["unify_stops"]:
                for sid, slat, slon in pts:
                    rep_info[sid] = (name, slat, slon)
                    similar[sid] = (sid, name, slat, slon)
                continue

            # 同名でも離れていれば別の停留所。「中野」「四谷」のような一般名は
            # 1フィード内の別々の場所にあり、まとめると存在しない長距離区間が
            # 生まれる（西東京バスの「中野」で 8km、都営バスの「富岡一丁目」で
            # 25km の直線ができていた）。同名グループを距離で単連結クラスタに割る。
            parent = list(range(len(pts)))

            def find(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            for i in range(len(pts)):
                _, lat1, lon1 = pts[i]
                c = math.cos(math.radians(lat1))
                for j in range(i + 1, len(pts)):
                    _, lat2, lon2 = pts[j]
                    dy = (lat2 - lat1) * 111320.0
                    dx = (lon2 - lon1) * 111320.0 * c
                    if dy * dy + dx * dx <= unify_m * unify_m:
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[ri] = rj

            clusters: dict[int, list[int]] = defaultdict(list)
            for i in range(len(pts)):
                clusters[find(i)].append(i)

            for members in clusters.values():
                lat = sum(pts[i][1] for i in members) / len(members)
                lon = sum(pts[i][2] for i in members) / len(members)
                rep = pts[members[0]][0]
                rep_info[rep] = (name, lat, lon)
                for i in members:
                    similar[pts[i][0]] = (rep, name, lat, lon)

        # --- 時間帯フィルタ ---
        begin_s = filter_time_to_seconds(opts["begin_time"])
        end_s = filter_time_to_seconds(opts["end_time"])
        use_time = begin_s >= 0 and end_s >= 0

        # --- 集計 ---
        # 区間: key -> [freq_ab, freq_ba, set(route_id)]
        segments: dict[tuple, list] = {}
        stop_counts: dict[str, int] = defaultdict(int)

        for tid, times in trip_times.items():
            if trip_service.get(tid) not in active:
                continue
            rid = trip_route[tid]

            for _seq, sid, t, served in times:
                if not served:
                    continue
                if use_time:
                    sec = time_to_seconds(t)
                    if sec < 0 or sec < begin_s or sec > end_s:
                        continue
                sim = similar.get(sid)
                if sim:
                    stop_counts[sim[0]] += 1

            for i in range(len(times) - 1):
                _s1, sid1, t1, _v1 = times[i]
                _s2, sid2, _t2, _v2 = times[i + 1]
                if use_time:
                    sec = time_to_seconds(t1)
                    if sec < 0 or sec < begin_s or sec > end_s:
                        continue
                a = similar.get(sid1)
                b = similar.get(sid2)
                if not a or not b or a[0] == b[0]:
                    continue
                aid, bid = a[0], b[0]
                if opts["directional"]:
                    k = (aid, bid, rid if opts["by_route"] else "")
                    seg = segments.get(k)
                    if seg is None:
                        segments[k] = [1, 0, {rid}]
                    else:
                        seg[0] += 1
                        seg[2].add(rid)
                else:
                    # 無向: 端点をソートして同一キーに寄せ、向きは ab/ba に分けて数える
                    fwd = aid <= bid
                    lo, hi = (aid, bid) if fwd else (bid, aid)
                    k = (lo, hi, rid if opts["by_route"] else "")
                    seg = segments.get(k)
                    if seg is None:
                        seg = [0, 0, set()]
                        segments[k] = seg
                    seg[0 if fwd else 1] += 1
                    seg[2].add(rid)

        # --- 中間レコード化（GeoJSON 化は親プロセスの統合パスで行う） ---
        # 事業者をまたぐ統合は全フィードを見渡さないとできないため、ここでは
        # 「名前 + 代表座標」を持つ素のレコードを返すだけにする。
        agency_name = default_agency or entry["organization_name"]
        for (aid, bid, rid), (fab, fba, rids) in segments.items():
            ra = rep_info.get(aid)
            rb = rep_info.get(bid)
            if not ra or not rb:
                continue
            agency = (route_meta.get(rid) if rid else None) or (
                route_meta.get(next(iter(rids)), "") if rids else ""
            ) or agency_name
            # (name, lat, lon) を両端に持たせる。fab は a→b、fba は b→a の便数。
            result["routes"].append(
                (ra[0], ra[1], ra[2], rb[0], rb[1], rb[2], fab, fba, len(rids), agency, rid)
            )

        for sid, count in stop_counts.items():
            info = rep_info.get(sid)
            if not info or count == 0:
                continue
            result["stops"].append((info[0], info[1], info[2], count, agency_name))

    return result


# ---------------------------------------------------------------------------
# 事業者横断の停留所統合
# ---------------------------------------------------------------------------
MERGE_THRESHOLD_M = 50.0

# フィード内で同名の停留所をまとめるときの距離上限。同名グループの広がりを実測すると
# 200m 以内が 98.5%（50m:77.5% / 100m:91.3%）で、それを超えるのは「中野」「七日町」の
# ような一般名が離れた場所に別々にあるケース。上限を置かないと幻の長距離区間ができる。
UNIFY_THRESHOLD_M = 200.0

# 区間は停留所どうしを直線で結んだもの。高速バスの「バスタ新宿→徳島駅前」のように
# 途中停車のない区間は 500km の直線になり、経路とは無関係な線が地図を横切る。
# 区間長の中央値は 0.42km、30km を超えるのは全体の 0.06% で、いずれも 1〜2便の
# 高速バスだった。既定で落とし、必要なら --max-segment-km 0 で残す。
MAX_SEGMENT_KM = 30.0


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        p = self.parent.setdefault(x, x)
        while p != x:
            x, p = p, self.parent.setdefault(p, p)
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_stops(
    nodes: list[tuple[str, float, float]], threshold_m: float
) -> list[int]:
    """(名前, lat, lon) の列を「同名かつ threshold 以内」で連結し、代表IDの配列を返す。

    しきい値をセルサイズにしたグリッドに落とし、同名・近傍セルのみを比較するので
    地点数に対して線形。全国の「駅前」のような巨大な同名グループでも破綻しない。
    """
    deg = threshold_m / 111320.0  # 緯度1度≒111.32km。セルサイズとして使う
    buckets: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for i, (name, lat, lon) in enumerate(nodes):
        buckets[(name, int(lat / deg), int(lon / deg))].append(i)

    uf = _UnionFind()
    for i in range(len(nodes)):
        uf.find(i)
    cos_cache: dict[int, float] = {}
    for (name, gy, gx), idxs in buckets.items():
        # 自セル + 隣接8セルを候補にする
        cand: list[int] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                cand.extend(buckets.get((name, gy + dy, gx + dx), ()))
        for i in idxs:
            _, lat1, lon1 = nodes[i]
            key = int(lat1)
            c = cos_cache.get(key)
            if c is None:
                c = math.cos(math.radians(lat1))
                cos_cache[key] = c
            for j in cand:
                if j <= i:
                    continue
                _, lat2, lon2 = nodes[j]
                dy_m = (lat2 - lat1) * 111320.0
                dx_m = (lon2 - lon1) * 111320.0 * c
                if dy_m * dy_m + dx_m * dx_m <= threshold_m * threshold_m:
                    uf.union(i, j)
    return [uf.find(i) for i in range(len(nodes))]


# ---------------------------------------------------------------------------
# ソースをまたぐ重複フィードの検出
# ---------------------------------------------------------------------------
# 同じ事業者が gtfs-data.jp と ODPT の両方で配信していることがある（実測11者）。
# そのまま足すと事業者横断の停留所統合が両方を合算し、便数が倍になる。
DUP_OVERLAP = 0.8

_CORP_RE = re.compile(r"(株式会社|有限会社|\(株\)|（株）|一般社団法人|公益社団法人|合同会社)")


def normalize_agency(name: str) -> str:
    return _CORP_RE.sub("", name).replace(" ", "").replace("　", "")


def dedupe_feeds(results: list[dict], threshold: float = DUP_OVERLAP) -> list[dict]:
    """重複フィードに status="duplicate" を立てる。落とした側は集計に入れない。

    事業者名の一致だけでは判定できない。日立自動車交通は gtfs-data.jp に葛飾さくら、
    ODPT に文京Bーぐると千代田風ぐるまがあり、同じ事業者だが別の路線群になる。
    逆に停留所の重なりだけでも判定できない。熊本の九州産交バスと熊本都市バスは
    別事業者だが市内の停留所をほぼ共有しており、これは統合したい重複ではない。
    そこで「正規化した事業者名が一致」かつ「停留所名の重なりが threshold 以上」
    の両方を満たすものだけを重複とみなす。

    さらに gtfs-data.jp どうしは比較しない。あちらは1事業者の路線群がフィードに
    分かれているだけで重複は無く、比較すると誤検出になる（JR東日本盛岡支社の
    津軽線代行バスとわんどタクシーは停留所が89%重なるが別サービス）。重複が
    起きるのは ODPT が絡む場合、すなわちソース間か、ODPT 内のライセンス違い
    （東大和市ちょこバスの CC0 版と CC BY 版）だけ。

    残す側の優先順位は gtfs-data.jp を先にする。全国を網羅していて都道府県コードも
    持っており、こちらを基準にしたほうが結果が安定するため。
    """
    order = sorted(
        results,
        key=lambda r: (0 if r.get("source") != "odpt" else 1, -len(r.get("stops", []))),
    )
    kept: list[dict] = []
    by_agency: dict[str, list[int]] = defaultdict(list)
    for r in order:
        if r["status"] != "ok" or not r["stops"]:
            continue
        names = {s[0] for s in r["stops"]}
        ags = {normalize_agency(a) for a in r.get("agencies") or []}
        ags.add(normalize_agency(r["organization_name"]))
        ags.discard("")

        dup = None
        for i in {i for a in ags for i in by_agency.get(a, ())}:
            other = kept[i]
            if r.get("source") != "odpt" and other.get("source") != "odpt":
                continue
            overlap = len(names & other["_stop_names"]) / min(len(names), len(other["_stop_names"]))
            if overlap >= threshold:
                dup = (other, overlap)
                break
        if dup:
            other, overlap = dup
            r["status"] = "duplicate"
            r["note"] = (
                f'{other["key"]} ({other.get("source", "?")}) と重複'
                f"（停留所の重なり {overlap:.0%}）"
            )
            r["routes"], r["stops"] = [], []
            continue

        r["_stop_names"] = names
        kept.append(r)
        for a in ags:
            by_agency[a].append(len(kept) - 1)
    for r in kept:
        r.pop("_stop_names", None)
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="全国のGTFSから運行頻度を集計")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="build")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--date", default="", help="対象日を固定 (YYYY-MM-DD)。既定は自動選択")
    ap.add_argument("--today", default="", help="基準日 (YYYY-MM-DD)。既定は実行日")
    ap.add_argument("--begin-time", default="", help="時間帯フィルタ開始 (HH:MM)")
    ap.add_argument("--end-time", default="", help="時間帯フィルタ終了 (HH:MM)")
    ap.add_argument("--no-unify-stops", action="store_true", help="同名停留所をまとめない")
    ap.add_argument(
        "--unify-threshold",
        type=float,
        default=UNIFY_THRESHOLD_M,
        help=f"フィード内で同名停留所をまとめる距離の上限 [m] (default: {UNIFY_THRESHOLD_M:.0f})",
    )
    ap.add_argument("--delimiter", default="", help="停留所名の区切り文字（前方部分でまとめる）")
    ap.add_argument("--directional", action="store_true", help="上り下りを別地物にする")
    ap.add_argument("--by-route", action="store_true", help="系統ごとに別地物にする")
    ap.add_argument(
        "--no-merge-operators",
        action="store_true",
        help="事業者をまたぐ同一停留所の統合を行わない（フィードごとに別地物のまま）",
    )
    ap.add_argument(
        "--merge-threshold",
        type=float,
        default=MERGE_THRESHOLD_M,
        help=f"事業者横断で同一停留所とみなす距離 [m] (default: {MERGE_THRESHOLD_M:.0f})",
    )
    ap.add_argument(
        "--max-segment-km",
        type=float,
        default=MAX_SEGMENT_KM,
        help=(
            "隣接停留所間がこの距離を超える区間を落とす [km]。"
            f"0 で無効 (default: {MAX_SEGMENT_KM:.0f})"
        ),
    )
    ap.add_argument("--no-odpt", action="store_true", help="ODPT のフィードを使わない")
    ap.add_argument(
        "--dup-overlap",
        type=float,
        default=DUP_OVERLAP,
        help=f"重複フィードとみなす停留所名の重なり (default: {DUP_OVERLAP})",
    )
    args = ap.parse_args()

    data = Path(args.data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ソースは2つ。gtfs-data.jp が主で、ODPT は都営バスなど gtfs-data.jp に無い
    # 事業者を埋めるためのもの。取り込み方が違うだけで、以降の扱いは同じ。
    sources = [("gtfs-data.jp", data / "manifest.json", data / "feeds", "src/fetch.py")]
    if not args.no_odpt:
        sources.append(("odpt", data / "manifest_odpt.json", data / "feeds_odpt", "src/fetch_odpt.py"))

    manifests: list[tuple[str, dict, Path]] = []
    for name, mpath, fdir, script in sources:
        if not mpath.exists():
            if name == "gtfs-data.jp":
                print(f"ERROR: {mpath} がありません。先に {script} を実行してください。", file=sys.stderr)
                return 1
            print(f"[skip] {mpath} がありません（{script} 未実行）。{name} は使いません。")
            continue
        manifests.append((name, json.loads(mpath.read_text(encoding="utf-8")), fdir))

    today = parse_date(args.today) or datetime.date.today()
    opts = {
        "today": today,
        "date": args.date,
        "begin_time": args.begin_time,
        "end_time": args.end_time,
        "unify_stops": not args.no_unify_stops,
        "unify_threshold": args.unify_threshold,
        "delimiter": args.delimiter,
        "directional": args.directional,
        "by_route": args.by_route,
    }

    tasks = []
    per_source: dict[str, int] = defaultdict(int)
    for name, manifest, fdir in manifests:
        for key, entry in manifest["feeds"].items():
            if entry.get("discontinued"):
                continue
            zp = fdir / f"{key}.zip"
            if not zp.exists():
                continue
            tasks.append(
                {"key": key, "entry": entry, "zip_path": str(zp), "opts": opts, "source": name}
            )
            per_source[name] += 1

    print(f"集計対象: {len(tasks)} フィード（基準日 {today}）  内訳 {dict(per_source)}")
    print(f"設定: 停留所統合={opts['unify_stops']} 無向={not args.directional} 系統統合={not args.by_route}")

    routes_path = out / "routes.geojsonl"
    stops_path = out / "stops.geojsonl"
    report: list[dict] = []
    status_count: dict[str, int] = defaultdict(int)

    # 全フィードの中間レコードを集める（区間64k・停留所57k規模なのでメモリ上で扱える）
    raw_segments: list[tuple] = []  # (ai, bi, fab, fba, nroutes, agency, feed, pref, rid)
    raw_stops: list[tuple] = []  # (ni, count, agency, feed, pref)
    nodes: list[tuple[str, float, float]] = []  # 統合対象の停留所ノード
    node_index: dict[tuple[str, float, float], int] = {}

    def node_id(name: str, lat: float, lon: float) -> int:
        k = (name, round(lat, 6), round(lon, 6))
        i = node_index.get(k)
        if i is None:
            i = len(nodes)
            node_index[k] = i
            nodes.append(k)
        return i

    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(aggregate_feed, t) for t in tasks]
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 100 == 0 or done == len(futs):
                print(f"  {done}/{len(futs)}", flush=True)

    # 重複の判定は全フィードが揃ってからでないとできない
    if len(manifests) > 1:
        dedupe_feeds(results, args.dup_overlap)
        dups = [r for r in results if r["status"] == "duplicate"]
        print(f"ソース間の重複: {len(dups)} フィードを除外")
        for r in sorted(dups, key=lambda x: x["key"]):
            print(f'  [dup] {r["organization_name"]}/{r["feed_name"]}: {r["note"]}')

    for r in results:
        status_count[r["status"]] += 1
        key, pref = r["key"], r["pref_id"]
        for na, la, oa, nb, lb, ob, fab, fba, nr, agency, rid in r["routes"]:
            raw_segments.append(
                (node_id(na, la, oa), node_id(nb, lb, ob), fab, fba, nr, agency, key, pref, rid)
            )
        for nm, la, lo, count, agency in r["stops"]:
            raw_stops.append((node_id(nm, la, lo), count, agency, key, pref))
        report.append(
            {
                "key": r["key"],
                "source": r.get("source", ""),
                "organization_name": r["organization_name"],
                "feed_name": r["feed_name"],
                "pref_id": r["pref_id"],
                "license": r["license"],
                "target_date": r["target_date"],
                "status": r["status"],
                "note": r["note"],
                "n_segments": len(r["routes"]),
                "n_stops": len(r["stops"]),
            }
        )

    print(f"\nフィード単位の集計: 区間 {len(raw_segments):,} / 停留所 {len(raw_stops):,}")

    # --- 事業者横断の停留所統合 ---
    if args.no_merge_operators:
        rep_of = list(range(len(nodes)))
        print("事業者横断の統合: 無効 (--no-merge-operators)")
    else:
        rep_of = cluster_stops(nodes, args.merge_threshold)
        n_clusters = len(set(rep_of))
        print(
            f"事業者横断の統合: 停留所ノード {len(nodes):,} → {n_clusters:,} "
            f"({len(nodes)-n_clusters:,} 件を統合, しきい値 {args.merge_threshold:.0f}m)"
        )

    # クラスタの代表座標は構成ノードの重心
    csum: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    cname: dict[int, str] = {}
    for i, (name, lat, lon) in enumerate(nodes):
        c = rep_of[i]
        acc = csum[c]
        acc[0] += lat
        acc[1] += lon
        acc[2] += 1
        cname.setdefault(c, name)
    ccoord = {c: (v[0] / v[2], v[1] / v[2]) for c, v in csum.items()}

    # --- 区間の統合 ---
    seg_key = 8 if args.by_route else None
    merged_seg: dict[tuple, list] = {}
    for ai, bi, fab, fba, nr, agency, feed, pref, rid in raw_segments:
        ca, cb = rep_of[ai], rep_of[bi]
        if ca == cb:
            continue  # 統合により同一停留所になった区間は落とす
        if args.directional:
            k = (ca, cb, rid if args.by_route else "")
            fwd = True
        else:
            fwd = ca <= cb
            lo, hi = (ca, cb) if fwd else (cb, ca)
            k = (lo, hi, rid if args.by_route else "")
        m = merged_seg.get(k)
        if m is None:
            m = [0, 0, 0, set(), set(), pref]
            merged_seg[k] = m
        if fwd:
            m[0] += fab
            m[1] += fba
        else:
            m[0] += fba
            m[1] += fab
        m[2] += nr
        m[3].add(agency)
        m[4].add(feed)

    # --- 停留所の統合 ---
    merged_stop: dict[int, list] = {}
    for ni, count, agency, feed, pref in raw_stops:
        c = rep_of[ni]
        m = merged_stop.get(c)
        if m is None:
            m = [0, set(), set(), pref]
            merged_stop[c] = m
        m[0] += count
        m[1].add(agency)
        m[2].add(feed)

    # --- 書き出し ---
    # 便数に応じて地物の最小ズームを決める。
    # tippecanoe の --drop-densest-as-needed に任せると密度だけで間引かれ、
    # 全国ズームで「たまたま残った区間」が見える絵になってしまう。便数の多い
    # 幹線から順に現れるよう、こちらで明示的に指定する。
    # 閾値は実データの分布から、各ズームで見える区間数がおおよそ
    # z4:800 / z5:2.9千 / z6:9.5千 / z7:2.6万 / z8:4.7万 / z9以降:全件
    # になるように決めた。全国ズームで数百本しか出ないと絵にならず、
    # かといって全件出すとタイルが膨らむ。
    def route_minzoom(freq: int) -> int:
        for th, z in ((100, 4), (50, 5), (25, 6), (12, 7), (6, 8), (2, 9)):
            if freq >= th:
                return z
        return 10

    def stop_minzoom(count: int) -> int:
        for th, z in ((200, 9), (50, 10), (10, 11)):
            if count >= th:
                return z
        return 12

    def agency_label(names: set[str]) -> str:
        vals = sorted(n for n in names if n)
        if not vals:
            return ""
        if len(vals) <= 3:
            return "・".join(vals)
        return "・".join(vals[:3]) + f" 他{len(vals)-3}社"

    max_seg_m = args.max_segment_km * 1000.0

    def too_long(la: float, oa: float, lb: float, ob: float) -> bool:
        if max_seg_m <= 0:
            return False
        dy = (lb - la) * 111320.0
        dx = (ob - oa) * 111320.0 * math.cos(math.radians((la + lb) / 2))
        return dy * dy + dx * dx > max_seg_m * max_seg_m

    n_routes = n_stops = n_dropped = 0
    with routes_path.open("w", encoding="utf-8") as fr:
        for (ca, cb, rid), (fab, fba, nr, agencies, feeds, pref) in merged_seg.items():
            la, oa = ccoord[ca]
            lb, ob = ccoord[cb]
            if too_long(la, oa, lb, ob):
                n_dropped += 1
                continue
            props = {
                "frequency": fab + fba,
                "frequency_ab": fab,
                "frequency_ba": fba,
                "stop_a": cname[ca],
                "stop_b": cname[cb],
                "agency": agency_label(agencies),
                "operators": len(agencies),
                "routes": nr,
                "pref": pref,
            }
            if args.by_route:
                props["route_id"] = rid
            fr.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": route_minzoom(fab + fba)},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                [round(oa, 6), round(la, 6)],
                                [round(ob, 6), round(lb, 6)],
                            ],
                        },
                        "properties": props,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            n_routes += 1

    with stops_path.open("w", encoding="utf-8") as fs:
        for c, (count, agencies, feeds, pref) in merged_stop.items():
            lat, lon = ccoord[c]
            fs.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": stop_minzoom(count)},
                        "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                        "properties": {
                            "name": cname[c],
                            "count": count,
                            "agency": agency_label(agencies),
                            "operators": len(agencies),
                            "pref": pref,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            n_stops += 1

    report.sort(key=lambda x: (-x["n_segments"], x["key"]))
    (out / "aggregate_report.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.datetime.now(datetime.timezone.utc)
                .astimezone()
                .isoformat(timespec="seconds"),
                "today": today.isoformat(),
                "options": {
                    **{k: (str(v) if isinstance(v, datetime.date) else v) for k, v in opts.items()},
                    "merge_operators": not args.no_merge_operators,
                    "unify_threshold_m": args.unify_threshold,
                    "merge_threshold_m": args.merge_threshold,
                    "max_segment_km": args.max_segment_km,
                    "dup_overlap": args.dup_overlap,
                },
                "sources": {name: per_source[name] for name, _, _ in manifests},
                "totals": {
                    "feeds": len(tasks),
                    "segments": n_routes,
                    "segments_dropped_long": n_dropped,
                    "stops": n_stops,
                    "status": dict(status_count),
                },
                "feeds": report,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print()
    print(
        f"区間: {n_routes:,} 地物  → {routes_path}"
        + (f"  ({n_dropped:,} 件を長すぎる区間として除外)" if n_dropped else "")
    )
    print(f"停留所: {n_stops:,} 地物  → {stops_path}")
    print(f"状態: {dict(status_count)}")
    print(f"レポート: {out / 'aggregate_report.json'}")
    skipped = [r for r in report if r["status"] != "ok"]
    if skipped:
        print(f"\nスキップ/エラー {len(skipped)} 件:")
        for r in skipped[:30]:
            print(f"  [{r['status']}] {r['organization_name']}/{r['feed_name']}: {r['note']}")
        if len(skipped) > 30:
            print(f"  ... 他 {len(skipped)-30} 件（aggregate_report.json 参照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
