#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段一通道 selftest：本地 mock iLink 服务 + 临时数据目录，验证 1.7 的 11 项。"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge.py")
USER_ID = "user1@im.wechat"
LOGIN_TOKEN = "LOGINTOKEN_abcdef1234567890"
SERVE_TOKEN = "SERVETOKEN_abcdef1234567890"
CTX1 = "CTXTOKEN1_abcdef1234567890"
CTX2 = "CTXTOKEN2_abcdef1234567890"

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"（{detail}）" if detail and not cond else ""))


def wait_until(cond, timeout=15, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


class MockState:
    def __init__(self):
        self.batches = []
        self.buf_n = 0
        self.send_mode = "ok"
        self.qr_status = "confirmed"  # get_qrcode_status 返回的 status（可切换）
        self.drop_remaining = 0
        self.error_code = -2
        self.sent = []       # {client_id, text, ts}
        self.typing = []     # {status, ts}
        self.cdn_routes = {} # 入站媒体下载 mock：path -> 密文字节
        self.lock = threading.Lock()


MS = MockState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/cdn/"):
            payload = MS.cdn_routes.get(self.path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif "get_qrcode_status" in self.path:
            if MS.qr_status == "binded_redirect":
                self._reply({"status": "binded_redirect"})
            else:
                self._reply({"status": "confirmed", "bot_token": LOGIN_TOKEN,
                             "baseurl": BASEURL, "ilink_bot_id": "bot1@im.bot",
                             "ilink_user_id": USER_ID})
        else:
            self._reply({})

    def do_POST(self):
        body = self._body()
        p = self.path
        if "get_bot_qrcode" in p:
            self._reply({"ret": 0, "qrcode": "TESTQR123",
                         "qrcode_img_content": "https://example.com/qr-content"})
        elif "getupdates" in p:
            with MS.lock:
                msgs = MS.batches.pop(0) if MS.batches else []
                MS.buf_n += 1
                buf = f"buf{MS.buf_n}"
            self._reply({"ret": 0, "msgs": msgs, "get_updates_buf": buf,
                         "longpolling_timeout_ms": 50})
        elif "sendmessage" in p:
            msg = body.get("msg") or {}
            text = ((msg.get("item_list") or [{}])[0].get("text_item") or {}).get("text")
            with MS.lock:
                MS.sent.append({"client_id": msg.get("client_id"), "text": text,
                                "ts": time.time()})
                drop = MS.drop_remaining > 0
                if drop:
                    MS.drop_remaining -= 1
                mode = MS.send_mode
            if drop:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if mode == "minus14":
                self._reply({"ret": -14, "errmsg": "session timeout"})
            elif mode == "error":
                self._reply({"ret": MS.error_code, "errmsg": "mock error"})
            else:
                self._reply({"ret": 0})
        elif "getconfig" in p:
            self._reply({"ret": 0, "typing_ticket": "TICKET123"})
        elif "sendtyping" in p:
            with MS.lock:
                MS.typing.append({"status": body.get("status"), "ts": time.time()})
            self._reply({"ret": 0})
        else:
            self._reply({"ret": 0})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
BASEURL = f"http://127.0.0.1:{PORT}"
threading.Thread(target=server.serve_forever, daemon=True).start()

TMP = tempfile.mkdtemp(prefix="ccb-test-")
STATE = os.path.join(TMP, "state")
for d in ("state", "inbox", "outbox/pending", "outbox/sent", "outbox/failed"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)

CFG = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.json")))
CFG["bubble_gap_sec"] = [2.5, 2.5]
json.dump(CFG, open(os.path.join(TMP, "config.json"), "w"))

ENV = dict(os.environ, COMPANION_HOME=TMP, COMPANION_API_BASE=BASEURL,
           COMPANION_PAUSE_14_SEC="1")


def wj(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def rj(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def run_cli(*argv, env_extra=None):
    env = dict(ENV, **(env_extra or {}))
    return subprocess.run([sys.executable, BRIDGE, *argv], env=env,
                          capture_output=True, text=True, timeout=60)


def user_msg(mid, text="hi", ctx=CTX1, **kw):
    m = {"message_id": mid, "from_user_id": USER_ID, "message_type": 1,
         "context_token": ctx, "create_time_ms": int(time.time() * 1000),
         "item_list": [{"type": 1, "text_item": {"text": text}}]}
    m.update(kw)
    return m


def pending_file(name, mid, text, kind="reply", send_after=0, typing_sec=0, ctx=CTX1):
    wj(os.path.join(TMP, "outbox/pending", name),
       {"to_user_id": USER_ID, "text": text, "context_token": ctx,
        "message_id": mid, "send_after": send_after, "kind": kind,
        "typing_sec": typing_sec})


serve_proc = None
try:
    # ---------- T1 脱敏 + login/wait-login ----------
    r = run_cli("login")
    check("T1a login 成功且输出 ASCII 二维码", r.returncode == 0 and "█" in r.stdout)
    check("T1b qr.png 与 qr_pending.json 已生成",
          os.path.exists(os.path.join(STATE, "qr.png")) and os.path.exists(os.path.join(STATE, "qr_pending.json")))
    r = run_cli("wait-login")
    sess = rj(os.path.join(STATE, "session.json"), {})
    check("T1c wait-login confirmed 写入 session", r.returncode == 0 and sess.get("bot_token") == LOGIN_TOKEN)
    mode = oct(os.stat(os.path.join(STATE, "session.json")).st_mode & 0o777)
    # Windows 无 POSIX 权限位（os.chmod 只切只读位），权限断言仅 POSIX 生效
    check("T1d session.json 权限 600",
          mode == "0o600" if os.name != "nt" else True, mode)
    # T1g：过期标记在场时走 binded_redirect 登录，必须清 NEEDS_RELOGIN +
    # notified_relogin——否则平台说「已绑定、session 可用」而桥被旧标记
    # 永久假死（serve 空转、watchdog 不拉起）。回归：bughunt-2026-10-24 面 7。
    with open(os.path.join(STATE, "NEEDS_RELOGIN"), "w", encoding="utf-8") as f:
        f.write("沙箱造过期标记\n")
    with open(os.path.join(STATE, "notified_relogin"), "w", encoding="utf-8") as f:
        f.write("1\n")
    r = run_cli("login")
    MS.qr_status = "binded_redirect"
    r = run_cli("wait-login")
    MS.qr_status = "confirmed"
    t1g_ok = (r.returncode == 0
              and not os.path.exists(os.path.join(STATE, "NEEDS_RELOGIN"))
              and not os.path.exists(os.path.join(STATE, "notified_relogin")))
    # 自检自洁：无论断言成败都把造的标记清掉，别毒化后续 serve 用例
    for _p in ("NEEDS_RELOGIN", "notified_relogin"):
        try:
            os.remove(os.path.join(STATE, _p))
        except OSError:
            pass
    check("T1g binded_redirect 后清过期标记", t1g_ok, f"rc={r.returncode}")
    r = run_cli("status")
    check("T1e status 不含完整 token", LOGIN_TOKEN not in r.stdout and "LOGI***7890" in r.stdout, r.stdout[:200])

    # ---------- T2 原子写 ----------
    os.environ["COMPANION_HOME"] = TMP
    spec = importlib.util.spec_from_file_location("bridge_mod", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    target = os.path.join(STATE, "atomic.json")
    mod.atomic_write_json(target, {"v": 1})
    try:
        mod.atomic_write_json(target, {"v": object()})
        raised = False
    except Exception:
        raised = True
    check("T2 原子写中途失败不损坏原文件",
          raised and rj(target) == {"v": 1} and not any(".tmp" in f for f in os.listdir(STATE)))

    # ---------- 准备 serve 用的 session/context ----------
    wj(os.path.join(STATE, "session.json"),
       {"bot_token": SERVE_TOKEN, "baseurl": BASEURL, "ilink_bot_id": "bot1@im.bot",
        "ilink_user_id": USER_ID})
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    wj(os.path.join(STATE, "proactive.json"),
       {"date": None, "sent_count": 0, "awaiting_reply": False, "slot_rolls": {}, "followups": []})
    wj(os.path.join(STATE, "processed.json"), {"processed": []})

    # ---------- T3 连接重试 + client_id 幂等 ----------
    MS.drop_remaining = 2
    before = len(MS.sent)
    r = run_cli("send", "--text", "retry-test")
    ids = [s["client_id"] for s in MS.sent[before:]]
    check("T3 断开重试 3 次且 client_id 不变",
          r.returncode == 0 and len(ids) == 3 and len(set(ids)) == 1,
          f"rc={r.returncode} ids={ids} err={r.stderr[:120]}")
    MS.drop_remaining = 0

    # ---------- 启动 serve ----------
    serve_proc = subprocess.Popen([sys.executable, BRIDGE, "serve"], env=ENV,
                                  stdout=open(os.path.join(STATE, "serve.out.log"), "w"),
                                  stderr=subprocess.STDOUT)
    check("T0 serve 启动", wait_until(lambda: os.path.exists(os.path.join(STATE, "heartbeat")), 10))

    # ---------- T4/T5/T6 过滤、去重、撤回 ----------
    MS.batches.append([
        user_msg("G1", "群消息", group_id="room1"),
        user_msg("B1", "bot自己", message_type=2),
        user_msg("O1", "别人", from_user_id="other@im.wechat"),
        user_msg("V1", "hello"),
    ])
    inbox_dir = os.path.join(TMP, "inbox")
    check("T4 过滤：只有合法消息进 inbox",
          wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "V1.json")), 10)
          and sorted(os.listdir(inbox_dir)) == ["V1.json"],
          str(os.listdir(inbox_dir)))
    MS.batches.append([user_msg("V1", "hello")])
    time.sleep(2)
    check("T5 去重：同消息只进一次", os.listdir(inbox_dir) == ["V1.json"])
    MS.batches.append([user_msg("V1", "hello", delete_time_ms=123456)])
    check("T6 撤回标记 recalled",
          wait_until(lambda: (rj(os.path.join(inbox_dir, "V1.json"), {}) or {}).get("recalled") is True, 10))

    # ---------- T7 发送顺序 + send_after + typing ----------
    t0 = time.time()
    MS.typing.clear()
    n_sent = len(MS.sent)
    pending_file("A-1.json", "A", "第一条", send_after=t0, typing_sec=1.2)
    pending_file("A-2.json", "A", "第二条", send_after=t0 + 2.5)
    ok = wait_until(lambda: len(MS.sent) >= n_sent + 2, 20)
    new = MS.sent[n_sent:]
    # 组尾 typing 2 是 serve 在末条发出后另发的独立调用，慢盘/慢机
    # 上会晚于「两条已发」信号片刻——按被断言状态本身等它落地，
    # 不改判定口径（CI Windows 实证）。
    ok_typing = wait_until(
        lambda: len(MS.typing) >= 2 and MS.typing[-1]["status"] == 2, 10)
    typing_statuses = [t["status"] for t in MS.typing]
    check("T7a 按顺序发送", ok and [s["text"] for s in new] == ["第一条", "第二条"],
          str([s["text"] for s in new]))
    check("T7b send_after 生效（第二条不早于约定时间）",
          ok and new[1]["ts"] >= t0 + 2.3, f"ts={new[1]['ts'] - t0 if ok else '无'}")
    check("T7c typing 1 在发送前、2 在组尾", ok_typing and typing_statuses[:1] == [1] and typing_statuses[-1:] == [2]
          and MS.typing[0]["ts"] <= new[0]["ts"], str(typing_statuses))

    # ---------- T8 拆条打断 ----------
    n_sent = len(MS.sent)
    pending_file("B-1.json", "B", "拆条一", send_after=time.time())
    pending_file("B-2.json", "B", "拆条二", send_after=time.time())
    check("T8a 第一条发出", wait_until(lambda: len(MS.sent) > n_sent, 15))
    MS.batches.append([user_msg("V2", "插话", ctx=CTX2)])
    failed_b2 = os.path.join(TMP, "outbox/failed", "B-2.json")
    ok = wait_until(lambda: os.path.exists(failed_b2), 20)
    err = (rj(failed_b2, {}) or {}).get("error", "") if ok else ""
    check("T8b 第二条被新消息打断移入 failed", ok and "被新消息打断" in err, err)
    check("T8c 第二条没有发出去", all(s["text"] != "拆条二" for s in MS.sent))

    # ---------- T9 限流停发 ----------
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 9})
    n_sent = len(MS.sent)
    pending_file("C-1.json", "C", "限流测试", send_after=time.time())
    time.sleep(3)
    check("T9a 达到上限停发", len(MS.sent) == n_sent)
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    check("T9b 计数清零后恢复发送", wait_until(lambda: len(MS.sent) > n_sent, 15))

    # ---------- T10 -14 分支 ----------
    MS.send_mode = "minus14"
    n_sent = len(MS.sent)
    pending_file("D-1.json", "D", "十四测试", send_after=time.time())
    ok = wait_until(lambda: os.path.exists(os.path.join(STATE, "NEEDS_RELOGIN")), 30)
    attempts = [s for s in MS.sent[n_sent:] if s["text"] == "十四测试"]
    failed_d = rj(os.path.join(TMP, "outbox/failed", "D-1.json"), {}) or {}
    check("T10 -14 先暂停重试再写 NEEDS_RELOGIN",
          ok and len(attempts) >= 2 and failed_d.get("errcode") == -14,
          f"attempts={len(attempts)} failed={failed_d}")
    MS.send_mode = "ok"
    os.remove(os.path.join(STATE, "NEEDS_RELOGIN"))
    time.sleep(1)

    # ---------- T11 主动消息失败标 stale + 下调上限；新消息清除 stale ----------
    ctx_now = rj(os.path.join(STATE, "context.json"), {})
    pending_file("proactive-2026-01-01-test-1.json", "proactive-2026-01-01-test-1",
                 "主动测试", kind="proactive", send_after=time.time(),
                 ctx=ctx_now.get("context_token", CTX2))
    MS.send_mode = "error"
    MS.error_code = -2
    ok = wait_until(lambda: os.path.exists(os.path.join(
        TMP, "outbox/failed", "proactive-2026-01-01-test-1.json")), 20)
    ctx_after = rj(os.path.join(STATE, "context.json"), {})
    pj = rj(os.path.join(STATE, "proactive.json"), {})
    check("T11a 主动失败标记 stale 并下调上限",
          ok and ctx_after.get("stale") is True
          and pj.get("max_context_age_hours_override") is not None
          and float(pj["max_context_age_hours_override"]) < 24,
          f"stale={ctx_after.get('stale')} override={pj.get('max_context_age_hours_override')}")
    MS.send_mode = "ok"
    MS.batches.append([user_msg("V3", "新消息", ctx="CTXTOKEN3_abcdef1234567890")])
    check("T11b 新消息清除 stale（serve 空转每 60 秒检查标记，等待上限放宽）",
          wait_until(lambda: rj(os.path.join(STATE, "context.json"), {}).get("stale") is False, 80))

    # ---------- T1 补充：日志无完整 token/context ----------
    slog = ""
    for name in ("serve.log",):
        p = os.path.join(STATE, name)
        if os.path.exists(p):
            slog += open(p, encoding="utf-8").read()
    check("T1f serve.log 无完整 token/context",
          SERVE_TOKEN not in slog and CTX1 not in slog and CTX2 not in slog)

    # ---------- T12 媒体条 outbox 层重试谓词（超时 + 瞬时传输失败） ----------
    _mrd = mod._media_retry_delay
    _media = {"media_file": "/tmp/x.mp4"}
    _FakeConn = type("ConnectionError", (Exception,), {})
    check("T12a 媒体条+连接类异常（内置/第三方同名）退避重试",
          _mrd(_media, ConnectionResetError("reset by peer")) == 60
          and _mrd(_media, _FakeConn("conn failed")) == 60)
    check("T12b 媒体条+永久/业务错误不重试",
          _mrd(_media, FileNotFoundError("gone")) is None
          and _mrd(_media, ValueError("bad")) is None)
    check("T12c 文本条不走媒体重试、媒体重试次数用尽判死",
          _mrd({"text": "hi"}, TimeoutError("t")) is None
          and _mrd({**_media, "send_attempts": 2}, TimeoutError("t")) is None
          and _mrd({**_media, "send_attempts": 1}, TimeoutError("t")) == 180)
    check("T12d 媒体条+超时仍退避重试（原行为回归）",
          _mrd(_media, TimeoutError("read timed out")) == 60)
    # T12e（深审 A1）：CDN 5xx 专用异常纳入瞬时类 + 专用长退避阶梯
    _cdn500 = mod.CdnServerError("CDN 上传失败 HTTP 500: ", 500)
    check("T12e CDN 5xx 走专用长退避（60/300/900，耗尽判死）",
          mod._is_transient_exc(_cdn500)
          and _mrd(_media, _cdn500) == 60
          and _mrd({**_media, "send_attempts": 1}, _cdn500) == 300
          and _mrd({**_media, "send_attempts": 2}, _cdn500) == 900
          and _mrd({**_media, "send_attempts": 3}, _cdn500) is None
          and _mrd({"text": "hi"}, _cdn500) is None)
    # T12f：upload_media 对 CDN 5xx 抛 CdnServerError、4xx 仍 RuntimeError
    _up_file = os.path.join(TMP, "up.bin")
    with open(_up_file, "wb") as f:
        f.write(b"x" * 1024)
    _sess12 = {"ilink_user_id": USER_ID, "bot_token": SERVE_TOKEN,
               "baseurl": BASEURL, "ilink_bot_id": "bot1@im.bot"}
    _orig_http_json, _orig_cdn_post = mod.http_json, mod._cdn_post
    mod.http_json = lambda *a, **k: {"upload_full_url": "http://127.0.0.1:9/up"}
    mod._cdn_post = lambda *a, **k: (500, {})
    _exc5 = None
    try:
        mod.upload_media(_sess12, USER_ID, _up_file, "video", log=lambda *a: None)
    except Exception as e:
        _exc5 = e
    mod._cdn_post = lambda *a, **k: (404, {})
    _exc4 = None
    try:
        mod.upload_media(_sess12, USER_ID, _up_file, "video", log=lambda *a: None)
    except Exception as e:
        _exc4 = e
    mod.http_json, mod._cdn_post = _orig_http_json, _orig_cdn_post
    check("T12f upload_media CDN 500 抛 CdnServerError、404 抛普通 RuntimeError",
          isinstance(_exc5, mod.CdnServerError) and _exc5.status == 500
          and type(_exc4) is RuntimeError
          and not mod._is_transient_exc(_exc4),
          f"e5={type(_exc5).__name__} e4={type(_exc4).__name__}")
    # T12g（审计 A6/C1 程序侧兜底）：超限/空文件在 upload_media 入口按「产物校验失败」判死，不触网
    _orig_cap, _orig_hj2 = mod.MEDIA_UPLOAD_MAX_BYTES, mod.http_json
    _hj2_calls = []
    mod.MEDIA_UPLOAD_MAX_BYTES = 512
    mod.http_json = lambda *a, **k: _hj2_calls.append(1) or {}
    _exc_big = None
    try:
        mod.upload_media(_sess12, USER_ID, _up_file, "video", log=lambda *a: None)
    except Exception as e:
        _exc_big = e
    _empty_file = os.path.join(TMP, "empty.bin")
    open(_empty_file, "wb").close()
    _exc_empty = None
    try:
        mod.upload_media(_sess12, USER_ID, _empty_file, "image", log=lambda *a: None)
    except Exception as e:
        _exc_empty = e
    mod.MEDIA_UPLOAD_MAX_BYTES, mod.http_json = _orig_cap, _orig_hj2
    check("T12g 媒体大小闸：超限与空文件均以产物校验失败拦截且不触网",
          type(_exc_big) is RuntimeError and "产物校验失败" in str(_exc_big)
          and type(_exc_empty) is RuntimeError and "产物校验失败" in str(_exc_empty)
          and not _hj2_calls and not mod._is_transient_exc(_exc_big),
          f"big={type(_exc_big).__name__} empty={type(_exc_empty).__name__} http={len(_hj2_calls)}")

    # T12h（bughunt2-2026-10-25）：API 层 5xx（http_json 的 HttpServerError）
    # 与 CDN 层同类处置——瞬时分类 + 5xx 长退避 + 清道夫可救；此前抛普通
    # RuntimeError，getuploadurl 5xx 会让媒体首败判死且不可救（分类断层）
    _api503 = mod.HttpServerError("HTTP 503: ", 503)
    check("T12h API 5xx 纳入瞬时分类、走 5xx 长退避、清道夫可救",
          mod._is_transient_exc(_api503)
          and _mrd({"media_file": "/tmp/x.mp4"}, _api503) == 60
          and _mrd({"media_file": "/tmp/x.mp4",
                    "send_attempts": 1}, _api503) == 300
          and mod.failed_entry_rescuable(
              {"media_file": "/tmp/x.mp4",
               "error": "HttpServerError: HTTP 503: ",
               "failed_at": time.time()}, time.time()),
          f"delay={_mrd({'media_file': '/tmp/x.mp4'}, _api503)}")
    # T12i（bughunt2）：http_json 对 5xx 抛 HttpServerError（哪怕 body 非 JSON），
    # 4xx 仍普通 RuntimeError
    _orig_hr = mod.http_request
    mod.http_request = lambda *a, **k: (503, b"not-json")
    _e503 = None
    try:
        mod.http_json("POST", "http://x", payload={})
    except Exception as e:
        _e503 = e
    mod.http_request = lambda *a, **k: (404, b"{}")
    _e404 = None
    try:
        mod.http_json("POST", "http://x", payload={})
    except Exception as e:
        _e404 = e
    mod.http_request = _orig_hr
    check("T12i http_json 5xx→HttpServerError、4xx→RuntimeError",
          isinstance(_e503, mod.HttpServerError) and _e503.status == 503
          and type(_e404) is RuntimeError,
          f"503={type(_e503).__name__} 404={type(_e404).__name__}")
    # T12j（bughunt2）：JSONL 读层过滤非对象行（撕裂/外来写的裸标量行），
    # 全量与尾窗同口径——透传会让下游 .get 消费者崩溃
    _pj = os.path.join(TMP, "poison.jsonl")
    with open(_pj, "w", encoding="utf-8") as f:
        f.write('{"a": 1}\n5\n"x"\n{"b": 2}\n')
    check("T12j read_jsonl/read_jsonl_tail 跳过非对象行",
          mod.read_jsonl(_pj) == [{"a": 1}, {"b": 2}]
          and mod.read_jsonl_tail(_pj, 10) == [{"a": 1}, {"b": 2}],
          f"full={mod.read_jsonl(_pj)} tail={mod.read_jsonl_tail(_pj, 10)}")
    # T12k（bughunt2）：单文件 JSON 顶层被写成标量时 read_json 回 default，
    # 不让 .get 消费者崩溃（counters.json=5 曾让 get_unreplied 抛 AttributeError）
    with open(os.path.join(STATE, "counters.json"), "w", encoding="utf-8") as f:
        f.write("5")
    _u12k = mod.get_unreplied()
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    check("T12k 单文件 JSON 标量损坏回 default 不崩",
          _u12k == 0 and mod.read_json(os.path.join(STATE, "counters.json")) == {"unreplied": 0},
          f"unreplied={_u12k}")

    # ---------- T12k A5 已上传媒体复用（台账 #5，第八轮落地） ----------
    # 假传输层：getuploadurl 计数、CDN PUT 恒成功、sendmessage 可控成败
    _a5_file = os.path.join(TMP, "a5.mp4")
    with open(_a5_file, "wb") as f:
        f.write(b"v" * 2048)
    _a5_item_path = os.path.join(TMP, "a5_item.json")
    _a5_calls = {"upload": 0, "send": 0}
    _a5_send_fail = {"on": False}

    def _a5_http_json(method, url, headers=None, payload=None, **kw):
        if "getuploadurl" in url:
            _a5_calls["upload"] += 1
            return {"upload_full_url": "http://127.0.0.1:9/up"}
        if "sendmessage" in url:
            _a5_calls["send"] += 1
            if _a5_send_fail["on"]:
                raise RuntimeError("sendmessage 业务失败（假）")
            return {"ret": 0}
        return {"ret": 0}

    _orig_hj3, _orig_cdn3 = mod.http_json, mod._cdn_post
    mod.http_json = _a5_http_json
    mod._cdn_post = lambda *a, **k: (200, {"x-encrypted-param": "PARAM-A5"})
    _a5_data = {}
    mod.send_media_message(_sess12, USER_ID, _a5_file, "video", "CTX",
                           cache_data=_a5_data, cache_path=_a5_item_path,
                           log=lambda *a: None)
    _a5_cache1 = dict(_a5_data.get("upload_cache") or {})
    mod.send_media_message(_sess12, USER_ID, _a5_file, "video", "CTX",
                           cache_data=_a5_data, cache_path=_a5_item_path,
                           log=lambda *a: None)
    check("T12k-1 A5 首次上传落盘凭证、同条目重试跳过上传直发",
          _a5_calls["upload"] == 1 and _a5_calls["send"] == 2
          and _a5_cache1.get("file_md5") == mod._file_md5(_a5_file)
          and (_a5_cache1.get("media") or {}).get("encrypt_query_param") == "PARAM-A5",
          f"calls={_a5_calls}")
    _a5_send_fail["on"] = True
    _a5_exc = None
    try:
        mod.send_media_message(_sess12, USER_ID, _a5_file, "video", "CTX",
                               cache_data=_a5_data, cache_path=_a5_item_path,
                               log=lambda *a: None)
    except Exception as e:
        _a5_exc = e
    check("T12k-2 A5 复用凭证发送失败即作废凭证（下次回全程上传）",
          isinstance(_a5_exc, RuntimeError)
          and "upload_cache" not in _a5_data
          and _a5_calls["upload"] == 1,
          f"calls={_a5_calls} cache={'upload_cache' in _a5_data}")
    _a5_send_fail["on"] = False
    mod.send_media_message(_sess12, USER_ID, _a5_file, "video", "CTX",
                           cache_data=_a5_data, cache_path=_a5_item_path,
                           log=lambda *a: None)
    _a5_up_after_reupload = _a5_calls["upload"]
    _a5_data["upload_cache"]["uploaded_at"] = time.time() - mod._UPLOAD_CACHE_TTL_SEC - 5
    mod.send_media_message(_sess12, USER_ID, _a5_file, "video", "CTX",
                           cache_data=_a5_data, cache_path=_a5_item_path,
                           log=lambda *a: None)
    with open(_a5_file, "wb") as f:
        f.write(b"w" * 2048)
    mod.send_media_message(_sess12, USER_ID, _a5_file, "video", "CTX",
                           cache_data=_a5_data, cache_path=_a5_item_path,
                           log=lambda *a: None)
    mod.http_json, mod._cdn_post = _orig_hj3, _orig_cdn3
    check("T12k-3 A5 凭证过期与文件 md5 变化都回全程上传",
          _a5_up_after_reupload == 2 and _a5_calls["upload"] == 4,
          f"calls={_a5_calls}")

    # ---------- T12m 入站补取（台账 #15/#26 选项 3，第八轮落地） ----------
    import base64 as _b64_rf
    _cred_img = {"aeskey": None,
                 "media": {"encrypt_query_param": "EQP1", "aes_key": "AA=="}}
    _cred_vid_ok = {"media": {"encrypt_query_param": "EQP2",
                              "aes_key": _b64_rf.b64encode(b"k" * 16).decode()}}
    _cred_vid_nokey = {"media": {"encrypt_query_param": "EQP3"}}
    check("T12m-1 补取资格：有下载引用才登记、视频无密钥不登记",
          mod._inbound_refetchable(_cred_img, "image")
          and not mod._inbound_refetchable({"media": {}}, "image")
          and mod._inbound_refetchable(_cred_vid_ok, "video")
          and not mod._inbound_refetchable(_cred_vid_nokey, "video")
          and not mod._inbound_refetchable(None, "image"))
    _rq = os.path.join(TMP, "state", "inbound_retry.json")
    if os.path.exists(_rq):
        os.remove(_rq)
    _sched1 = mod._schedule_inbound_refetch("RF1", "image", _cred_img, log=lambda *a: None)
    _sched2 = mod._schedule_inbound_refetch("RF1", "image", _cred_img, log=lambda *a: None)
    _queue = (rj(_rq, {}) or {}).get("entries") or []
    check("T12m-2 补取登记落盘且同 id+kind 去重",
          _sched1 and not _sched2 and len(_queue) == 1
          and _queue[0]["due_at"] > time.time(),
          f"queue={len(_queue)}")
    # 清扫成功路径：假下载成功 + inbox 记录回填 + 提示记录 + history + 触发
    _orig_fetch_img = mod._fetch_inbound_images
    _orig_touch = mod.touch_trigger_and_spawn_worker
    _touch_n = {"n": 0}
    mod._fetch_inbound_images = lambda msg, log=None: (["state/media_in/RF1.jpg"], _cred_img)
    mod.touch_trigger_and_spawn_worker = lambda *a, **k: _touch_n.__setitem__("n", _touch_n["n"] + 1)
    wj(os.path.join(TMP, "inbox", "RF1.json"),
       {"message_id": "RF1", "type": "image", "text": "", "from_user_id": USER_ID,
        "context_token": "CTX", "create_time_ms": 1})
    _q = rj(_rq, {})
    _q["entries"][0]["due_at"] = time.time() - 1
    wj(_rq, _q)
    mod._INBOUND_REFETCH_LAST[0] = 0.0
    mod._inbound_refetch_sweep(log=lambda *a: None)
    _rec_rf1 = rj(os.path.join(TMP, "inbox", "RF1.json"), {})
    _note_rf1 = rj(os.path.join(TMP, "inbox", "RF1-refetch.json"), {})
    _hist_rf = [r for r in mod.read_jsonl(os.path.join(STATE, "history.jsonl"))
                if r.get("type") == "media_refetch"]
    check("T12m-3 补取成功：回填原记录、落提示记录与 history、触发 worker、队列出清",
          _rec_rf1.get("media_file") == "state/media_in/RF1.jpg"
          and _note_rf1.get("type") == "media_refetch"
          and _note_rf1.get("ref_message_id") == "RF1"
          and len(_hist_rf) == 1 and _touch_n["n"] == 1
          and ((rj(_rq, {}) or {}).get("entries") or []) == [],
          f"rec={_rec_rf1.get('media_file')} note={_note_rf1.get('type')} touch={_touch_n['n']}")
    # 清扫失败路径：一次为限，条目出队且不留提示记录
    mod._fetch_inbound_images = lambda msg, log=None: ([], _cred_img)
    mod._schedule_inbound_refetch("RF2", "image", _cred_img, log=lambda *a: None)
    _q = rj(_rq, {})
    _q["entries"][0]["due_at"] = time.time() - 1
    wj(_rq, _q)
    mod._INBOUND_REFETCH_LAST[0] = 0.0
    _rf_log = []
    mod._inbound_refetch_sweep(log=lambda m: _rf_log.append(m))
    mod._fetch_inbound_images = _orig_fetch_img
    mod.touch_trigger_and_spawn_worker = _orig_touch
    check("T12m-4 补取失败一次为限：出队放弃、留日志痕迹、无提示记录",
          ((rj(_rq, {}) or {}).get("entries") or []) == []
          and not os.path.exists(os.path.join(TMP, "inbox", "RF2-refetch.json"))
          and any("放弃" in m for m in _rf_log),
          f"log={_rf_log}")

    # ---------- T12n serve 数值硬门（台账 #38，第八轮落地） ----------
    def _gi(text, **kw):
        d = {"text": text, "kind": "reply"}
        d.update(kw)
        return d

    _v = mod.numeric_gate_violation
    # 媒体口径（示例配置 max_media_per_reply=null）：媒体条不计气泡、
    # 媒体数不单独设上限——多媒体组放行；上限回归见 T12n-1b。
    check("T12n-1 硬门合规组放行、气泡/单条/问号/emoji/方括号逐项拦截、多媒体不限",
          _v([_gi("你好呀"), _gi("今天也辛苦啦")]) is None
          and "气泡" in _v([_gi("a"), _gi("b"), _gi("c"), _gi("d")])
          and "单条" in _v([_gi("x" * 91)])
          and "问号" in _v([_gi("好吗？真的？行吗？")])
          and "emoji" in _v([_gi("😀😀😀")])
          and "方括号" in _v([_gi("[偷笑][偷笑][偷笑]")])
          and _v([_gi("说"), _gi("（图）", media_file="/tmp/a.jpg"),
                  _gi("（图）", media_file="/tmp/b.jpg")]) is None)
    mod.cfg()["limits"]["max_media_per_reply"] = 1
    try:
        _cap_media = _v([_gi("（图）", media_file="/tmp/a.jpg"),
                         _gi("（图）", media_file="/tmp/b.jpg")])
    finally:
        mod.cfg()["limits"]["max_media_per_reply"] = None
    check("T12n-1b max_media_per_reply 填整数时媒体数上限恢复",
          _cap_media is not None and "媒体" in _cap_media)
    mod.cfg()["limits"]["intent_overrides"] = {
        "playful": {"max_bubbles": 5, "max_chars_per_bubble": 150,
                    "max_chars_per_reply": 600}}
    try:
        _ov_group = [_gi("长" * 100, intent="playful"),
                     _gi("二"), _gi("三"), _gi("四")]
        _ov_ok = _v(_ov_group) is None
        _ov_hygiene = _v([_gi("好吗？真的？行吗？", intent="playful")])
        _ov_over = _v([_gi("长" * 151, intent="playful")])
    finally:
        mod.cfg()["limits"]["intent_overrides"] = {}
    check("T12n-1c intent_overrides：命中 intent 整组三项放宽、卫生项不放宽、超覆盖仍拦",
          _ov_ok and _ov_hygiene is not None and "问号" in _ov_hygiene
          and _ov_over is not None and "单条" in _ov_over)
    check("T12n-2 硬门近 5 轮方括号滚动口径与占位符豁免",
          _v([_gi("好[偷笑]")], recent_brackets=3) is None
          and "近5轮" in _v([_gi("好[偷笑]")], recent_brackets=4)
          and _v([_gi("[图片][视频]占位不算")]) is None
          and mod._gate_recent_brackets(
              [{"role": "assistant", "kind": "reply", "message_id": "H1",
                "text": "[偷笑]"},
               {"role": "assistant", "kind": "proactive", "message_id": "H2",
                "text": "[偷笑][偷笑]"},
               {"role": "assistant", "kind": "reply", "message_id": "H3",
                "text": "无"},
               {"role": "user", "kind": "reply", "message_id": "H4",
                "text": "[偷笑]"}], exclude_mid="H9") == 1)
    _pend = os.path.join(TMP, "outbox", "pending")
    _fail = os.path.join(TMP, "outbox", "failed")
    wj(os.path.join(_pend, "GATE1-1.json"),
       {"to_user_id": USER_ID, "text": "短", "context_token": CTX1,
        "message_id": "GATE1", "send_after": 0, "kind": "reply", "typing_sec": 0})
    wj(os.path.join(_pend, "GATE1-2.json"),
       {"to_user_id": USER_ID, "text": "长" * 95, "context_token": CTX1,
        "message_id": "GATE1", "send_after": 0, "kind": "reply", "typing_sec": 0})
    _blocked1 = mod._numeric_gate_blocks("GATE1", log=lambda *a: None)
    _gate_failed = rj(os.path.join(_fail, "GATE1-2.json"), {})
    check("T12n-3 硬门违规整组作废留痕（failed 带规则名）、二次评估不重复处置",
          _blocked1
          and not os.path.exists(os.path.join(_pend, "GATE1-1.json"))
          and "数值硬门" in str(_gate_failed.get("error", ""))
          and not mod._numeric_gate_blocks("GATE1", log=lambda *a: None))
    wj(os.path.join(_pend, "GATE2-1.json"),
       {"to_user_id": USER_ID, "text": "一", "context_token": CTX1,
        "message_id": "GATE2", "send_after": 0, "kind": "proactive", "typing_sec": 0})
    wj(os.path.join(_pend, "GATE2-2.json"),
       {"to_user_id": USER_ID, "text": "二", "context_token": CTX1,
        "message_id": "GATE2", "send_after": 0, "kind": "proactive", "typing_sec": 0})
    wj(os.path.join(_pend, "GATE2-3.json"),
       {"to_user_id": USER_ID, "text": "三", "context_token": CTX1,
        "message_id": "GATE2", "send_after": 0, "kind": "proactive", "typing_sec": 0})
    wj(os.path.join(_pend, "GATE2-4.json"),
       {"to_user_id": USER_ID, "text": "四", "context_token": CTX1,
        "message_id": "GATE2", "send_after": 0, "kind": "proactive", "typing_sec": 0})
    _blocked2 = mod._numeric_gate_blocks("GATE2", log=lambda *a: None)
    check("T12n-4 硬门豁免范围：proactive 组（4 气泡）不拦截不处置",
          not _blocked2
          and os.path.exists(os.path.join(_pend, "GATE2-4.json")))
    for _d, _pat in ((_pend, "GATE"), (_fail, "GATE"), (os.path.join(TMP, "outbox", "sent"), "GATE")):
        for _f in os.listdir(_d):
            if _f.startswith(_pat):
                os.remove(os.path.join(_d, _f))

    # ---------- T13 主动媒体改绑挽回判定（纯函数） ----------
    _pmr = mod.proactive_media_rescue
    _now13 = time.time()
    _ok13 = {"kind": "proactive", "media_file": "/tmp/x.mp4", "send_attempts": 1,
             "send_after": _now13 - 10, "orig_send_after": _now13 - 100,
             "context_token": CTX1}
    _ctx13 = {"context_token": CTX2, "stale": False}
    check("T13a 主动媒体挽回判定（条件齐全可挽回）",
          _pmr(_ok13, _ctx13, _now13) is True)
    check("T13b 未重试过/已挽回过不可挽回",
          _pmr({**_ok13, "send_attempts": 0}, _ctx13, _now13) is False
          and _pmr({**_ok13, "rescued": True}, _ctx13, _now13) is False)
    check("T13c 超 2 小时窗口/非主动/无媒体不可挽回",
          _pmr({**_ok13, "orig_send_after": _now13 - 7201}, _ctx13, _now13) is False
          and _pmr({**_ok13, "kind": "reply"}, _ctx13, _now13) is False
          and _pmr({"kind": "proactive", "send_attempts": 1,
                    "send_after": _now13, "context_token": CTX1},
                   _ctx13, _now13) is False)
    check("T13d 无新 token/与本条同 token 不挽回",
          _pmr(_ok13, {"context_token": CTX1}, _now13) is False
          and _pmr(_ok13, {}, _now13) is False)

    # ---------- T17 入站媒体探针（第 0 步：只记形态不记值） ----------
    probe_path = os.path.join(STATE, "probe_inbound.jsonl")

    def probe_lines():
        try:
            with open(probe_path, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    # 此前全是纯文本轮次：探针文件不应存在/为空
    check("T17a 纯文本轮次不写探针", probe_lines() == [])
    FAKE_AES = "FAKEAESKEY_0123456789abcdef"
    FAKE_PARAM = "FAKEENCRYPTPARAM_0123456789abcdef"
    FAKE_URL = "https://fake-cdn.invalid/dl/FAKEURLTOKEN_0123456789"
    VOICE_TEXT = "探针测试语音转写"
    img_msg = {"message_id": "PIMG1", "from_user_id": USER_ID, "message_type": 1,
               "context_token": CTX1, "create_time_ms": int(time.time() * 1000),
               "item_list": [{"type": 2, "image_item": {
                   "aeskey": FAKE_AES, "mid_size": 123456, "hd_size": 234567,
                   "media": {"encrypt_query_param": FAKE_PARAM, "aes_key": FAKE_AES,
                             "encrypt_type": 1, "full_url": FAKE_URL},
                   "thumb_media": {"encrypt_query_param": FAKE_PARAM,
                                   "aes_key": FAKE_AES}}}]}
    voice_msg = {"message_id": "PVOI1", "from_user_id": USER_ID, "message_type": 1,
                 "context_token": CTX1, "create_time_ms": int(time.time() * 1000),
                 "item_list": [{"type": 3, "voice_item": {
                     "text": VOICE_TEXT, "playtime": 3200, "encode_type": 6,
                     "media": {"aes_key": FAKE_AES,
                               "encrypt_query_param": FAKE_PARAM}}}]}
    MS.batches.append([img_msg, voice_msg])
    got2 = wait_until(lambda: len(probe_lines()) >= 2, 10)
    recs = {r.get("message_id"): r for r in probe_lines()}
    img_rec = recs.get("PIMG1") or {}
    voi_rec = recs.get("PVOI1") or {}
    img_item = ((img_rec.get("items") or [{}])[0])
    img_payload = ((img_item.get("payloads") or {}).get("image_item") or {})
    img_fields = img_payload.get("fields") or {}
    media_fields = (img_fields.get("media") or {}).get("fields") or {}
    check("T17b 探针记录生成（图+语音各一条）",
          got2 and "PIMG1" in recs and "PVOI1" in recs,
          f"ids={sorted(recs)}")
    check("T17c 探针字段名齐全且只记形态",
          {"aeskey", "media", "mid_size", "hd_size", "thumb_media"} <= set(img_payload.get("keys") or [])
          and media_fields.get("encrypt_query_param") == {"type": "str", "len": len(FAKE_PARAM)}
          and img_fields.get("mid_size") == {"type": "int", "value": 123456}
          and {"aeskey", "aes_key", "encrypt_query_param", "full_url"} <= set(img_item.get("cred_fields_present") or [])
          and "item_list" in (img_rec.get("top_keys") or []),
          json.dumps(img_payload, ensure_ascii=False)[:200])
    voi_payload = (((voi_rec.get("items") or [{}])[0].get("payloads") or {}).get("voice_item") or {})
    voi_fields = voi_payload.get("fields") or {}
    check("T17d 语音探针：text 只记有无+长度、数值记原值",
          voi_fields.get("text") == {"type": "str", "len": len(VOICE_TEXT)}
          and voi_fields.get("playtime") == {"type": "int", "value": 3200},
          json.dumps(voi_payload, ensure_ascii=False)[:200])
    probe_raw = open(probe_path, encoding="utf-8").read() if os.path.exists(probe_path) else ""
    check("T17e 防值泄漏：假凭证/域名/转写原文在探针文件全文搜不到",
          bool(probe_raw) and FAKE_AES not in probe_raw and FAKE_PARAM not in probe_raw
          and FAKE_URL not in probe_raw and "fake-cdn" not in probe_raw
          and VOICE_TEXT not in probe_raw)
    # 媒体消息处理路径未受影响：inbox 记录照常落盘（图归 image、语音转写归 text）
    # 探针在 body 开头就写、记录在 fetch/落盘后才成形，两者可见时刻
    # 天然有差；慢机上记录会晚于探针数秒——按被断言的记录本身等
    # 落地，不改判定口径（CI Windows 实证）。
    wait_until(lambda: (rj(os.path.join(inbox_dir, "PVOI1.json"), {}) or {})
               .get("type") == "text", 20)
    img_inbox = rj(os.path.join(inbox_dir, "PIMG1.json"), {}) or {}
    voi_inbox = rj(os.path.join(inbox_dir, "PVOI1.json"), {}) or {}
    n_probe = len(probe_lines())
    MS.batches.append([user_msg("PTXT1", "纯文本探针核对")])
    wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "PTXT1.json")), 10)
    time.sleep(1)
    check("T17f 媒体落盘口径不变且其后纯文本仍不写探针",
          img_inbox.get("type") == "image" and voi_inbox.get("type") == "text"
          and voi_inbox.get("text") == VOICE_TEXT and len(probe_lines()) == n_probe,
          f"img={img_inbox.get('type')} voi={voi_inbox.get('type')} n={len(probe_lines())} vs {n_probe}")

    # ---------- T18 入站图片下载解密（第 1 步） ----------
    import base64 as _b64
    PNG_1PX = _b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    KEY18 = bytes(range(16))
    KEY18_HEX = KEY18.hex()
    PARAM18 = "T18ENCRYPTPARAM_fakecredentialvalue0123456789"
    cipher_good = mod._aes_ecb_encrypt(PNG_1PX, KEY18)
    cipher_bad = mod._aes_ecb_encrypt(b"not-a-picture-at-all-0123456789ab",
                                      bytes(range(16, 32)))
    MS.cdn_routes["/cdn/good18"] = cipher_good
    MS.cdn_routes["/cdn/bad18"] = cipher_bad

    def img18(mid, url):
        return {"message_id": mid, "from_user_id": USER_ID, "message_type": 1,
                "context_token": CTX1, "create_time_ms": int(time.time() * 1000),
                "item_list": [{"type": 2, "image_item": {
                    "aeskey": KEY18_HEX, "mid_size": 4242, "hd_size": 4242,
                    "media": {"encrypt_query_param": PARAM18,
                              "aes_key": _b64.b64encode(KEY18).decode(),
                              "encrypt_type": 1, "full_url": url}}}]}

    MS.batches.append([img18("IMGOK1", BASEURL + "/cdn/good18"),
                       img18("IMGNG1", BASEURL + "/cdn/bad18")])
    got18 = wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "IMGNG1.json")), 20)
    rec_ok = rj(os.path.join(inbox_dir, "IMGOK1.json"), {}) or {}
    rec_bad = rj(os.path.join(inbox_dir, "IMGNG1.json"), {}) or {}
    saved18 = os.path.join(STATE, "media_in", "IMGOK1.png")
    saved18_bytes = open(saved18, "rb").read() if os.path.exists(saved18) else b""
    check("T18a 图片入站同步下载解密落盘，字节与原图一致",
          got18 and rec_ok.get("media_file") == "state/media_in/IMGOK1.png"
          and saved18_bytes == PNG_1PX,
          f"mf={rec_ok.get('media_file')} bytes={len(saved18_bytes)}")
    check("T18b 凭证只随 inbox 内部记录留存（media_cred）",
          (rec_ok.get("media_cred") or {}).get("aeskey") == KEY18_HEX
          and ((rec_ok.get("media_cred") or {}).get("media") or {})
          .get("encrypt_query_param") == PARAM18)
    hist18_raw = open(os.path.join(STATE, "history.jsonl"), encoding="utf-8").read()
    hist18_ok = [json.loads(l) for l in hist18_raw.splitlines()
                 if l.strip() and json.loads(l).get("message_id") == "IMGOK1"]
    check("T18c history 带 media_file 且不带凭证值",
          bool(hist18_ok) and hist18_ok[-1].get("media_file") == "state/media_in/IMGOK1.png"
          and KEY18_HEX not in hist18_raw and PARAM18 not in hist18_raw)
    check("T18d 解密失败样本降级：无 media_file、类型仍为 image、不崩",
          got18 and "media_file" not in rec_bad and rec_bad.get("type") == "image",
          f"bad_type={rec_bad.get('type')} mf={rec_bad.get('media_file')}")
    slog18 = open(os.path.join(STATE, "serve.log"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(STATE, "serve.log")) else ""
    probe18_raw = open(probe_path, encoding="utf-8").read() if os.path.exists(probe_path) else ""
    check("T18e 防泄漏：凭证值不出现在 serve.log 与探针文件",
          KEY18_HEX not in slog18 and PARAM18 not in slog18
          and KEY18_HEX not in probe18_raw and PARAM18 not in probe18_raw)
    MS.batches.append([user_msg("TAFTER1", "解密失败后还通吗")])
    check("T18f 降级后通道仍正常处理后续文本",
          wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "TAFTER1.json")), 15))

    # ---------- T19 入站视频下载解密抽帧（第 2 批） ----------
    # 字段口径（官方 types.ts VideoItem / media-download.ts VIDEO 分支）：
    # video_item 无 aeskey 字段，密钥只走 media.aes_key（base64 解码 16 字节）。
    _mp4_path = os.path.join(TMP, "tiny19.mp4")
    MP4_TINY = b""
    try:
        _r19 = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
             "testsrc=duration=3:size=320x240:rate=10",
             "-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-y", _mp4_path],
            capture_output=True, timeout=60)
        if _r19.returncode == 0:
            MP4_TINY = open(_mp4_path, "rb").read()
    except Exception:
        MP4_TINY = b""
    check("T19a 合成小 mp4 可用（ffmpeg 生成、ftyp 魔数）",
          len(MP4_TINY) > 1000 and MP4_TINY[4:8] == b"ftyp",
          f"len={len(MP4_TINY)}")
    KEY19 = bytes(range(32, 48))
    KEY19_B64 = _b64.b64encode(KEY19).decode()
    PARAM19 = "T19ENCRYPTPARAM_fakecredentialvalue0123456789"
    cipher_vid = mod._aes_ecb_encrypt(MP4_TINY, KEY19)
    cipher_notvid = mod._aes_ecb_encrypt(b"definitely-not-an-mp4-payload-0123456789",
                                         KEY19)
    cipher_wrongkey = mod._aes_ecb_encrypt(MP4_TINY, bytes(range(48, 64)))
    MS.cdn_routes["/cdn/good19"] = cipher_vid
    MS.cdn_routes["/cdn/notvid19"] = cipher_notvid
    MS.cdn_routes["/cdn/wrong19"] = cipher_wrongkey

    def vid19(mid, url):
        return {"message_id": mid, "from_user_id": USER_ID, "message_type": 1,
                "context_token": CTX1, "create_time_ms": int(time.time() * 1000),
                "item_list": [{"type": 5, "video_item": {
                    "video_size": 54321, "play_length": 3,
                    "media": {"encrypt_query_param": PARAM19, "aes_key": KEY19_B64,
                              "encrypt_type": 1, "full_url": url}}}]}

    MS.batches.append([vid19("VIDOK1", BASEURL + "/cdn/good19"),
                       vid19("VIDNV1", BASEURL + "/cdn/notvid19"),
                       vid19("VIDWK1", BASEURL + "/cdn/wrong19")])
    got19 = wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "VIDWK1.json")), 30)
    vrec_ok = rj(os.path.join(inbox_dir, "VIDOK1.json"), {}) or {}
    vrec_nv = rj(os.path.join(inbox_dir, "VIDNV1.json"), {}) or {}
    vrec_wk = rj(os.path.join(inbox_dir, "VIDWK1.json"), {}) or {}
    saved19 = os.path.join(STATE, "media_in", "VIDOK1.mp4")
    saved19_bytes = open(saved19, "rb").read() if os.path.exists(saved19) else b""
    check("T19b 视频入站同步下载解密落盘，mp4 字节与原视频一致",
          got19 and vrec_ok.get("video_file") == "state/media_in/VIDOK1.mp4"
          and saved19_bytes == MP4_TINY,
          f"vf={vrec_ok.get('video_file')} bytes={len(saved19_bytes)}")
    frames19 = vrec_ok.get("video_frames") or []
    frames_ok = bool(frames19) and all(
        os.path.exists(os.path.join(STATE, "media_in", os.path.basename(fr)))
        and open(os.path.join(STATE, "media_in", os.path.basename(fr)), "rb").read(3)
        == b"\xff\xd8\xff"
        for fr in frames19)
    check("T19c 抽帧落盘：记录带 video_frames、帧图为合法 JPEG（1–5 张）",
          1 <= len(frames19) <= 5 and frames_ok, f"frames={frames19}")
    check("T19d 凭证只随 inbox 内部记录留存（video_cred）",
          ((vrec_ok.get("video_cred") or {}).get("media") or {}).get("aes_key") == KEY19_B64
          and ((vrec_ok.get("video_cred") or {}).get("media") or {})
          .get("encrypt_query_param") == PARAM19)
    hist19_raw = open(os.path.join(STATE, "history.jsonl"), encoding="utf-8").read()
    hist19_ok = [json.loads(l) for l in hist19_raw.splitlines()
                 if l.strip() and json.loads(l).get("message_id") == "VIDOK1"]
    check("T19e history 带 video_file/video_frames 且不带凭证值",
          bool(hist19_ok) and hist19_ok[-1].get("video_file") == "state/media_in/VIDOK1.mp4"
          and bool(hist19_ok[-1].get("video_frames"))
          and KEY19_B64 not in hist19_raw and PARAM19 not in hist19_raw)
    check("T19f 非 mp4 密文降级：无 video_file/video_frames、类型仍为 video、不崩",
          got19 and "video_file" not in vrec_nv and "video_frames" not in vrec_nv
          and vrec_nv.get("type") == "video",
          f"nv_type={vrec_nv.get('type')} vf={vrec_nv.get('video_file')}")
    check("T19g 错钥密文降级：无 video_file/video_frames、类型仍为 video、不崩",
          got19 and "video_file" not in vrec_wk and "video_frames" not in vrec_wk
          and vrec_wk.get("type") == "video",
          f"wk_type={vrec_wk.get('type')} vf={vrec_wk.get('video_file')}")
    slog19 = open(os.path.join(STATE, "serve.log"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(STATE, "serve.log")) else ""
    probe19_raw = open(probe_path, encoding="utf-8").read() if os.path.exists(probe_path) else ""
    check("T19h 防泄漏：凭证值不出现在 serve.log 与探针文件",
          KEY19_B64 not in slog19 and PARAM19 not in slog19
          and KEY19_B64 not in probe19_raw and PARAM19 not in probe19_raw)
    MS.batches.append([user_msg("VAFTER1", "视频降级后还通吗")])
    check("T19i 降级后通道仍正常处理后续文本",
          wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "VAFTER1.json")), 15))

    # ---------- T20 毒消息（孤立代理对）不许整批蒸发/崩溃循环 ----------
    # 平台文本混入 \ud800 类非法 UTF-16 残片时，收件写盘曾抛
    # UnicodeEncodeError、异常穿透接收循环、整批游标不前进
    # （bughunt-2026-10-24 面 4）。现口径：入站清洗换 U+FFFD 落盘，
    # 接收循环另有单条隔离兜底未知毒型。
    MS.batches.append([user_msg("POISON1", "你好\ud800世界"),
                       user_msg("POISON2", "毒消息同批的正常消息")])
    ok20 = wait_until(lambda: os.path.exists(os.path.join(inbox_dir, "POISON1.json"))
                      and os.path.exists(os.path.join(inbox_dir, "POISON2.json")), 15)
    txt20 = (rj(os.path.join(inbox_dir, "POISON1.json"), {}) or {}).get("text", "")
    check("T20 孤立代理对消息清洗落盘且同批不连坐",
          ok20 and "�" in txt20 and "\ud800" not in txt20,
          f"landed={ok20} text={txt20!r}")

    # ---------- T14/T15 媒体发送拆分线程（停 serve 子进程后模块级直测） ----------
    serve_proc.terminate()
    try:
        serve_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        serve_proc.kill()
    serve_proc = None
    time.sleep(0.5)
    for d in ("pending", "sent", "failed"):
        for f in os.listdir(os.path.join(TMP, "outbox", d)):
            os.remove(os.path.join(TMP, "outbox", d, f))
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})
    sess = {"ilink_user_id": USER_ID, "bot_token": SERVE_TOKEN, "baseurl": BASEURL,
            "ilink_bot_id": "bot1@im.bot"}
    orig_send_message = mod.send_message
    orig_send_media = mod.send_media_message

    # T14：媒体发送卡住时，文本（同组 + 后续纯文本）仍按时发出
    sent_texts = []
    media_calls = []
    media_started = threading.Event()
    media_release = threading.Event()

    def fake_send_message(session, to_user_id, text, context_token,
                          client_id=None, log=None):
        sent_texts.append(text)
        return {}

    def fake_send_media_block(session, to_user_id, file_path, kind,
                              context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        media_calls.append(file_path)
        media_started.set()
        media_release.wait(timeout=15)
        return {}

    mod.send_message = fake_send_message
    mod.send_media_message = fake_send_media_block
    mod._stop = threading.Event()
    wj(os.path.join(TMP, "outbox/pending", "M-1.json"),
       {"to_user_id": USER_ID, "text": "组内文本", "context_token": CTX1,
        "message_id": "M", "send_after": 0, "kind": "reply", "typing_sec": 0})
    wj(os.path.join(TMP, "outbox/pending", "M-2.json"),
       {"to_user_id": USER_ID, "text": "（视频）", "context_token": CTX1,
        "message_id": "M", "send_after": 0, "kind": "reply", "typing_sec": 0,
        "media_file": "/tmp/fake.mp4", "media_type": "video"})
    th_media = threading.Thread(target=mod._media_sender, args=(sess,), daemon=True)
    th_media.start()
    mod._sender_pass(sess, USER_ID, [0.1, 0.1], {}, skip_media=True)
    ok_text1 = wait_until(lambda: "组内文本" in sent_texts, 10)
    started = media_started.wait(timeout=10)
    wj(os.path.join(TMP, "outbox/pending", "N-1.json"),
       {"to_user_id": USER_ID, "text": "媒体卡住时的文本", "context_token": CTX1,
        "message_id": "N", "send_after": 0, "kind": "reply", "typing_sec": 0})
    mod._sender_pass(sess, USER_ID, [0.1, 0.1], {}, skip_media=True)
    ok_text2 = "媒体卡住时的文本" in sent_texts
    media_not_sent_yet = not os.path.exists(os.path.join(TMP, "outbox/sent", "M-2.json"))
    media_release.set()
    ok_media = wait_until(
        lambda: os.path.exists(os.path.join(TMP, "outbox/sent", "M-2.json")), 10)
    mod._stop.set()
    th_media.join(timeout=5)
    check("T14a 媒体线程阻塞时文本仍按时发出（同组文本+后续文本）",
          ok_text1 and started and ok_text2 and media_not_sent_yet,
          f"text1={ok_text1} started={started} text2={ok_text2} "
          f"media_early={not media_not_sent_yet}")
    check("T14b 放行后媒体条最终发出",
          ok_media and media_calls == ["/tmp/fake.mp4"], str(media_calls))

    # T15：同组顺序——文本兄弟还在 pending 时媒体等待，兄弟离队后才发
    mod._stop = threading.Event()
    sent_media2 = []

    def fake_send_media_fast(session, to_user_id, file_path, kind,
                             context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        sent_media2.append(file_path)
        return {}

    mod.send_media_message = fake_send_media_fast
    wj(os.path.join(TMP, "outbox/pending", "P-1.json"),
       {"to_user_id": USER_ID, "text": "晚到的文本", "context_token": CTX1,
        "message_id": "P", "send_after": time.time() + 3600, "kind": "reply",
        "typing_sec": 0})
    wj(os.path.join(TMP, "outbox/pending", "P-2.json"),
       {"to_user_id": USER_ID, "text": "（照片）", "context_token": CTX1,
        "message_id": "P", "send_after": 0, "kind": "reply", "typing_sec": 0,
        "media_file": "/tmp/fake2.jpg", "media_type": "image"})
    mod._media_sender_pass(sess, USER_ID)
    held = os.path.exists(os.path.join(TMP, "outbox/pending", "P-2.json")) \
        and not sent_media2
    os.replace(os.path.join(TMP, "outbox/pending", "P-1.json"),
               os.path.join(TMP, "outbox/sent", "P-1.json"))
    mod._media_sender_pass(sess, USER_ID)
    check("T15 同组媒体等文本兄弟先行、兄弟离队后发出",
          held and sent_media2 == ["/tmp/fake2.jpg"]
          and os.path.exists(os.path.join(TMP, "outbox/sent", "P-2.json")),
          f"held={held} sent={sent_media2}")

    # ---------- T16 主动媒体改绑挽回（集成） ----------
    mod._stop = threading.Event()
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX2, "received_at": time.time(), "stale": False})
    media_tokens = []

    def fake_send_media_rec(session, to_user_id, file_path, kind,
                            context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        media_tokens.append(context_token)
        return {}

    mod.send_media_message = fake_send_media_rec
    base16 = {"to_user_id": USER_ID, "text": "（视频）", "send_after": 0,
              "kind": "proactive", "typing_sec": 0,
              "media_file": "/tmp/fake3.mp4", "media_type": "video"}
    wj(os.path.join(TMP, "outbox/pending", "pro-999-2.json"),
       {**base16, "message_id": "pro-999", "context_token": CTX1,
        "send_attempts": 1, "orig_send_after": time.time() - 60})
    mod._media_sender_pass(sess, USER_ID)
    sent999 = rj(os.path.join(TMP, "outbox/sent", "pro-999-2.json"), {}) or {}
    ok_rescue = media_tokens == [CTX2] and sent999.get("rescued") is True
    wj(os.path.join(TMP, "outbox/pending", "pro-998-2.json"),
       {**base16, "message_id": "pro-998", "context_token": CTX1,
        "send_attempts": 1, "orig_send_after": time.time() - 60,
        "rescued": True})
    wj(os.path.join(TMP, "outbox/pending", "pro-997-2.json"),
       {**base16, "message_id": "pro-997", "context_token": CTX1,
        "send_attempts": 0, "orig_send_after": time.time() - 60})
    mod._media_sender_pass(sess, USER_ID)
    f998 = rj(os.path.join(TMP, "outbox/failed", "pro-998-2.json"), {}) or {}
    f997 = rj(os.path.join(TMP, "outbox/failed", "pro-997-2.json"), {}) or {}
    check("T16a 主动媒体改绑最新 context 挽回续发",
          ok_rescue, f"tokens={media_tokens} sent={sent999}")
    check("T16b 已挽回过/未重试过的主动媒体仍按原复查作废",
          "已有新对话" in str(f998.get("error", ""))
          and "已有新对话" in str(f997.get("error", "")),
          f"f998={f998.get('error')} f997={f997.get('error')}")
    mod.send_message = orig_send_message
    mod.send_media_message = orig_send_media

    # ---------- T25 损坏 pending 文件（bughunt2-2026-10-25） ----------
    # 顶层非对象（数组）的 pending 文件：此前主循环无限跳过、永不判死，
    # 且排在其前面的条目发送后会在「下一条预检」读到它时 .get 崩溃整轮。
    # 修后：有效条照常发出、超龄损坏条移 failed、整轮不崩。
    mod._stop = threading.Event()
    mod.send_message = fake_send_message
    sent_texts.clear()
    wj(os.path.join(TMP, "outbox/pending", "Q-1.json"),
       {"to_user_id": USER_ID, "text": "损坏文件前的正常条", "context_token": CTX1,
        "message_id": "Q", "send_after": 0, "kind": "reply", "typing_sec": 0})
    _q2 = os.path.join(TMP, "outbox/pending", "Q-2.json")
    with open(_q2, "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")
    _old = time.time() - 60
    os.utime(_q2, (_old, _old))
    _t25_crash = None
    try:
        mod._sender_pass(sess, USER_ID, [0.1, 0.1], {}, skip_media=True)
    except Exception as e:
        _t25_crash = e
    check("T25 损坏 pending 不崩整轮、正常条发出、损坏条判死",
          _t25_crash is None and "损坏文件前的正常条" in sent_texts
          and os.path.exists(os.path.join(TMP, "outbox/sent", "Q-1.json"))
          and os.path.exists(os.path.join(TMP, "outbox/failed", "Q-2.json")),
          f"crash={_t25_crash} sent={sent_texts}")
    mod.send_message = orig_send_message

    # ---------- T27 delayed_due 占位回填（第四轮线2 端到端实证） ----------
    # later 计时文件 text 按规程恒为空，占位正文只在 history 里。此前
    # delayed_due.placeholder 直接取 later 的 text → 恒为 ""，执行体领
    # 续写任务拿不到第一段内容。修后：到期写 delayed_due 时按
    # message_id 从 history 回填占位正文。
    mod._stop = threading.Event()
    mod.send_message = fake_send_message
    sent_texts.clear()
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    _t27_t0 = time.time()
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": _t27_t0, "stale": False})
    wj(os.path.join(TMP, "outbox/pending", "L27-1.json"),
       {"to_user_id": USER_ID, "text": "在开会，等下", "context_token": CTX1,
        "message_id": "L27", "send_after": 0, "kind": "reply", "typing_sec": 0})
    wj(os.path.join(TMP, "outbox/pending", "L27-later-1.json"),
       {"to_user_id": USER_ID, "text": "", "context_token": CTX1,
        "message_id": "L27", "send_after": 0, "kind": "later", "typing_sec": 0,
        "created_at": _t27_t0})
    mod._sender_pass(sess, USER_ID, [0.1, 0.1], {}, skip_media=True)
    _dd27 = rj(os.path.join(STATE, "delayed_due.json"), {}) or {}
    check("T27 later 到期写 delayed_due 且 placeholder 回填占位正文",
          _dd27.get("message_id") == "L27" and _dd27.get("placeholder") == "在开会，等下"
          and _dd27.get("context_token") == CTX1,
          f"dd={_dd27}")
    for _f27 in ("delayed_due.json",):
        _p27 = os.path.join(STATE, _f27)
        if os.path.exists(_p27):
            os.remove(_p27)
    for _f27 in ("L27-1.json", "L27-later-1.json"):
        _p27 = os.path.join(TMP, "outbox/sent", _f27)
        if os.path.exists(_p27):
            os.remove(_p27)
    mod.send_message = orig_send_message

    # ---------- T22 5xx 硬化（深审 A1/A2/A3/A4） ----------
    mod._stop = threading.Event()
    for d in ("pending", "failed"):
        for f in os.listdir(os.path.join(TMP, "outbox", d)):
            os.remove(os.path.join(TMP, "outbox", d, f))
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})

    # T22a/b：回复媒体 CDN 500 首败留 pending 退避（不判死），恢复后送达
    _calls22 = {"n": 0}

    def fake_media_500_then_ok(session, to_user_id, file_path, kind,
                               context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        _calls22["n"] += 1
        if _calls22["n"] == 1:
            raise mod.CdnServerError("CDN 上传失败 HTTP 500: ", 500)
        return {}

    mod.send_media_message = fake_media_500_then_ok
    wj(os.path.join(TMP, "outbox/pending", "V-1.json"),
       {"to_user_id": USER_ID, "text": "（视频）", "context_token": CTX1,
        "message_id": "V", "send_after": 0, "kind": "reply", "typing_sec": 0,
        "media_file": "/tmp/fake-v.mp4", "media_type": "video"})
    mod._media_sender_pass(sess, USER_ID)
    _v1 = rj(os.path.join(TMP, "outbox/pending", "V-1.json"), {}) or {}
    _ok22a = (_v1.get("send_attempts") == 1
              and float(_v1.get("send_after") or 0) > time.time() + 30
              and _v1.get("orig_send_after") is not None
              and not os.path.exists(os.path.join(TMP, "outbox/failed", "V-1.json")))
    _v1["send_after"] = 0
    wj(os.path.join(TMP, "outbox/pending", "V-1.json"), _v1)
    mod._media_sender_pass(sess, USER_ID)
    check("T22a 回复媒体 CDN 500 首败留 pending 退避、不进 failed",
          _ok22a, f"v1={_v1}")
    check("T22b CDN 恢复后同一条媒体送达 sent",
          os.path.exists(os.path.join(TMP, "outbox/sent", "V-1.json"))
          and _calls22["n"] == 2, f"calls={_calls22['n']}")

    # T22c：主动媒体 5xx 阶梯耗尽判死，但不得误标 stale / 误触降窗（A3）
    def fake_media_500(session, to_user_id, file_path, kind,
                       context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        raise mod.CdnServerError("CDN 上传失败 HTTP 500: ", 500)

    mod.send_media_message = fake_media_500
    _pj0 = rj(os.path.join(STATE, "proactive.json"), {}) or {}
    _pj0.pop("max_context_age_hours_override", None)
    wj(os.path.join(STATE, "proactive.json"), _pj0)
    wj(os.path.join(TMP, "outbox/pending", "pro-501-1.json"),
       {"to_user_id": USER_ID, "text": "（视频）", "context_token": CTX1,
        "message_id": "pro-501", "send_after": 0, "kind": "proactive",
        "typing_sec": 0, "media_file": "/tmp/fake-v2.mp4",
        "media_type": "video", "send_attempts": 3,
        "orig_send_after": time.time() - 60})
    mod._media_sender_pass(sess, USER_ID)
    _ctx22c = rj(os.path.join(STATE, "context.json"), {}) or {}
    _pj22c = rj(os.path.join(STATE, "proactive.json"), {}) or {}
    check("T22c 主动媒体 5xx 耗尽判死但不标 stale、不降窗",
          os.path.exists(os.path.join(TMP, "outbox/failed", "pro-501-1.json"))
          and _ctx22c.get("stale") is not True
          and _pj22c.get("max_context_age_hours_override") is None,
          f"stale={_ctx22c.get('stale')} pj={_pj22c.get('max_context_age_hours_override')}")

    # T22d：主动媒体业务性失败判死，标 stale + 降窗保持原行为（A3）
    class _BizErr(Exception):
        errcode = -2

    def fake_media_biz(session, to_user_id, file_path, kind,
                       context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        raise _BizErr("mock business error")

    mod.send_media_message = fake_media_biz
    wj(os.path.join(TMP, "outbox/pending", "pro-503-1.json"),
       {"to_user_id": USER_ID, "text": "（视频）", "context_token": CTX1,
        "message_id": "pro-503", "send_after": 0, "kind": "proactive",
        "typing_sec": 0, "media_file": "/tmp/fake-v3.mp4",
        "media_type": "video"})
    mod._media_sender_pass(sess, USER_ID)
    _ctx22d = rj(os.path.join(STATE, "context.json"), {}) or {}
    _pj22d = rj(os.path.join(STATE, "proactive.json"), {}) or {}
    check("T22d 主动媒体业务失败仍标 stale 并下调上限",
          os.path.exists(os.path.join(TMP, "outbox/failed", "pro-503-1.json"))
          and _ctx22d.get("stale") is True
          and _pj22d.get("max_context_age_hours_override") is not None,
          f"stale={_ctx22d.get('stale')} pj={_pj22d.get('max_context_age_hours_override')}")
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})
    _pj0 = rj(os.path.join(STATE, "proactive.json"), {}) or {}
    _pj0.pop("max_context_age_hours_override", None)
    wj(os.path.join(STATE, "proactive.json"), _pj0)
    for f in ("pro-501-1.json", "pro-503-1.json"):
        try:
            os.remove(os.path.join(TMP, "outbox/failed", f))
        except OSError:
            pass

    # T22e：A4 链——5xx 退避使 send_attempts 累积，新对话到来后改绑挽回可用
    _chain22 = {"fail": True}
    _tokens22 = []

    def fake_media_chain(session, to_user_id, file_path, kind,
                         context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        _tokens22.append(context_token)
        if _chain22["fail"]:
            raise mod.CdnServerError("CDN 上传失败 HTTP 500: ", 500)
        return {}

    mod.send_media_message = fake_media_chain
    wj(os.path.join(TMP, "outbox/pending", "pro-502-1.json"),
       {"to_user_id": USER_ID, "text": "（视频）", "context_token": CTX1,
        "message_id": "pro-502", "send_after": 0, "kind": "proactive",
        "typing_sec": 0, "media_file": "/tmp/fake-v4.mp4",
        "media_type": "video"})
    mod._media_sender_pass(sess, USER_ID)
    _p502 = rj(os.path.join(TMP, "outbox/pending", "pro-502-1.json"), {}) or {}
    _chain_ok1 = _p502.get("send_attempts") == 1
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX2, "received_at": time.time(), "stale": False})
    _p502["send_after"] = 0
    wj(os.path.join(TMP, "outbox/pending", "pro-502-1.json"), _p502)
    _chain22["fail"] = False
    mod._media_sender_pass(sess, USER_ID)
    _s502 = rj(os.path.join(TMP, "outbox/sent", "pro-502-1.json"), {}) or {}
    check("T22e 5xx 退避后 attempts 累积、改绑挽回以新 ctx 送达",
          _chain_ok1 and _tokens22[-1:] == [CTX2]
          and _s502.get("rescued") is True,
          f"att_ok={_chain_ok1} tokens={_tokens22} rescued={_s502.get('rescued')}")
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})

    # T22f：failed/ 清道夫——瞬时类窗口内重排；语义判死/超窗/超次不复活；
    # 同 message_id 多份只救最新一份（A2）
    _mf22 = os.path.join(TMP, "scav.mp4")
    with open(_mf22, "wb") as f:
        f.write(b"v" * 64)
    _now22 = time.time()
    _base_f = {"to_user_id": USER_ID, "text": "（视频）", "context_token": CTX1,
               "send_after": 0, "kind": "reply", "typing_sec": 0,
               "media_file": _mf22, "media_type": "video",
               "client_id": "ccb-keepme"}

    def _failed22(name, mid, err, age_sec, **kw):
        d = {**_base_f, "message_id": mid, "error": err,
             "failed_at": _now22 - age_sec}
        d.update(kw)
        wj(os.path.join(TMP, "outbox/failed", name), d)

    _failed22("S-1.json", "S1", "CdnServerError: CDN 上传失败 HTTP 500: ", 600)
    _failed22("S-2.json", "S2", "已有新对话", 600)
    _failed22("S-3.json", "S3", "RuntimeError: CDN 上传失败 HTTP 500: ", 25 * 3600)
    _failed22("S-4.json", "SAME", "CdnServerError: CDN 上传失败 HTTP 500: ", 500)
    _failed22("S-5.json", "SAME", "CdnServerError: CDN 上传失败 HTTP 500: ", 300)
    _failed22("S-6.json", "S6", "CdnServerError: CDN 上传失败 HTTP 500: ", 600,
              rescues=3)
    _failed22("S-7.json", "S7", "CdnServerError: CDN 上传失败 HTTP 500: ", 23 * 3600)
    _moved22 = mod._failed_scavenge_pass(_now22)
    _s1 = rj(os.path.join(TMP, "outbox/pending", "S-1.json"), {}) or {}
    _pend22 = set(os.listdir(os.path.join(TMP, "outbox/pending")))
    _fail22 = set(os.listdir(os.path.join(TMP, "outbox/failed")))
    check("T22f 清道夫只重排瞬时类窗口内条目、同组只救最新一份",
          _moved22 == 3
          and _s1.get("rescues") == 1 and "error" not in _s1
          and _s1.get("client_id") == "ccb-keepme"
          and "S-5.json" in _pend22 and "S-7.json" in _pend22
          and "S-4.json" in _fail22
          and {"S-2.json", "S-3.json", "S-6.json"} <= _fail22,
          f"moved={_moved22} pend={sorted(_pend22)} fail={sorted(_fail22)}")
    # T22f2：24h 窗边界——23h 龄在窗内获救（S-7 已验）、25h 龄超窗不救
    # （S-3 已验），此处只断言窗口常量本身为 24h，防口径回退。
    check("T22f2 清道夫窗口为 24h（选项 1 默认）",
          mod.FAILED_RESCUE_WINDOW_SEC == 24 * 3600,
          f"win={mod.FAILED_RESCUE_WINDOW_SEC}")
    # T26（第四轮故障注入 F3）：纯文本条瞬时传输失败也可救——此前
    # failed_entry_rescuable 只认 media_file，文本回复撞一次连接重置
    # （http 层重试耗尽后）即永久丢回复。语义判死/-14/字段不完整仍不救。
    _txt26 = {"to_user_id": USER_ID, "text": "在的", "context_token": CTX1,
              "message_id": "R4T1", "send_after": 0, "kind": "reply",
              "typing_sec": 0, "client_id": "ccb-txt26",
              "error": "ConnectionError: Connection reset by peer",
              "failed_at": _now22 - 600}
    check("T26a 文本条瞬时失败可救、语义判死/-14/字段缺失不可救",
          mod.failed_entry_rescuable(_txt26, _now22)
          and not mod.failed_entry_rescuable({**_txt26, "error": "被新消息打断"}, _now22)
          and not mod.failed_entry_rescuable({**_txt26, "errcode": -14}, _now22)
          and not mod.failed_entry_rescuable(
              {k: v for k, v in _txt26.items() if k != "context_token"}, _now22))
    wj(os.path.join(TMP, "outbox/failed", "R4-T.json"), _txt26)
    _moved26 = mod._failed_scavenge_pass(_now22)
    _r4t = rj(os.path.join(TMP, "outbox/pending", "R4-T.json"), {}) or {}
    check("T26b 清道夫重排文本条（rescues+1、剥错误字段、client_id 不变）",
          _moved26 >= 1 and _r4t.get("rescues") == 1 and "error" not in _r4t
          and _r4t.get("client_id") == "ccb-txt26",
          f"moved={_moved26} r4t={_r4t}")
    _p26 = os.path.join(TMP, "outbox/pending", "R4-T.json")
    if os.path.exists(_p26):
        os.remove(_p26)
    # T26c（第四轮 F6）：atomic_write_json 在 os.replace 失败时清 tmp 残渣
    _tgt26 = os.path.join(STATE, "r4_atomic.json")
    wj(_tgt26, {"v": 1})
    _orig_replace26 = os.replace
    def _enospc_replace(a, b):
        raise OSError(28, "No space left")
    os.replace = _enospc_replace
    _raised26 = False
    try:
        mod.atomic_write_json(_tgt26, {"v": 2})
    except OSError:
        _raised26 = True
    os.replace = _orig_replace26
    check("T26c replace 失败抛错、原文件完好、无 tmp 残渣",
          _raised26 and (rj(_tgt26, {}) or {}).get("v") == 1
          # 残渣口径收窄到本目标自己的 tmp：serve 并发运行时其他
          # 状态文件的原子写 tmp 会瞬时在册，全局断言在慢盘上误报
          # （CI Windows 实证）；F6 缺陷面是本写失败留自己的 tmp。
          and not any(x.startswith("r4_atomic.json.tmp.")
                      for x in os.listdir(STATE)),
          f"raised={_raised26}")
    if os.path.exists(_tgt26):
        os.remove(_tgt26)
    for d in ("pending", "failed"):
        for f in os.listdir(os.path.join(TMP, "outbox", d)):
            if f.startswith("S-"):
                os.remove(os.path.join(TMP, "outbox", d, f))
    mod.send_media_message = orig_send_media

    # ---------- T22g 回复媒体打断分类型（选项 2 默认）：视频豁免、图片仍作废 ----------
    mod._stop = threading.Event()
    _g0 = time.time() - 200
    with open(os.path.join(STATE, "history.jsonl"), "a", encoding="utf-8") as hf:
        # role 必带：S4 后打断判据只认 assistant 记录（生产 history 恒有 role）
        hf.write(json.dumps({"message_id": "VG", "ts": _g0,
                             "role": "assistant"}) + "\n")
        hf.write(json.dumps({"message_id": "IG", "ts": _g0,
                             "role": "assistant"}) + "\n")
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": _g0 + 100, "stale": False})
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    _sent_g = []

    def fake_send_media_g(session, to_user_id, file_path, kind,
                          context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        _sent_g.append(file_path)
        return {}

    mod.send_media_message = fake_send_media_g
    _base_g = {"to_user_id": USER_ID, "context_token": CTX1, "send_after": 0,
               "kind": "reply", "typing_sec": 0}
    wj(os.path.join(TMP, "outbox/pending", "VG-2.json"),
       {**_base_g, "text": "（视频）", "message_id": "VG",
        "media_file": "/tmp/fake-vg.mp4", "media_type": "video"})
    wj(os.path.join(TMP, "outbox/pending", "IG-2.json"),
       {**_base_g, "text": "（照片）", "message_id": "IG",
        "media_file": "/tmp/fake-ig.jpg", "media_type": "image"})
    mod._media_sender_pass(sess, USER_ID)
    _ig_fail = rj(os.path.join(TMP, "outbox/failed", "IG-2.json"), {}) or {}
    check("T22g-a 回复视频被新消息打断时豁免作废、继续发送",
          _sent_g == ["/tmp/fake-vg.mp4"]
          and os.path.exists(os.path.join(TMP, "outbox/sent", "VG-2.json")),
          f"sent={_sent_g}")
    check("T22g-b 回复图片被新消息打断时仍作废",
          "被新消息打断" in str(_ig_fail.get("error", ""))
          and "/tmp/fake-ig.jpg" not in _sent_g,
          f"err={_ig_fail.get('error')}")
    mod.send_media_message = orig_send_media
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})

    # ---------- T23 S4 媒体打断判据（started 只取 assistant 记录） ----------
    # 镜像二轮深审沙箱六例（A–F）+ 多条合并批次（G）+ 无 assistant 记录
    # 跳过检查（H）。旧判据把 user 记录混入 started：C/D/E/F/G/H 六例在
    # 旧代码下都会被假阳性作废，新判据应全部送达；B 真新消息仍作废。
    mod._stop = threading.Event()
    _sent23 = []

    def fake_send_media_23(session, to_user_id, file_path, kind,
                           context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        _sent23.append(file_path)
        return {}

    mod.send_media_message = fake_send_media_23
    wj(os.path.join(STATE, "counters.json"), {"unreplied": 0})
    _T = time.time() - 1000

    def _u(mid, ts):
        return {"message_id": mid, "ts": ts, "role": "user"}

    def _a(mid, ts):
        return {"message_id": mid, "ts": ts, "role": "assistant"}

    def t23_run(key, hist_rows, recv, fname):
        with open(os.path.join(STATE, "history.jsonl"), "a",
                  encoding="utf-8") as hf:
            for row in hist_rows:
                hf.write(json.dumps(row) + "\n")
        wj(os.path.join(STATE, "context.json"),
           {"context_token": CTX1, "received_at": recv, "stale": False})
        wj(os.path.join(TMP, "outbox/pending", fname),
           {"to_user_id": USER_ID, "context_token": CTX1, "send_after": 0,
            "kind": "reply", "typing_sec": 0, "text": "（照片）",
            "message_id": key, "media_file": "/tmp/fake-" + key + ".jpg",
            "media_type": "image"})
        _before = len(_sent23)
        mod._media_sender_pass(sess, USER_ID)
        _sent = _sent23[_before:] == ["/tmp/fake-" + key + ".jpg"]
        _fail = rj(os.path.join(TMP, "outbox/failed", fname), {}) or {}
        return _sent, "被新消息打断" in str(_fail.get("error", ""))

    s, v = t23_run("S4A", [_u("S4A", _T + 0.03), _a("S4A", _T + 120)],
                   _T, "S4A-2.json")
    check("T23a S4 正常路径（到达后首发、无新消息）送达",
          s and not v, f"sent={s} void={v}")
    s, v = t23_run("S4B", [_u("S4B", _T), _a("S4B", _T + 120)],
                   _T + 180, "S4B-2.json")
    check("T23b S4 首发后真新消息仍作废",
          v and not s, f"sent={s} void={v}")
    s, v = t23_run("S4C", [_u("S4C", _T), _a("S4C", _T + 120)],
                   _T + 120, "S4C-2.json")
    check("T23c S4 recv==首发时刻（等号边界）送达",
          s and not v, f"sent={s} void={v}")
    s, v = t23_run("S4D", [_u("S4D", _T - 50), _a("S4D", _T + 120)],
                   _T + 120, "S4D-2.json")
    check("T23d S4 user 记录更早不再拉低阈值",
          s and not v, f"sent={s} void={v}")
    s, v = t23_run("S4E", [_u("S4E", _T - 0.5), _a("S4E", _T + 60)],
                   _T, "S4E-2.json")
    check("T23e S4 亚秒写入顺序扰动不误作废",
          s and not v, f"sent={s} void={v}")
    s, v = t23_run("S4F", [_u("S4F", _T), _a("S4F", _T + 120)],
                   _T + 1, "S4F-2.json")
    check("T23f S4 1 秒时钟扰动不误作废",
          s and not v, f"sent={s} void={v}")
    s, v = t23_run("S4G", [_u("S4G", _T), _u("S4G2", _T + 5),
                           _a("S4G", _T + 30)],
                   _T + 5, "S4G-2.json")
    check("T23g S4 多条合并批次 + 回复图片不误作废",
          s and not v, f"sent={s} void={v}")
    s, v = t23_run("S4H", [_u("S4H", _T)], _T + 300, "S4H-2.json")
    check("T23h S4 无 assistant 记录跳过打断检查、送达",
          s and not v, f"sent={s} void={v}")
    mod.send_media_message = orig_send_media
    wj(os.path.join(STATE, "context.json"),
       {"context_token": CTX1, "received_at": time.time(), "stale": False})

    # ---------- T21 P3：抢锁失败独立退出码 ----------
    # （源套件 T21b/T21c/T24 测 watchdog.sh 的分类与通知聚合；通用版无
    # watchdog.sh（部署走 systemd service），三段已剔除，见 tests/README.md。）
    # T21a：先起一个 serve 持锁，再启第二个 serve——必须以专属退出码 75
    # 退出并在 serve.log 留痕，不覆盖 pid 文件（T14 起本套件无常驻 serve，
    # 故这里自起一个，测完即停）。
    # 先删旧 serve.pid：Windows 的 terminate() 直接杀进程、不走优雅退出，
    # 旧 serve 的 pid 文件不会被清理；不删则 wait_until 立刻命中旧文件、
    # pid_before 读到 stale 值，断言必败（CI Windows 格实证）。
    _stale_pid = os.path.join(STATE, "serve.pid")
    if os.path.exists(_stale_pid):
        os.remove(_stale_pid)
    serve2 = subprocess.Popen([sys.executable, BRIDGE, "serve"], env=ENV,
                              stdout=open(os.path.join(STATE, "serve.out.log"), "a"),
                              stderr=subprocess.STDOUT)
    try:
        wait_until(lambda: os.path.exists(os.path.join(STATE, "serve.pid")), 10)
        pid_before = open(os.path.join(STATE, "serve.pid")).read().strip()
        r = run_cli("serve")
        slog = ""
        try:
            slog = open(os.path.join(STATE, "serve.log"), encoding="utf-8").read()
        except OSError:
            pass
        check("T21a 抢锁失败以 75 退出且 serve.log 留痕、pid 未被覆盖",
              r.returncode == 75 and "以 75 退出" in slog
              and open(os.path.join(STATE, "serve.pid")).read().strip() == pid_before,
              f"rc={r.returncode} stderr={r.stderr[:120]}")
    finally:
        serve2.terminate()
        try:
            serve2.wait(timeout=15)
        except subprocess.TimeoutExpired:
            serve2.kill()
finally:
    if serve_proc:
        serve_proc.terminate()
        try:
            serve_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            serve_proc.kill()

print()
fails = [r for r in RESULTS if not r[1]]
print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(fails)}，失败 {len(fails)}")
for name, _, detail in fails:
    print("失败：", name, detail)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
