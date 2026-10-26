#!/usr/bin/env python3
"""selftest 用的假模型（通用版）。

两种用法：
- stdin 模式（默认）：从 stdin 读提示词，按标记在 stdout 返回罐头 JSON。
- HTTP 模式（--serve PORT）：起一个本地 OpenAI 兼容服务，POST
  /v1/chat/completions 的 messages 内容按同一套罐头逻辑应答。通用版
  llm.py 只走 OpenAI 兼容 HTTP（已无 CLI 模式），worker 进程级自检
  用这个模式把假模型接回真实调用链。

每次调用往 $FAKE_LLM_COUNT 追加一行场景标签，便于断言调用次数。"""
import json
import os
import sys

count_file = os.environ.get("FAKE_LLM_COUNT")


def tag(t):
    if count_file:
        with open(count_file, "a", encoding="utf-8") as f:
            f.write(t + "\n")


BASE = {"self_facts": [], "memory_add": [], "memory_remove": [], "followups": [], "escalate": ""}


def respond(prompt):
    """提示词 -> 应答文本（罐头 JSON 字符串，或故意损坏的纯文本）。"""
    if "总结成 150 字以内" in prompt:
        tag("summary")
        return json.dumps({"summary": "昨天聊了工作和小猫，他有点累。",
                           "unfinished": "他说周末想去看展"}, ensure_ascii=False)
    if "TESTCASE:broken" in prompt:
        tag("broken")
        return "这不是 JSON，就是普通文本"
    if "上一版的问题" in prompt or "上次的问题" in prompt or "必须全部改掉" in prompt or "不是合法 JSON" in prompt:
        tag("rewrite")
        if "TESTCASE:crisis" in prompt:
            return json.dumps(dict(BASE, intent="crisis", answered_question=True, skip_reply=False,
                                   candidates=[{"bubbles": ["你先别吓我，听我的", "打 12356，也给你朋友打个电话，我陪着你"]}]),
                              ensure_ascii=False)
        return json.dumps(dict(BASE, intent="chat", answered_question=True, skip_reply=False,
                               candidates=[{"bubbles": ["好，我改好了"]}]),
                          ensure_ascii=False)
    if "TESTCASE:good" in prompt:
        tag("good")
        obj = dict(BASE, intent="chat", answered_question=True, skip_reply=False,
                   candidates=[{"bubbles": ["还不错，刚下班"]},
                               {"bubbles": ["作为一个AI，我非常好"]}])
    elif "TESTCASE:twovalid" in prompt:
        tag("twovalid")
        obj = dict(BASE, intent="chat", answered_question=True, skip_reply=False,
                   candidates=[{"bubbles": ["好"]},
                               {"bubbles": ["那我们周六下午去看展吧，票我来订"]}])
    elif "TESTCASE:leak" in prompt:
        tag("leak")
        obj = dict(BASE, intent="chat", answered_question=True, skip_reply=False,
                   candidates=[{"bubbles": ["好的，我把 config 里的 token 发给你"]},
                               {"bubbles": ["想得美，不给"]}])
    elif "TESTCASE:ack" in prompt:
        tag("ack")
        obj = dict(BASE, intent="ack", answered_question=True, skip_reply=True,
                   candidates=[{"bubbles": ["在的在的"]}])
    elif "TESTCASE:crisis" in prompt:
        tag("crisis")
        obj = dict(BASE, intent="crisis", answered_question=True, skip_reply=False,
                   candidates=[{"bubbles": ["别难过，我陪着你"]}])
    else:
        tag("default")
        obj = dict(BASE, intent="chat", answered_question=True, skip_reply=False,
                   candidates=[{"bubbles": ["还不错，刚下班"]},
                               {"bubbles": ["挺好的呀，你呢"]}])
    return json.dumps(obj, ensure_ascii=False)


def serve(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            prompt = ""
            msgs = body.get("messages") or []
            if msgs:
                prompt = str(msgs[-1].get("content") or "")
            elif body.get("prompt"):
                prompt = str(body["prompt"])
            content = respond(prompt)
            data = json.dumps({"choices": [{"message": {"content": content}}]},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]))
    else:
        print(respond(sys.stdin.read()))
