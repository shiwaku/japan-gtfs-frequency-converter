#!/usr/bin/env python3
"""gtfs-data.jp（GTFSデータリポジトリ）から全国のGTFSフィードを取得する。

  python3 src/fetch.py [--out data] [--jobs 6] [--force] [--skip-feed-meta]

やっていること:
  1. /v2/files で全フィードの current 版一覧を取得
  2. /v2/organizations/{org}/feeds/{feed} で版履歴と is_discontinued を取得
     （廃止フィードは /v2/files に残り続けるため、この確認は省略できない）
  3. 前回の manifest と gtfs_file_uid を比較し、変わったフィードだけ再取得
  4. ZIP として開けるか検証してから採用

なぜ 3・4 が必要か:
  - file_url は 302 で S3 の署名付きURLに飛ぶが、その署名の有効期間は 60 秒しかない。
    事前にURLを集めて後でまとめて落とすことはできず、失敗時は取り直すしかない。
  - 期限切れ時のレスポンスは HTTP 200 で AccessDenied の XML（約350バイト）が返る。
    ZIP として開いて初めて壊れていることが分かるので、サイズだけでは検知できない。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://api.gtfs-data.jp/v2"
UA = "japan-bus-frequency-pmtiles/0.1 (+https://gtfs-data.jp/)"
MAX_RETRY = 4


def get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip

            raw = gzip.decompress(raw)
        return raw


def get_json(url: str, timeout: int = 120) -> dict:
    return json.loads(get(url, timeout).decode("utf-8"))


def feed_key(org: str, feed: str) -> str:
    return f"{org}__{feed}"


# ---------------------------------------------------------------------------
# 1. フィード一覧
# ---------------------------------------------------------------------------
def fetch_file_list() -> list[dict]:
    """/v2/files の全件。クエリパラメータは効かないので絞り込みはクライアント側で行う。"""
    body = get_json(f"{API}/files")["body"]
    if not isinstance(body, list):
        raise RuntimeError(f"想定外のレスポンス: {str(body)[:200]}")
    return body


# ---------------------------------------------------------------------------
# 2. フィード個別メタデータ（is_discontinued / 版履歴）
# ---------------------------------------------------------------------------
def fetch_feed_meta(org: str, feed: str) -> dict | None:
    url = f"{API}/organizations/{org}/feeds/{feed}"
    for attempt in range(MAX_RETRY):
        try:
            return get_json(url, timeout=60)["body"]
        except Exception as e:
            if attempt == MAX_RETRY - 1:
                print(f"  WARN: メタデータ取得失敗 {org}/{feed}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_all_feed_meta(files: list[dict], jobs: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {
            ex.submit(fetch_feed_meta, f["organization_id"], f["feed_id"]): feed_key(
                f["organization_id"], f["feed_id"]
            )
            for f in files
        }
        done = 0
        for fut in as_completed(futs):
            key = futs[fut]
            meta = fut.result()
            if meta is not None:
                out[key] = meta
            done += 1
            if done % 50 == 0 or done == len(futs):
                print(f"  メタデータ {done}/{len(futs)}", flush=True)
    return out


# ---------------------------------------------------------------------------
# 3. ZIP ダウンロード
# ---------------------------------------------------------------------------
def download_feed(url: str, dest: Path) -> tuple[bool, str]:
    """署名60秒の制約下でZIPを取得する。ZIPとして開けるまでリトライ。"""
    tmp = dest.with_suffix(".part")
    last = ""
    for attempt in range(MAX_RETRY):
        try:
            data = get(url, timeout=180)
            tmp.write_bytes(data)
            # 期限切れは HTTP 200 + AccessDenied XML で返るため、ZIP として検証する
            with zipfile.ZipFile(tmp) as z:
                names = {n.split("/")[-1] for n in z.namelist()}
                if "stops.txt" not in names:
                    raise ValueError(f"stops.txt が無い (entries={len(z.namelist())})")
            tmp.replace(dest)
            return True, ""
        except Exception as e:
            last = str(e)[:120]
            head = tmp.read_bytes()[:200] if tmp.exists() else b""
            if b"AccessDenied" in head or b"Request has expired" in head:
                last = "署名付きURLの期限切れ (Request has expired)"
            if attempt < MAX_RETRY - 1:
                time.sleep(2.0 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    return False, last


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="gtfs-data.jp から全国のGTFSを取得")
    ap.add_argument("--out", default="data", help="出力ディレクトリ (default: data)")
    ap.add_argument("--jobs", type=int, default=6, help="並列数 (default: 6)")
    ap.add_argument("--force", action="store_true", help="uid が同じでも再取得する")
    ap.add_argument(
        "--skip-feed-meta",
        action="store_true",
        help="個別メタデータ取得を省略し前回の manifest を流用（廃止判定が古くなる）",
    )
    args = ap.parse_args()

    out = Path(args.out)
    feeds_dir = out / "feeds"
    feeds_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"

    prev: dict[str, dict] = {}
    if manifest_path.exists():
        prev = json.loads(manifest_path.read_text(encoding="utf-8")).get("feeds", {})
        print(f"前回の manifest: {len(prev)} フィード")

    print("フィード一覧を取得中 ...")
    files = fetch_file_list()
    print(f"  /v2/files: {len(files)} フィード")

    # --- 個別メタデータ（廃止判定） ---
    if args.skip_feed_meta:
        print("個別メタデータの取得をスキップします（--skip-feed-meta）")
        metas: dict[str, dict] = {}
    else:
        print(f"個別メタデータを取得中（廃止判定・版履歴, 並列{args.jobs}） ...")
        metas = fetch_all_feed_meta(files, args.jobs)

    # --- ダウンロード対象の決定 ---
    todo: list[tuple[str, dict]] = []
    skipped_disc: list[str] = []
    unchanged = 0
    entries: dict[str, dict] = {}

    for f in files:
        key = feed_key(f["organization_id"], f["feed_id"])
        meta = metas.get(key)
        discontinued = bool(meta.get("is_discontinued")) if meta else bool(
            prev.get(key, {}).get("discontinued")
        )
        disc_date = (meta or {}).get("discontinued_date") or prev.get(key, {}).get(
            "discontinued_date", ""
        )

        entry = {
            "organization_id": f["organization_id"],
            "organization_name": f["organization_name"],
            "feed_id": f["feed_id"],
            "feed_name": f["feed_name"],
            "pref_id": f["feed_pref_id"],
            "license": f["feed_license_id"],
            "license_url": f["feed_license_url"],
            "feed_page_url": f["feed_page_url"],
            "file_uid": f["file_uid"],
            "from_date": f["file_from_date"],
            "to_date": f["file_to_date"],
            "last_updated_at": f["file_last_updated_at"],
            "discontinued": discontinued,
            "discontinued_date": disc_date,
        }
        entries[key] = entry

        if discontinued:
            skipped_disc.append(key)
            continue

        zpath = feeds_dir / f"{key}.zip"
        same_uid = prev.get(key, {}).get("file_uid") == f["file_uid"]
        if zpath.exists() and same_uid and not args.force:
            unchanged += 1
            entry["bytes"] = prev[key].get("bytes") or zpath.stat().st_size
            entry["sha256"] = prev[key].get("sha256") or sha256(zpath)
            continue
        todo.append((key, f))

    print()
    print(f"廃止フィード（除外）    : {len(skipped_disc)}")
    for k in skipped_disc:
        e = entries[k]
        print(f"    - {e['organization_name']} / {e['feed_name']} ({e['discontinued_date'] or '日付不明'})")
    print(f"変更なし（再取得しない）: {unchanged}")
    print(f"取得対象                : {len(todo)}")

    # --- 並列ダウンロード ---
    failed: list[tuple[str, str]] = []
    if todo:
        print(f"\nダウンロード中（並列{args.jobs}） ...")
        done = 0
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {
                ex.submit(download_feed, f["file_url"], feeds_dir / f"{key}.zip"): key
                for key, f in todo
            }
            for fut in as_completed(futs):
                key = futs[fut]
                ok, err = fut.result()
                done += 1
                if ok:
                    p = feeds_dir / f"{key}.zip"
                    entries[key]["bytes"] = p.stat().st_size
                    entries[key]["sha256"] = sha256(p)
                else:
                    failed.append((key, err))
                    print(f"  FAIL {key}: {err}", flush=True)
                if done % 25 == 0 or done == len(futs):
                    print(f"  {done}/{len(futs)}", flush=True)

    # --- manifest 書き出し ---
    # 再現性（どの版で作ったか）と CC BY の帰属表示の材料を兼ねる
    manifest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
        "source": f"{API}/files",
        "source_name": "GTFSデータリポジトリ (gtfs-data.jp)",
        "counts": {
            "listed": len(files),
            "discontinued": len(skipped_disc),
            "unchanged": unchanged,
            "downloaded": len(todo) - len(failed),
            "failed": len(failed),
        },
        "feeds": entries,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print()
    print(f"manifest: {manifest_path}")
    if failed:
        print(f"\n失敗 {len(failed)} 件（再実行すれば未取得分のみ再試行されます）:")
        for k, e in failed:
            print(f"  {k}: {e}")
        return 1
    print("完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
