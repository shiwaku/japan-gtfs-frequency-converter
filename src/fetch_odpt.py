#!/usr/bin/env python3
"""公共交通オープンデータセンター (ODPT) から GTFS-JP フィードを取得する。

  python3 src/fetch_odpt.py [--out data] [--jobs 6] [--force] [--token KEY]

gtfs-data.jp（src/fetch.py）に無い事業者を埋めるためのもの。都営バス・京王バス・
横浜市営バスなど、大都市圏の主要事業者はこちら側にしかない。

取得経路:
  ODPT には機械可読なデータカタログAPIが無い（ckan.odpt.org/api/3/action/* は
  HTML を返す）ので、CKAN の HTML を res_format=GTFS/GTFS-JP で絞って辿る。
  データセットページ → リソースページ → 実体の ZIP URL、の3段。

配信元は2種類あり、扱いが違う:
  * api-public.odpt.org — アクセストークン不要（48データセット）
  * api.odpt.org        — acl:consumerKey が必須（60データセット）
    トークンは --token / 環境変数 ODPT_ACCESS_TOKEN / ~/.odpt_token の順で探す。
    無ければ該当データセットは status=no_token でスキップする。

同一データセットに複数リソースがあるのは、多くが「同じファイルの版違い」
（?date=YYYYMMDD が異なる）。ファイル名ごとに最新の日付を選ぶ。ファイル名が
違うもの（稲城市 iバスの A/B コースなど）は別々に取得する。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CKAN = "https://ckan.odpt.org"
RES_FORMAT = "GTFS%2FGTFS-JP"
UA = "japan-gtfs-frequency-converter/0.1 (+https://github.com/shiwaku/japan-gtfs-frequency-converter)"
MAX_RETRY = 4
TOKEN_PLACEHOLDER_RE = re.compile(r"acl:consumerKey=\[[^\]]*\]")


def normalize_url(url: str) -> str:
    """パスに日本語を含む URL がある（小豆島町の「gtfs-町営バス三都西線…zip」など）ので
    非 ASCII だけをパーセントエンコードする。既存の %XX は壊さない。"""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, urllib.parse.quote(p.path, safe="/%"), p.query, p.fragment)
    )


def get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        normalize_url(url), headers={"User-Agent": UA, "Accept-Encoding": "gzip"}
    )
    last: Exception | None = None
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip

                    data = gzip.decompress(data)
                return data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"取得失敗: {url}: {last}")


def get_text(url: str) -> str:
    return get(url).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# カタログの走査
# ---------------------------------------------------------------------------
def dataset_paths() -> list[str]:
    """GTFS/GTFS-JP 形式を持つデータセットのパスを全ページ分集める。"""
    found: list[str] = []
    for page in range(1, 30):
        t = get_text(f"{CKAN}/dataset?res_format={RES_FORMAT}&page={page}")
        links = [l for l in re.findall(r'href="(/dataset/[^"#?]+)"', t) if l != "/dataset/"]
        new = [l for l in dict.fromkeys(links) if l not in found]
        if not new:
            break
        found.extend(new)
    return found


def zip_urls(dataset_path: str) -> tuple[str, list[str]]:
    """データセットページからタイトルと、リソースページ上の ZIP URL を集める。"""
    t = get_text(CKAN + dataset_path)
    m = re.search(r"<title>(.*?)</title>", t, re.S)
    title = html.unescape(m.group(1)).split(" - データセット")[0].strip() if m else ""
    urls: list[str] = []
    for rp in dict.fromkeys(re.findall(r'href="(/dataset/[^"]*/resource/[^"]+)"', t)):
        rt = get_text(CKAN + rp)
        for u in re.findall(r'href="(https?://api(?:-public)?\.odpt\.org/[^"]+)"', rt):
            u = html.unescape(u)
            if ".zip" in u.lower() and u not in urls:
                urls.append(u)
    return title, urls


def pick_latest(urls: list[str]) -> dict[str, str]:
    """{ファイルパス: 最新の URL}。版違い（?date=）は最新だけ残す。"""
    best: dict[str, tuple[str, str]] = {}
    for u in urls:
        parts = urllib.parse.urlsplit(u)
        date = urllib.parse.parse_qs(parts.query).get("date", [""])[0]
        prev = best.get(parts.path)
        if prev is None or date > prev[0]:
            best[parts.path] = (date, u)
    return {p: u for p, (_, u) in best.items()}


def feed_key(dataset_key: str, url_path: str) -> str:
    """1データセットが複数ファイルを持つことがあるので、ファイル名も鍵に含める。"""
    stem = re.sub(r"\.zip$", "", url_path.rsplit("/", 1)[-1], flags=re.I)
    stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", stem)[:60]
    return f"{dataset_key}__{stem}"


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def find_token(cli: str) -> str:
    if cli:
        return cli.strip()
    env = os.environ.get("ODPT_ACCESS_TOKEN", "").strip()
    if env:
        return env
    p = Path.home() / ".odpt_token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def apply_token(url: str, token: str) -> str:
    """URL 中のトークン欄を埋める。api-public はそもそも欄が無い。"""
    if "acl:consumerKey" not in url:
        return url
    return TOKEN_PLACEHOLDER_RE.sub(f"acl:consumerKey={urllib.parse.quote(token)}", url)


def needs_token(url: str) -> bool:
    return "acl:consumerKey" in url


def download(entry: dict, out_dir: Path, token: str, force: bool) -> dict:
    key, url = entry["key"], entry["url"]
    result = {**entry, "status": "ok", "note": "", "bytes": 0}
    if needs_token(url) and not token:
        result["status"] = "no_token"
        result["note"] = "アクセストークンが必要（--token / ODPT_ACCESS_TOKEN / ~/.odpt_token）"
        return result

    dest = out_dir / f"{key}.zip"
    if dest.exists() and not force and entry.get("unchanged"):
        result["status"] = "cached"
        result["bytes"] = dest.stat().st_size
        return result

    try:
        blob = get(apply_token(url, token), timeout=300)
    except Exception as e:
        result["status"] = "error"
        result["note"] = str(e)[:200]
        return result

    tmp = dest.with_suffix(".zip.part")
    tmp.write_bytes(blob)
    # ZIP として開けるかまで見ないと、エラーページを掴まされていても気づけない
    try:
        with zipfile.ZipFile(tmp) as z:
            names = {n.split("/")[-1] for n in z.namelist()}
            if "stop_times.txt" not in names or "trips.txt" not in names:
                raise ValueError(f"GTFS に見えない: {sorted(names)[:6]}")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        result["status"] = "error"
        result["note"] = f"ZIP 検証失敗: {e}"[:200]
        return result

    tmp.replace(dest)
    result["bytes"] = dest.stat().st_size
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data", help="出力ディレクトリ (default: data)")
    ap.add_argument("--jobs", type=int, default=6, help="並列数 (default: 6)")
    ap.add_argument("--force", action="store_true", help="URL が同じでも再取得する")
    ap.add_argument("--token", default="", help="ODPT アクセストークン（既定は環境変数/~/.odpt_token）")
    ap.add_argument("--catalog-only", action="store_true", help="カタログ走査だけして終わる")
    args = ap.parse_args()

    out = Path(args.out)
    feeds_dir = out / "feeds_odpt"
    feeds_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest_odpt.json"
    prev = {}
    if manifest_path.exists():
        prev = json.loads(manifest_path.read_text(encoding="utf-8")).get("feeds", {})

    token = find_token(args.token)
    print(f"アクセストークン: {'あり' if token else 'なし（api-public のみ取得）'}")

    paths = dataset_paths()
    print(f"カタログ: GTFS/GTFS-JP のデータセット {len(paths)} 件。リソースを走査中…")

    entries: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(zip_urls, p): p for p in paths}
        for i, f in enumerate(as_completed(futs), 1):
            p = futs[f]
            dataset_key = p.rsplit("/", 1)[-1]
            try:
                title, urls = f.result()
            except Exception as e:
                print(f"  [warn] {dataset_key}: {e}", file=sys.stderr)
                continue
            for url_path, url in pick_latest(urls).items():
                k = feed_key(dataset_key, url_path)
                entries[k] = {
                    "key": k,
                    "dataset": dataset_key,
                    "title": title,
                    "url": url,
                    "needs_token": needs_token(url),
                    "unchanged": prev.get(k, {}).get("url") == url,
                }
            if i % 20 == 0:
                print(f"  …{i}/{len(paths)}", flush=True)

    n_tok = sum(1 for e in entries.values() if e["needs_token"])
    print(f"ZIP: {len(entries)} 本（うちトークン必須 {n_tok}）")
    if args.catalog_only:
        for e in sorted(entries.values(), key=lambda x: x["key"]):
            print(f'  {"[要トークン]" if e["needs_token"] else "[公開]      "} {e["title"][:40]:40} {e["url"]}')
        return 0

    results = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(download, e, feeds_dir, token, args.force) for e in entries.values()]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results[r["key"]] = r
            if r["status"] not in ("ok", "cached"):
                print(f'  [{r["status"]}] {r["title"][:36]}: {r["note"]}')
            if i % 20 == 0:
                print(f"  …{i}/{len(entries)}", flush=True)

    count: dict[str, int] = {}
    for r in results.values():
        count[r["status"]] = count.get(r["status"], 0) + 1

    feeds = {}
    for k, r in sorted(results.items()):
        if r["status"] not in ("ok", "cached"):
            continue
        feeds[k] = {
            "organization_name": r["title"].split(" / ")[0].strip(),
            "feed_name": r["dataset"],
            "pref_id": 0,  # ODPT のカタログには都道府県コードが無い
            "license": "",  # データセットごとに異なる。CKAN のページを参照
            "source": "odpt",
            "dataset_url": f'{CKAN}/dataset/{r["dataset"]}',
            "url": r["url"],
            "needs_token": r["needs_token"],
            "discontinued": False,
            "bytes": r["bytes"],
        }

    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
                "source": CKAN,
                "source_name": "公共交通オープンデータセンター (ODPT)",
                "counts": count,
                "feeds": feeds,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    total = sum(f["bytes"] for f in feeds.values())
    print(f"\n状態: {count}")
    print(f"保存: {len(feeds)} フィード / {total/1e6:.1f} MB → {feeds_dir}")
    print(f"マニフェスト: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
