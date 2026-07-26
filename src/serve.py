#!/usr/bin/env python3
"""プレビュー用のローカル配信サーバ。

    python3 src/serve.py [port]
    -> http://127.0.0.1:8787/src/preview.html

標準の http.server は Range リクエストに応答しないので PMTiles を配信できない。
206 Partial Content だけ足した最小実装。
"""
from __future__ import annotations

import http.server
import os
import re
import socketserver
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        m = RANGE_RE.match(rng.strip())
        path = self.translate_path(self.path)
        if not m or not os.path.isfile(path):
            return super().send_head()

        size = os.path.getsize(path)
        start_s, end_s = m.group(1), m.group(2)
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:  # bytes=-N (末尾 N バイト)
            start, end = max(0, size - int(end_s)), size - 1
        end = min(end, size - 1)
        if start > end:
            self.send_error(416, "Requested Range Not Satisfiable")
            return None

        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        # copyfile が全部読まないよう、範囲分だけを渡す
        return _Slice(f, end - start + 1)

    def log_message(self, fmt, *args):  # 静かに
        pass


class _Slice:
    """copyfile に渡すための、残りバイト数を持つ読み出しラッパ。"""

    def __init__(self, f, remaining: int):
        self.f, self.remaining = f, remaining

    def read(self, n: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if n is None or n < 0:
            n = self.remaining
        data = self.f.read(min(n, self.remaining))
        self.remaining -= len(data)
        return data

    def close(self):
        self.f.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    with Server(("127.0.0.1", port), Handler) as httpd:
        print(f"http://127.0.0.1:{port}/src/preview.html  (root={ROOT})", flush=True)
        httpd.serve_forever()
