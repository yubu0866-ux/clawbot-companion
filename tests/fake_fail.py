#!/usr/bin/env python3
"""selftest 用的必失败模型：计数后以非 0 退出。

HTTP 模式（--serve PORT）：起本地服务，所有请求一律回 HTTP 500 并计数，
供通用版 llm.py（OpenAI 兼容 HTTP）的主备切换自检使用。"""
import os
import sys

count_file = os.environ.get("FAKE_FAIL_COUNT")


def bump():
    if count_file:
        with open(count_file, "a", encoding="utf-8") as f:
            f.write("fail\n")


def serve(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            bump()
            data = b'{"error": {"message": "simulated model failure"}}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]))
    sys.stdin.read()
    bump()
    print("模拟模型故障", file=sys.stderr)
    sys.exit(3)
