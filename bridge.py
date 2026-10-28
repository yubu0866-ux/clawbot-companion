#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge.py — 微信 iLink/ClawBot 通道收发桥（开源通用版）。

用途：扫码登录、长轮询收取私聊消息、context_token 维护、outbox/pending
发送队列、媒体上传与发送、typing 状态、看门狗心跳与单实例 serve。
配置依赖：同目录 config.json（时区、气泡/回复上限、llm、proactive 等），
人设见 persona.json；运行态全部落在 state/、inbox/、outbox/ 相对目录。
llm.mode="api"（默认）时由 worker.py 经 llm.py 调模型；llm.mode="agent"
时本桥只负责收发：入站消息落 inbox/ 文件，外部 agent 把回复写进
outbox/pending/，本桥按队列发送（文件契约见 README）。
凭据只存 state/session.json（600 权限）；一切输出对 token 脱敏。
环境变量：COMPANION_HOME 指定数据目录、COMPANION_API_BASE 覆盖登录域名、
COMPANION_PAUSE_14_SEC 覆盖 -14 暂停秒数（默认 300）。
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

# ---- 跨平台文件锁（portable lock shim）----
# POSIX 用 fcntl.flock；Windows 没有 fcntl，改用 msvcrt.locking 对锁文件
# 第 0 字节加范围锁。语义对齐：默认阻塞等锁，nb=True 时冲突立即抛
# OSError（与 flock LOCK_NB 一致）；阻塞锁用非阻塞轮询 + 让步实现真阻塞
# （msvcrt 的 LK_LOCK 约 10 秒即放弃，高并发下会悄悄退化成无锁，见下）。
try:
    import fcntl

    def _flock_ex(fd, nb=False):
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nb else 0))

    def _flock_un(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _flock_ex(fd, nb=False):
        fh = fd if isinstance(fd, int) else fd.fileno()
        if os.fstat(fh).st_size == 0:
            os.lseek(fh, 0, os.SEEK_SET)
            os.write(fh, b"\0")
        os.lseek(fh, 0, os.SEEK_SET)
        if nb:
            msvcrt.locking(fh, msvcrt.LK_NBLCK, 1)
            return
        # 阻塞锁必须真阻塞：msvcrt 的 LK_LOCK 约 10 秒即放弃并抛 OSError，
        # 而 _StateLock 会把 OSError 吞掉继续跑——高并发下悄悄退化成无锁
        # （16 进程 hammer 式抢锁即此风险）。改用非阻塞轮询 + 让步，上限
        # 120 秒，到时仍抛 OSError 走原有兜底路径。
        deadline = time.monotonic() + 120
        while True:
            try:
                msvcrt.locking(fh, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _flock_un(fd):
        fh = fd if isinstance(fd, int) else fd.fileno()
        os.lseek(fh, 0, os.SEEK_SET)
        msvcrt.locking(fh, msvcrt.LK_UNLCK, 1)

HOME = os.environ.get("COMPANION_HOME") or os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HOME, "state")
INBOX = os.path.join(HOME, "inbox")
PENDING = os.path.join(HOME, "outbox", "pending")
SENT = os.path.join(HOME, "outbox", "sent")
FAILED = os.path.join(HOME, "outbox", "failed")
CONFIG_FILE = os.path.join(HOME, "config.json")
SESSION_FILE = os.path.join(STATE, "session.json")
CURSOR_FILE = os.path.join(STATE, "cursor.json")
CONTEXT_FILE = os.path.join(STATE, "context.json")
COUNTERS_FILE = os.path.join(STATE, "counters.json")
PROACTIVE_FILE = os.path.join(STATE, "proactive.json")
DELAYED_DUE_FILE = os.path.join(STATE, "delayed_due.json")
DIRECTOR = os.path.join(STATE, "director")
DIRECTOR_DONE = os.path.join(STATE, "director", "done")
TUNE_LOG_FILE = os.path.join(STATE, "tune.log")
PROCESSED_FILE = os.path.join(STATE, "processed.json")
HISTORY_FILE = os.path.join(STATE, "history.jsonl")
PROBE_INBOUND_FILE = os.path.join(STATE, "probe_inbound.jsonl")
MEDIA_IN = os.path.join(STATE, "media_in")
HEALTH_FILE = os.path.join(STATE, "context_health.jsonl")
USAGE_FILE = os.path.join(STATE, "usage.json")
PID_FILE = os.path.join(STATE, "serve.pid")
HEARTBEAT_FILE = os.path.join(STATE, "heartbeat")
TRIGGER_FILE = os.path.join(STATE, "trigger")
PAUSED_FILE = os.path.join(STATE, "PAUSED")
NEEDS_RELOGIN_FILE = os.path.join(STATE, "NEEDS_RELOGIN")
NOTIFIED_FILE = os.path.join(STATE, "notified_relogin")
QR_PNG = os.path.join(STATE, "qr.png")
QR_PENDING_FILE = os.path.join(STATE, "qr_pending.json")
SERVE_LOG = os.path.join(STATE, "serve.log")
LOCK_FILE = os.path.join(STATE, ".lock")
INSTANCE_LOCK_FILE = os.path.join(STATE, "serve.lock")
# serve 抢单实例锁失败时的专属退出码（EX_TEMPFAIL）：watchdog 按「已有
# 实例在跑」分类、systemd unit 以 RestartPreventExitStatus=75 不重启循环。
EXIT_ALREADY_RUNNING = 75
TYPING_FILE = os.path.join(STATE, "typing.json")
INBOUND_RETRY_FILE = os.path.join(STATE, "inbound_retry.json")

API_BASE = os.environ.get("COMPANION_API_BASE", "https://ilinkai.weixin.qq.com").rstrip("/")
APP_ID = "bot"
CLIENT_VERSION = "132102"          # = 0x20406，即 2.4.6
CHANNEL_VERSION = "2.4.6"          # 与 ClientVersion 保持一致
BOT_AGENT = "ClawBotCompanion/1.0"
PAUSE_14_SEC = int(os.environ.get("COMPANION_PAUSE_14_SEC", "300"))

DEFAULT_CONFIG = {
    "tz": "Asia/Shanghai", "nickname_for_me": "小朋友",
    "agent_poll_interval_sec": 5, "debounce_sec": 3,
    "max_bubbles": 3, "bubble_gap_sec": [1.5, 3],
    "reply_max_chars": 150, "long_msg_chars": 2000,
    "history_turns": 20, "history_keep_days": 30, "summary_days": 3,
    "unreplied_warn_at": 7, "unreplied_stop_at": 9,
    "reply": {"candidates": 2, "mirror_ratio": 1.5, "skip_ack_prob": 0.4,
              "fast_window_min": 5, "night_hours": [23, 7],
              "delay_sec": {"fast": [2, 6], "comfort": [2, 8], "day": [3, 12], "night": [8, 25]},
              "busy_factor": {"low": 0.7, "mid": 1.0, "high": 1.5}, "delay_cap_sec": 40,
              "typing_sec_per_char": 0.2, "typing_cap_sec": 4},
    "llm": {"mode": "api", "base_url": "https://api.openai.com/v1", "model": "",
            "api_key_env": "COMPANION_LLM_API_KEY", "temperature": 0.8,
            "timeout_sec": 60, "max_calls_per_day": 300, "max_prompt_chars": 12000,
            "fallback": None,
            "routes": {"fast": {"timeout_sec": 20}, "deep": {"timeout_sec": 60}},
            "route_by_intent": {"fast": ["chat", "ack", "bye", "playful", "ask"],
                                "deep": ["vent", "help", "crisis"]}},
    "content_pack_dir": "content",
    "proactive": {"enabled": True, "daily_max": 3, "min_gap_hours": 3,
                  "quiet_hours": [0, 7], "active_skip_min": 30, "silence_hours": 5,
                  "max_context_age_hours": 24, "send_jitter_min": [0, 10],
                  "slots": {"morning": {"window": ["07:30", "09:30"], "p": 0.6},
                            "lunch": {"window": ["11:45", "12:40"], "p": 0.3},
                            "evening": {"window": ["18:00", "20:00"], "p": 0.3},
                            "goodnight": {"window": ["22:30", "23:40"], "p": 0.5}}},
}


class ConfigError(Exception):
    pass


class TokenDead(Exception):
    pass


class CdnServerError(RuntimeError):
    """CDN 上传返回 HTTP 5xx：服务端瞬时故障（与本地永久错误、业务错误
    区分）。outbox 层据此按 5xx 专用长退避重试（MEDIA_RETRY_BACKOFFS_5XX），
    不再首败即判死（2026-10-24 深审 A1：当晚 CDN 500 窗口内媒体全灭，
    根因即 5xx 的 RuntimeError 不在瞬时错误集合内、退避阶梯不可达）。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class HttpServerError(RuntimeError):
    """平台 API（http_json）返回 HTTP 5xx：服务端瞬时故障。与 CdnServerError
    同类处置（瞬时分类 + 5xx 长退避 + 清道夫可救）——bughunt2-2026-10-25
    实证：getuploadurl 等 API 层 5xx 经 http_json 抛普通 RuntimeError，
    _is_transient_exc 不认、退避不可达、落盘文案也不在清道夫白名单内，
    与 A1 修好的 CDN 层 5xx 形成分类断层，媒体会首败判死且不可救。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user_cfg = json.load(f)
        except ValueError as e:
            raise ConfigError(f"config.json 不是合法 JSON：{e}")
        if not isinstance(user_cfg, dict):
            raise ConfigError("config.json 顶层必须是对象")
        cfg = _deep_merge(cfg, user_cfg)
    # 校验关键字段
    try:
        ZoneInfo(cfg["tz"])
    except Exception:
        raise ConfigError(f"config.tz 非法时区：{cfg.get('tz')!r}")
    for key in ("agent_poll_interval_sec", "debounce_sec", "max_bubbles", "reply_max_chars",
                "unreplied_warn_at", "unreplied_stop_at"):
        v = cfg.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"config.{key} 必须是正数，当前为 {v!r}")
    if cfg["unreplied_warn_at"] >= cfg["unreplied_stop_at"]:
        raise ConfigError("config.unreplied_warn_at 必须小于 unreplied_stop_at")
    gap = cfg.get("bubble_gap_sec")
    if not (isinstance(gap, list) and len(gap) == 2 and all(isinstance(x, (int, float)) for x in gap) and 0 <= gap[0] <= gap[1]):
        raise ConfigError(f"config.bubble_gap_sec 必须是 [小, 大] 两个数，当前为 {gap!r}")
    if cfg["llm"].get("mode") not in ("api", "agent", "http"):
        raise ConfigError(f"config.llm.mode 只能是 api 或 agent（http 为 api 的旧别名），当前为 {cfg['llm'].get('mode')!r}")
    return cfg


CFG = None
TZ = None


def cfg():
    global CFG, TZ
    if CFG is None:
        CFG = load_config()
        TZ = ZoneInfo(CFG["tz"])
    return CFG


def tz():
    cfg()
    return TZ


def ensure_dirs():
    for d in (STATE, INBOX, PENDING, SENT, FAILED, DIRECTOR, DIRECTOR_DONE, MEDIA_IN):
        os.makedirs(d, exist_ok=True)
    try:
        os.chmod(STATE, 0o700)
    except OSError:
        pass


# ---------------- 基础工具：锁、原子写、脱敏、日志 ----------------

_lock_local = threading.Lock()

# serve 单实例 flock 的 fd（cmd_serve 抢锁成功后赋值，全程持有至进程结束）
_INSTANCE_LOCK_FD = None


class _StateLock:
    """进程内线程锁 + 跨进程 flock（state/.lock）。"""
    def __enter__(self):
        _lock_local.acquire()
        self._fd = None
        try:
            self._fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
            _flock_ex(self._fd)
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                _flock_un(self._fd)
                os.close(self._fd)
            except OSError:
                pass
        _lock_local.release()
        return False


def state_lock():
    return _StateLock()


def _fsync_dir(dirpath):
    """目录 fsync（仅 POSIX 有此概念，Windows 无 O_DIRECTORY 时跳过）。"""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        dfd = os.open(dirpath or ".", os.O_DIRECTORY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass


def atomic_write_json(path, data, mode=0o644):
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.replace(tmp, path)
    except OSError:
        # replace 失败（ENOSPC/跨设备等）时同样清掉 tmp，否则残渣在
        # state/ 里永久滞留（无任何 prune 覆盖该目录的 *.tmp.* 形态；
        # 第四轮故障注入 F6 实证）。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    _fsync_dir(os.path.dirname(path))


def write_json_locked(path, data, mode=0o644):
    with state_lock():
        atomic_write_json(path, data, mode)


def _quarantine_corrupt(path):
    """损坏 state 文件留痕：改名为 <path>.corrupt-<unix秒> 保全原始字节
    证据，返回留痕路径或 None。read_json 吞错回 default 后，下游常以空
    数据写回同一路径——不先留痕，损坏原件会被静默覆盖清零、无从恢复
    也无从定因。改名同时让原路径消失，后续读走 OSError 回 default，
    不会反复触发。最佳努力：改名失败静默（留痕不许反噬主流程）。
    同秒唯一命名：同文件同秒内再次损坏时基础名后加 -<pid>、再撞加
    -2/-3…，不覆盖前一份证据。"""
    try:
        base = f"{path}.corrupt-{int(time.time())}"
        dst = base
        if os.path.exists(dst):
            dst = f"{base}-{os.getpid()}"
            n = 2
            while os.path.exists(dst):
                dst = f"{base}-{os.getpid()}-{n}"
                n += 1
        os.replace(path, dst)
        return dst
    except OSError:
        return None


def _is_outbox_path(path):
    """outbox 三目录豁免留痕：pending/sent/failed 文件已有自己的死信
    契约（发送趟 _move_with_error 移入 failed/）；留痕改名会抢在死信
    之前把文件移走，破坏「损坏条判死」契约。state 文件仍留痕。"""
    for d in (PENDING, SENT, FAILED):
        if d and (path == d or path.startswith(d.rstrip(os.sep) + os.sep)):
            return True
    return False


def read_json(path, default=None):
    # 只认 dict 顶层：本桥全部单文件 JSON 存储按构造都是对象；顶层是
    # 裸标量/数组属损坏形态，透传会让下游 .get 消费者崩溃（实证：
    # counters.json=5 时消费者 AttributeError），与解析失败同口径回
    # default。解析失败/非对象顶层＝内容损坏：先留痕再回 default；
    # 文件不存在等 OSError 是常态（首次运行），不留痕。例外：outbox
    # 三目录文件不留痕（见 _is_outbox_path）。
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return default
    except ValueError:
        if not _is_outbox_path(path):
            kept = _quarantine_corrupt(path)
            if kept:
                serve_log(f"state 文件损坏已留痕：{os.path.basename(path)} → "
                          f"{os.path.basename(kept)}（回默认继续）")
        return default
    if not isinstance(data, dict):
        if not _is_outbox_path(path):
            kept = _quarantine_corrupt(path)
            if kept:
                serve_log(f"state 文件顶层非对象已留痕：{os.path.basename(path)}"
                          f" → {os.path.basename(kept)}（回默认继续）")
        return default
    return data


def append_jsonl(path, obj):
    with state_lock():
        # 断电/崩溃可能留下无换行的半行：直接续写会与半行拼成一行、
        # 两条记录一起被读层当坏行静默丢弃，追加前先查末字节、缺
        # 换行先补一个。
        prefix = ""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        prefix = "\n"
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(prefix + json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path):
    # 只收 dict 行：JSONL 各存储（history/records/summaries/…）按构造都是
    # 一行一对象；一行「合法 JSON 但非对象」（如裸标量 5）是撕裂/外来写
    # 入的损坏形态，透传会让下游 .get 消费者崩溃（bughunt2-2026-10-25
    # 实证 quiet_today AttributeError），在读层与坏行同口径跳过。
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
    except OSError:
        pass
    return out


def read_jsonl_tail(path, max_records, chunk_bytes=65536):
    """read_jsonl 的尾窗版：只读文件尾部，返回最后 max_records 条可解析
    记录（顺序与全量版一致），读取量与文件总体积脱钩（prep 打包简报用，
    避免每轮全量解析不断增长的 history/records）。
    从文件尾按块向前拼，直到凑够 max_records 条已解析记录或读到文件头；
    未到文件头时缓冲区首行可能是被块边界截断的半行，丢弃不解析；
    空行/坏行跳过口径与 read_jsonl 相同。"""
    if max_records <= 0:
        return []
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return []
        buf = b""
        pos = size
        records = []
        while True:
            step = min(chunk_bytes, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            if pos > 0:
                lines = lines[1:]  # 首行可能是半行残段，丢弃
            records = []
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict):  # 非对象行同 read_jsonl 口径跳过
                    records.append(rec)
            if len(records) >= max_records or pos == 0:
                return records[-max_records:]


def mask_secret(v):
    """token / context_token：只显示前 4 + 后 4（规格 0.5）。"""
    if not v:
        return "(无)"
    s = str(v)
    if len(s) <= 8:
        return "****"
    return s[:4] + "***" + s[-4:]


def mask_user(v):
    if not v:
        return "(无)"
    s = str(v)
    if len(s) <= 12:
        return s[:2] + "***"
    return s[:8] + "***" + s[-4:]


def _clean_text(s):
    """平台文本可能混入孤立代理对字符（非法 UTF-16 残片，如 \\ud800）：
    直接以 UTF-8 写文件/发请求会抛 UnicodeEncodeError。统一换成 U+FFFD
    保内容可达（bughunt-2026-10-24 面 4：毒消息曾让收件写盘抛错、
    整批游标不前进、serve 陷入崩溃循环）。"""
    if not isinstance(s, str):
        return s
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in s)


def _coerce_ms(v):
    """平台时间戳字段类型不可靠（可能是字符串）：能转 int 就转，
    转不动返回 None，调用方以到达时间兜底，避免下游 float(None) 崩溃
    或字符串时间戳把 context 年龄算成恒新鲜。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def iso(ts=None):
    ts = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _rotate(path, max_bytes=5 * 1024 * 1024, keep=3):
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            src = f"{path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def serve_log(msg):
    line = _clean_text(f"[{iso()}] {msg}")
    try:
        _rotate(SERVE_LOG)
        with open(SERVE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def ret_code(resp):
    if not isinstance(resp, dict):
        return None
    if resp.get("ret") is not None:
        return resp.get("ret")
    return resp.get("errcode")


# ---------------- HTTP 层：连接层重试 3 次，退避 1s/2s/4s ----------------

def _is_timeout_exc(e):
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return True
    return "timed out" in str(e).lower()


# requests/urllib3 等第三方库的连接类异常类名（不硬依赖其导入，仅按名识别）。
_TRANSIENT_EXC_NAMES = {"ConnectionError", "ChunkedEncodingError", "SSLError",
                        "NewConnectionError", "MaxRetryError"}


def _is_transient_exc(e):
    """超时之外的瞬时传输失败（连接被重置/断开、代理隧道抖动等）：值得 outbox
    层退避重试。本地永久错误（媒体文件缺失/无权限）不算，业务错误不算。"""
    if _is_timeout_exc(e):
        return True
    if isinstance(e, (CdnServerError, HttpServerError)):
        return True
    if isinstance(e, (FileNotFoundError, PermissionError)):
        return False
    if isinstance(e, (ConnectionError, http.client.HTTPException,
                      urllib.error.URLError)):
        return True
    return type(e).__name__ in _TRANSIENT_EXC_NAMES


def http_request(method, url, headers=None, body_bytes=None, timeout=35, retry_timeouts=True):
    """返回 (status, raw)。连接层异常（断开/重置/URLError/超时/HTTP 5xx）最多重试 3 次，
    退避 1s/2s/4s；HTTP 4xx 直接返回。retry_timeouts=False 时超时立即抛出（长轮询用）。"""
    headers = headers or {}
    last_exc = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            if 500 <= e.code < 600 and attempt < 3:
                time.sleep([1, 2, 4][attempt])
                continue
            return e.code, raw
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            last_exc = e
            if _is_timeout_exc(e) and not retry_timeouts:
                raise TimeoutError(str(e)) from e
            if attempt < 3:
                time.sleep([1, 2, 4][attempt])
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("http_request 未知失败")


def http_json(method, url, headers=None, payload=None, timeout=35, retry_timeouts=True):
    body_bytes = None
    hdrs = dict(headers or {})
    if payload is not None:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    status, raw = http_request(method, url, headers=hdrs, body_bytes=body_bytes,
                               timeout=timeout, retry_timeouts=retry_timeouts)
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if status >= 500:
        # 服务端故障优先于报文解析：哪怕 5xx 的 body 不是 JSON，也按瞬时
        # 故障分类（HttpServerError），不落进「响应不是 JSON」的永久错误
        raise HttpServerError(f"HTTP {status}: {text[:200]}", status)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        raise RuntimeError(f"响应不是 JSON (HTTP {status}): {text[:200]}")
    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {text[:200]}")
    return data


def gen_uin():
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def login_headers():
    return {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": gen_uin(), "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": CLIENT_VERSION}


def poll_headers():
    return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": CLIENT_VERSION}


def bot_headers(token):
    return {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
            "Authorization": "Bearer " + token, "X-WECHAT-UIN": gen_uin(),
            "iLink-App-Id": APP_ID, "iLink-App-ClientVersion": CLIENT_VERSION}


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


# ---------------- session 与共享状态 ----------------

def load_session():
    return read_json(SESSION_FILE, None)


def save_session(sess):
    write_json_locked(SESSION_FILE, sess, mode=0o600)


def session_base(session):
    return ((session or {}).get("baseurl") or API_BASE).rstrip("/")


def mark_needs_relogin(reason):
    ensure_dirs()
    with state_lock():
        with open(NEEDS_RELOGIN_FILE, "w", encoding="utf-8") as f:
            f.write(f"{iso()} {reason}\n")


def clear_relogin():
    for p in (NEEDS_RELOGIN_FILE, NOTIFIED_FILE):
        try:
            os.remove(p)
        except OSError:
            pass


def read_context():
    return read_json(CONTEXT_FILE, {}) or {}


def write_context(token):
    write_json_locked(CONTEXT_FILE,
                      {"context_token": token, "received_at": time.time(), "stale": False})


def mark_context_stale():
    with state_lock():
        ctx = read_context()
        ctx["stale"] = True
        atomic_write_json(CONTEXT_FILE, ctx)


def get_unreplied():
    return int((read_json(COUNTERS_FILE, {}) or {}).get("unreplied", 0))


def set_unreplied(n):
    with state_lock():
        data = read_json(COUNTERS_FILE, {}) or {}
        data["unreplied"] = int(n)
        data["updated_at"] = time.time()
        atomic_write_json(COUNTERS_FILE, data)


def bump_unreplied(delta):
    with state_lock():
        data = read_json(COUNTERS_FILE, {}) or {}
        data["unreplied"] = max(0, int(data.get("unreplied", 0)) + delta)
        data["updated_at"] = time.time()
        atomic_write_json(COUNTERS_FILE, data)
        return data["unreplied"]


def proactive_max_age_hours():
    pj = read_json(PROACTIVE_FILE, {}) or {}
    if pj.get("max_context_age_hours_override"):
        return float(pj["max_context_age_hours_override"])
    return float(cfg()["proactive"]["max_context_age_hours"])


def lower_proactive_max_age(age_hours):
    with state_lock():
        pj = read_json(PROACTIVE_FILE, {}) or {}
        cur = float(pj.get("max_context_age_hours_override")
                    or cfg()["proactive"]["max_context_age_hours"])
        new = min(cur, max(0.0, age_hours) * 0.8)
        pj["max_context_age_hours_override"] = new
        atomic_write_json(PROACTIVE_FILE, pj)
        return new


def record_latency(seconds, fast=False):
    day = time.strftime("%Y-%m-%d", time.localtime())
    with state_lock():
        usage = read_json(USAGE_FILE, {}) or {}
        d = usage.setdefault(day, {"reply_latencies_sec": []})
        d.setdefault("reply_latencies_sec", []).append(round(float(seconds), 2))
        if fast:
            d.setdefault("reply_latencies_fast_sec", []).append(round(float(seconds), 2))
        atomic_write_json(USAGE_FILE, usage)


# ---------------- Bot API ----------------

def _send_payload_once(session, to_user_id, item_list, context_token, client_id):
    payload = {"msg": {"from_user_id": "", "to_user_id": to_user_id, "client_id": client_id,
                       "message_type": 2, "message_state": 2, "context_token": context_token,
                       "item_list": item_list},
               "base_info": base_info()}
    return http_json("POST", session_base(session) + "/ilink/bot/sendmessage",
                     headers=bot_headers(session["bot_token"]), payload=payload, timeout=30)


def _send_once(session, to_user_id, text, context_token, client_id):
    return _send_payload_once(session, to_user_id,
                              [{"type": 1, "text_item": {"text": text}}],
                              context_token, client_id)


def _send_items(session, to_user_id, item_list, context_token, client_id=None, log=serve_log):
    """发送任意 item_list。同一条消息重试时 client_id 不变（幂等）。
    -14：暂停 PAUSE_14_SEC 后重试一次，仍失败抛 TokenDead 并写 NEEDS_RELOGIN。
    其他非 0 返回抛 RuntimeError（带 errcode 属性）。"""
    client_id = client_id or ("wxb-" + uuid.uuid4().hex)
    resp = _send_payload_once(session, to_user_id, item_list, context_token, client_id)
    code = ret_code(resp)
    if code in (0, None):
        return resp
    if code == -14:
        log(f"sendmessage 收到 -14（可能是限流），暂停 {PAUSE_14_SEC}s 后用同一 client_id 重试一次")
        time.sleep(PAUSE_14_SEC)
        resp2 = _send_payload_once(session, to_user_id, item_list, context_token, client_id)
        code2 = ret_code(resp2)
        if code2 in (0, None):
            return resp2
        if code2 == -14:
            mark_needs_relogin("sendmessage 连续返回 -14，重试后仍失败")
            raise TokenDead("sendmessage 连续 -14，已写 NEEDS_RELOGIN")
        err = RuntimeError(f"sendmessage 重试后失败 ret/errcode={code2} errmsg={resp2.get('errmsg')}")
        err.errcode = code2
        raise err
    err = RuntimeError(f"sendmessage 失败 ret/errcode={code} errmsg={resp.get('errmsg')}")
    err.errcode = code
    raise err


def send_message(session, to_user_id, text, context_token, client_id=None, log=serve_log):
    """发送一条文本。重试与 -14 语义见 _send_items。"""
    return _send_items(session, to_user_id,
                       [{"type": 1, "text_item": {"text": text}}],
                       context_token, client_id=client_id, log=log)


# ---------------- 媒体发送：CDN 上传（AES-128-ECB）+ sendmessage ----------------
# 协议与官方参考实现（openclaw-weixin）一致：getuploadurl 拿上传许可，
# 密文 POST 到 CDN，从响应头 x-encrypted-param 取下载参数，再随消息发出。

CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
_UPLOAD_MEDIA_TYPE = {"image": 1, "video": 2, "file": 3}


def _aes_ecb_padded_size(n):
    """AES-128-ECB + PKCS7 填充后的密文长度。"""
    return ((n + 1 + 15) // 16) * 16


_LIBCRYPTO = None


def _libcrypto():
    """懒加载系统 libcrypto（ctypes）：进程内 AES，避免把密钥放进子进程 argv。

    背景（2026-10-24 S3 硬化）：原实现用 `openssl enc -K <hex>` 子进程做
    AES-128-ECB，密钥明文出现在进程 argv 里，同机任何能列进程的用户都能在
    加密/解密窗口期读到（/proc/<pid>/cmdline）。改走 ctypes 直调 libcrypto
    的 EVP 接口后，密钥只存在于本进程内存。环境无新依赖：openssl CLI 本身
    就链接 libcrypto，find_library 必然命中；加载失败宁可抛错也不回退到
    argv 传密钥的旧路径（回退即重新引入暴露面）。Windows 的 libcrypto
    候选见下方 candidates（CPython 自带 DLLs\\libcrypto-3.dll 等）。"""
    global _LIBCRYPTO
    if _LIBCRYPTO is not None:
        return _LIBCRYPTO
    import ctypes
    import ctypes.util
    candidates = [ctypes.util.find_library("crypto")]
    if os.name == "nt":
        # Windows：CPython 安装目录自带 libcrypto-3.dll（DLLs 子目录，
        # _ssl 模块同款）；conda 在 Library\bin；最后退到 PATH 裸名。
        exe_dir = os.path.dirname(sys.executable)
        for base in (exe_dir, os.path.join(exe_dir, "DLLs"),
                     os.path.join(sys.prefix, "Library", "bin")):
            for dll in ("libcrypto-3.dll", "libcrypto-1_1.dll", "libeay32.dll"):
                candidates.append(os.path.join(base, dll))
        candidates += ["libcrypto-3.dll", "libcrypto-1_1.dll", "libeay32.dll"]
    else:
        candidates.append("libcrypto.so.3")
    lib = None
    for cand in candidates:
        if not cand:
            continue
        try:
            lib = ctypes.CDLL(cand)
            break
        except OSError:
            continue
    if lib is None:
        raise RuntimeError("无法加载 libcrypto（进程内 AES 不可用）")
    vp = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_new.restype = vp
    lib.EVP_CIPHER_CTX_free.argtypes = [vp]
    lib.EVP_aes_128_ecb.restype = vp
    for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
        getattr(lib, fn).argtypes = [vp, vp, vp, ctypes.c_char_p, ctypes.c_char_p]
        getattr(lib, fn).restype = ctypes.c_int
    for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
        getattr(lib, fn).argtypes = [vp, vp,
                                     ctypes.POINTER(ctypes.c_int),
                                     ctypes.c_char_p, ctypes.c_int]
        getattr(lib, fn).restype = ctypes.c_int
    for fn in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
        getattr(lib, fn).argtypes = [vp, vp, ctypes.POINTER(ctypes.c_int)]
        getattr(lib, fn).restype = ctypes.c_int
    _LIBCRYPTO = lib
    return lib


def _aes_ecb(data, key, encrypt):
    """AES-128-ECB + PKCS7（EVP 默认填充），与旧 openssl enc 路径逐字节等价。

    密钥只经 ctypes 参数进本进程内存，不落 argv/文件/env。解密时填充校验
    失败（密钥不对/密文损坏）由 Final 步返回 0 → 抛 RuntimeError，与旧路径
    「openssl 非零退出 → 抛错」语义一致，调用方的降级判断不变。"""
    import ctypes
    if len(key) != 16:
        raise RuntimeError(f"AES-128 密钥长度不对: {len(key)}")
    lib = _libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new 失败")
    try:
        init = lib.EVP_EncryptInit_ex if encrypt else lib.EVP_DecryptInit_ex
        update = lib.EVP_EncryptUpdate if encrypt else lib.EVP_DecryptUpdate
        final = lib.EVP_EncryptFinal_ex if encrypt else lib.EVP_DecryptFinal_ex
        if init(ctx, lib.EVP_aes_128_ecb(), None, bytes(key), None) != 1:
            raise RuntimeError("AES 初始化失败")
        # ECB 无 IV。输出上限：加密 = 明文 + 一个填充块；解密不超过密文长。
        out = ctypes.create_string_buffer(len(data) + 32)
        out_len = ctypes.c_int(0)
        fin_len = ctypes.c_int(0)
        if update(ctx, ctypes.addressof(out), ctypes.byref(out_len),
                  bytes(data), len(data)) != 1:
            raise RuntimeError("AES 处理失败")
        if final(ctx, ctypes.addressof(out) + out_len.value,
                 ctypes.byref(fin_len)) != 1:
            kind = "加密" if encrypt else "解密"
            raise RuntimeError(f"AES {kind}失败（填充校验不通过）")
        return out.raw[:out_len.value + fin_len.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _aes_ecb_encrypt(data, key):
    """AES-128-ECB 加密（PKCS7 填充），进程内完成，见 _libcrypto 说明。"""
    return _aes_ecb(data, key, True)


def _aes_ecb_decrypt(data, key):
    """AES-128-ECB 解密（PKCS7 去填充，与 _aes_ecb_encrypt 对称，入站媒体
    下载用）。填充不对（密钥不对/密文损坏）时抛 RuntimeError。"""
    return _aes_ecb(data, key, False)


def _cdn_post(url, body, timeout=180):
    """POST 原始字节到微信 CDN，返回 (status, headers 小写键 dict)。
    连接层异常与 HTTP 5xx 重试 3 次（退避 1s/2s/4s），与 http_request 一致。
    HTTP 错误时另把响应体前 4KB 的单行摘要放入 x-response-body：历史上
    CDN 500 的 x-error-message 常为空，若再把响应体直接丢弃，日志只剩
    裸 500、无从区分 CDN 源站/边缘与代理回包。只取响应体、不取 URL，
    不落任何签名参数。"""
    last_exc = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read(4096) or b""
            except Exception:
                pass
            hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            if raw:
                hdrs["x-response-body"] = " ".join(
                    raw.decode("utf-8", errors="replace").split())[:200]
            if 500 <= e.code < 600 and attempt < 3:
                time.sleep([1, 2, 4][attempt])
                continue
            return e.code, hdrs
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            last_exc = e
            if attempt < 3:
                time.sleep([1, 2, 4][attempt])
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("CDN 上传未知失败")


def _cdn_get(url, timeout=90):
    """GET 原始字节（入站媒体从微信 CDN 取件用），返回 (status, raw)。
    收件是同步路径：只尝试一次、失败直接抛给调用方降级，避免一条坏媒体
    拖住整批消息；HTTP 非 2xx 由 urlopen 抛 HTTPError，调用方按状态码归类。"""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _media_io_timeout(nbytes):
    """媒体 CDN 传输的单次套接字超时（秒）：按密文大小放宽。
    依据：媒体走出口代理，实测 122KB 图片整段上传约 3s，但 8.1MB 视频曾在
    固定 180s 下读超时（坏窗口里有效吞吐远低于 50KB/s）。按 120s 基础 +
    40s/MB 增量估算（8.1MB → 约 445s，可容忍约 20KB/s 级的吞吐/停顿），
    封顶 900s，避免发送线程被一条媒体无限期占住。"""
    return max(180, min(900, 120 + int(40 * (nbytes / 1_000_000))))


def _compress_image_bytes(data):
    """图片上传前压缩：微信里看图不需要原始大图，PNG 动辄 1-2MB 经代理
    上传要一分多钟。长边压到 1280、转 JPEG q82，一般能小 4-6 倍。
    失败或压完没变小都回原图，绝不因压缩丢发送。"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        out = buf.getvalue()
        return out if len(out) < len(data) else data
    except Exception:
        return data


MEDIA_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 出站媒体大小闸（审计 A6/C1 程序侧兜底）：超限按产物问题判死、不进 CDN


def upload_media(session, to_user_id, file_path, kind, log=serve_log):
    """把本地媒体上传到微信 CDN，返回 (cdn_media dict, 密文字节数)。kind: image/video/file。"""
    fsize = os.path.getsize(file_path)
    if fsize <= 0 or fsize > MEDIA_UPLOAD_MAX_BYTES:
        raise RuntimeError(
            f"产物校验失败：媒体文件大小异常 {fsize} 字节 file={os.path.basename(file_path)}")
    with open(file_path, "rb") as f:
        data = f.read()
    if kind == "image" and len(data) > 600_000:
        smaller = _compress_image_bytes(data)
        if len(smaller) < len(data):
            log(f"图片上传前压缩 {len(data)} → {len(smaller)} 字节 "
                f"file={os.path.basename(file_path)}")
            data = smaller
    rawsize = len(data)
    filesize = _aes_ecb_padded_size(rawsize)
    filekey = os.urandom(16).hex()
    aeskey = os.urandom(16)
    payload = {"filekey": filekey, "media_type": _UPLOAD_MEDIA_TYPE[kind],
               "to_user_id": to_user_id, "rawsize": rawsize,
               "rawfilemd5": hashlib.md5(data).hexdigest(), "filesize": filesize,
               "no_need_thumb": True, "aeskey": aeskey.hex(), "base_info": base_info()}
    resp = http_json("POST", session_base(session) + "/ilink/bot/getuploadurl",
                     headers=bot_headers(session["bot_token"]), payload=payload, timeout=60)
    code = ret_code(resp)
    if code not in (0, None):
        raise RuntimeError(f"getuploadurl 失败 ret/errcode={code} errmsg={resp.get('errmsg')}")
    full_url = (resp.get("upload_full_url") or "").strip()
    if full_url:
        cdn_url = full_url
    elif resp.get("upload_param"):
        cdn_url = (CDN_BASE_URL + "/upload?encrypted_query_param="
                   + urllib.parse.quote(resp["upload_param"], safe="")
                   + "&filekey=" + urllib.parse.quote(filekey, safe=""))
    else:
        raise RuntimeError("getuploadurl 没返回 upload_full_url / upload_param")
    ciphertext = _aes_ecb_encrypt(data, aeskey)
    status, hdrs = _cdn_post(cdn_url, ciphertext,
                             timeout=_media_io_timeout(len(ciphertext)))
    if status != 200:
        detail = (hdrs.get("x-error-message")
                  or hdrs.get("x-response-body") or "")[:200]
        extras = [f"{k}={str(hdrs[k])[:80]}"
                  for k in ("server", "via", "x-cache")
                  if hdrs.get(k)]
        msg = f"CDN 上传失败 HTTP {status}: {detail}"
        if extras:
            msg += " (" + ", ".join(extras) + ")"
        if 500 <= status < 600:
            raise CdnServerError(msg, status)
        raise RuntimeError(msg)
    download_param = hdrs.get("x-encrypted-param")
    if not download_param:
        raise RuntimeError("CDN 上传响应缺 x-encrypted-param")
    media = {"encrypt_query_param": download_param,
             "aes_key": base64.b64encode(aeskey.hex().encode()).decode(),
             "encrypt_type": 1}
    log(f"媒体已上传 kind={kind} raw={rawsize} cipher={len(ciphertext)} "
        f"file={os.path.basename(file_path)}")
    return media, len(ciphertext)


_UPLOAD_CACHE_TTL_SEC = 600  # A5 上传凭证复用 TTL：覆盖现行退避阶梯前两档（60/300）


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _upload_with_cache(session, to_user_id, file_path, kind, cache_data,
                       cache_path, log):
    """A5 已上传媒体复用（台账 #5，deferred-verdicts 设计）：cache_data 为
    outbox 条目 dict、cache_path 为条目文件路径。条目内 upload_cache 凭证
    （file_md5/media/cipher_size/uploaded_at）与当前文件 md5 一致且在 TTL
    内时跳过上传直接复用；否则全程上传并把凭证落盘进条目，等下一次重试
    复用。返回 (media, cipher_size, from_cache)。"""
    if cache_data is None:
        media, cipher_size = upload_media(session, to_user_id, file_path, kind, log=log)
        return media, cipher_size, False
    try:
        fmd5 = _file_md5(file_path)
    except OSError:
        fmd5 = None
    cache = cache_data.get("upload_cache")
    if fmd5 and isinstance(cache, dict) and cache.get("file_md5") == fmd5 \
            and isinstance(cache.get("media"), dict) \
            and time.time() - float(cache.get("uploaded_at") or 0) <= _UPLOAD_CACHE_TTL_SEC:
        log(f"媒体复用已上传凭证 kind={kind} file={os.path.basename(file_path)}")
        return cache["media"], int(cache.get("cipher_size") or 0), True
    media, cipher_size = upload_media(session, to_user_id, file_path, kind, log=log)
    if fmd5:
        cache_data["upload_cache"] = {"file_md5": fmd5, "media": media,
                                      "cipher_size": cipher_size,
                                      "uploaded_at": time.time()}
        if cache_path:
            try:
                write_json_locked(cache_path, cache_data)
            except Exception:
                pass  # 凭证落盘失败不阻断本次发送，下次重试自然全程上传
    return media, cipher_size, False


def send_media_message(session, to_user_id, file_path, kind, context_token,
                       client_id=None, log=serve_log,
                       cache_data=None, cache_path=None):
    """上传并发送一条媒体消息（image/video/file）。上传只做一次，
    sendmessage 的 -14 重试复用同一份已上传媒体与 client_id。
    带 cache_data（outbox 条目）时走 A5 凭证复用：本次新上传的凭证随
    条目落盘、供发送失败后的重试跳过上传；复用凭证发送失败则当场作废
    凭证，下次重试回全程（防过期引用硬发造新失败形态）。"""
    media, cipher_size, from_cache = _upload_with_cache(
        session, to_user_id, file_path, kind, cache_data, cache_path, log)
    if kind == "image":
        item = {"type": 2, "image_item": {"media": media, "mid_size": cipher_size}}
    elif kind == "video":
        item = {"type": 5, "video_item": {"media": media, "video_size": cipher_size}}
    else:
        item = {"type": 4, "file_item": {"media": media,
                                         "file_name": os.path.basename(file_path),
                                         "len": str(os.path.getsize(file_path))}}
    try:
        return _send_items(session, to_user_id, [item], context_token,
                           client_id=client_id, log=log)
    except Exception:
        if from_cache and cache_data is not None:
            cache_data.pop("upload_cache", None)
            if cache_path:
                try:
                    write_json_locked(cache_path, cache_data)
                except Exception:
                    pass
            log("复用凭证发送失败，凭证已作废，下次重试重新上传")
        raise


_typing_cache = {"ticket": None, "at": 0.0}


def get_typing_ticket(session, user_id, context_token):
    now = time.time()
    if _typing_cache["ticket"] and now - _typing_cache["at"] < 600:
        return _typing_cache["ticket"]
    saved = read_json(TYPING_FILE, {}) or {}
    if saved.get("ticket") and now - float(saved.get("at", 0)) < 600:
        _typing_cache.update(ticket=saved["ticket"], at=float(saved["at"]))
        return saved["ticket"]
    payload = {"ilink_user_id": user_id, "base_info": base_info()}
    if context_token:
        payload["context_token"] = context_token
    resp = http_json("POST", session_base(session) + "/ilink/bot/getconfig",
                     headers=bot_headers(session["bot_token"]), payload=payload, timeout=15)
    ticket = resp.get("typing_ticket")
    if ticket:
        _typing_cache.update(ticket=ticket, at=now)
        write_json_locked(TYPING_FILE, {"ticket": ticket, "at": now})
    return ticket


def send_typing(session, user_id, status, context_token=None, log=None):
    """status 1=正在输入 2=取消。失败不抛异常，不影响发消息。"""
    try:
        ticket = get_typing_ticket(session, user_id, context_token)
        if not ticket:
            return False
        payload = {"ilink_user_id": user_id, "typing_ticket": ticket,
                   "status": status, "base_info": base_info()}
        http_json("POST", session_base(session) + "/ilink/bot/sendtyping",
                  headers=bot_headers(session["bot_token"]), payload=payload, timeout=15)
        return True
    except Exception as e:
        if log:
            log(f"sendtyping({status}) 失败（忽略）：{type(e).__name__}: {e}")
        return False


def notify(session, kind, log=serve_log):
    try:
        r = http_json("POST", session_base(session) + f"/ilink/bot/msg/notify{kind}",
                      headers=bot_headers(session["bot_token"]),
                      payload={"base_info": base_info()}, timeout=15)
        if ret_code(r) not in (0, None):
            log(f"notify{kind} 警告 ret={ret_code(r)}")
    except Exception as e:
        log(f"notify{kind} 警告：{type(e).__name__}: {e}")


# ---------------- login / wait-login ----------------

def _ascii_qr(content):
    try:
        import qrcode as qr_lib
        qr = qr_lib.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        m = qr.get_matrix()
        lines = []
        for y in range(0, len(m), 2):
            row = ""
            for x in range(len(m[y])):
                top = m[y][x]
                bot = m[y + 1][x] if y + 1 < len(m) else False
                row += "█" if (top and bot) else ("▀" if top else ("▄" if bot else " "))
            lines.append(row)
        return "\n".join(lines)
    except Exception:
        return "(二维码生成失败，请看图片文件)"


def cmd_login(args):
    ensure_dirs()
    cfg()
    resp = http_json("POST", API_BASE + "/ilink/bot/get_bot_qrcode?bot_type=3",
                     headers=login_headers(), payload={"local_token_list": []}, timeout=30)
    if isinstance(resp, dict) and resp.get("ret") is not None and resp.get("ret") != 0:
        print(f"取码失败：ret={resp.get('ret')} errmsg={resp.get('errmsg')}。"
              f"你需要做：稍后重新运行 login。", file=sys.stderr)
        return 1
    qrcode_val = (resp or {}).get("qrcode")
    img_content = (resp or {}).get("qrcode_img_content")
    if not qrcode_val or not img_content:
        print("取码失败：响应缺少 qrcode 或 qrcode_img_content。"
              "你需要做：稍后重新运行 login。", file=sys.stderr)
        return 1
    write_json_locked(QR_PENDING_FILE,
                      {"qrcode": qrcode_val, "created_at": time.time()}, mode=0o600)
    try:
        import qrcode as qr_lib
        qr_lib.make(img_content).save(QR_PNG)
    except Exception as e:
        print(f"警告：二维码图片生成失败（{e}），可看终端 ASCII 二维码", file=sys.stderr)
    print("请用微信扫一扫扫描下方二维码（有效期约 2 分钟）：")
    print(_ascii_qr(img_content))
    print(f"二维码图片：{QR_PNG}")
    print("扫完后运行：python3 bridge.py wait-login")
    return 0


def cmd_wait_login(args):
    ensure_dirs()
    cfg()
    pending = read_json(QR_PENDING_FILE, {}) or {}
    qrcode_val = pending.get("qrcode")
    if not qrcode_val:
        print("没有待确认的二维码。你需要做：先运行 login 取码。", file=sys.stderr)
        return 1
    verify_code = args.verify_code or ""
    host = API_BASE
    deadline = time.time() + 180  # 总时长上限 3 分钟
    scaned_hinted = False
    print("开始轮询扫码状态（最长 3 分钟）...")
    while time.time() < deadline:
        q = urllib.parse.urlencode({"qrcode": qrcode_val})
        if verify_code:
            q += "&" + urllib.parse.urlencode({"verify_code": verify_code})
        try:
            resp = http_json("GET", f"{host}/ilink/bot/get_qrcode_status?{q}",
                             headers=poll_headers(), payload=None, timeout=35)
        except Exception:
            resp = {"status": "wait"}  # 请求失败按 wait 处理
        status = resp.get("status") or "wait"
        if status == "wait":
            time.sleep(1)
            continue
        if status == "scaned":
            if not scaned_hinted:
                print("已扫码，请在手机上确认")
                scaned_hinted = True
            time.sleep(1)
            continue
        if status == "scaned_but_redirect":
            rh = resp.get("redirect_host")
            if rh:
                host = (rh if rh.startswith("http") else "https://" + rh.strip("/")).rstrip("/")
            time.sleep(1)
            continue
        if status == "binded_redirect":
            # 平台说「已绑定、现有 session 可用」即登录态已恢复：必须清
            # NEEDS_RELOGIN/notified_relogin（与 confirmed 分支同口径）。
            # 否则过期标记残留会让 serve 永久空转、watchdog 永不拉起，
            # 恢复链在最后一步断裂（bughunt-2026-10-24 面 7）。
            clear_relogin()
            print("该 Bot 已绑定到现有实例，直接使用现有 session 即可。")
            return 0
        if status == "confirmed":
            token = resp.get("bot_token")
            if not token:
                print("登录失败：confirmed 但未返回 bot_token。你需要做：重新运行 login。", file=sys.stderr)
                return 1
            save_session({"bot_token": token, "baseurl": resp.get("baseurl") or host,
                          "ilink_bot_id": resp.get("ilink_bot_id"),
                          "ilink_user_id": resp.get("ilink_user_id"),
                          "confirmed_at": iso()})
            clear_relogin()
            try:
                os.remove(QR_PENDING_FILE)
            except OSError:
                pass
            print("登录成功！")
            print(f"  bot_token：{mask_secret(token)}")
            print(f"  ilink_bot_id：{resp.get('ilink_bot_id')}")
            print(f"  ilink_user_id：{mask_user(resp.get('ilink_user_id'))}")
            return 0
        if status == "expired":
            print("二维码已过期。你需要做：重新运行 login 取新码。", file=sys.stderr)
            return 2
        if status == "need_verifycode":
            print("需要验证码：请把手机微信上显示的验证码发给我，再用 "
                  "wait-login --verify-code <验证码> 继续。", file=sys.stderr)
            return 3
        if status == "verify_code_blocked":
            print("验证码输错次数过多，已被拦截。你需要做：重新运行 login 取新码。", file=sys.stderr)
            return 4
        time.sleep(1)
    print("等待超时（3 分钟）。你需要做：重新运行 login。", file=sys.stderr)
    return 2


# ---------------- serve：共享状态 ----------------

_stop = threading.Event()
_typing_lock = threading.Lock()


def _paused():
    return os.path.exists(PAUSED_FILE)


def _needs_relogin():
    return os.path.exists(NEEDS_RELOGIN_FILE)


def touch_trigger_and_spawn_worker(log=serve_log):
    try:
        with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    worker = os.path.join(HOME, "worker.py")
    if _paused() or not os.path.exists(worker):
        return
    try:
        wlog = open(os.path.join(STATE, "worker.log"), "a", encoding="utf-8")
        subprocess.Popen([sys.executable, worker], cwd=HOME, stdin=subprocess.DEVNULL,
                         stdout=wlog, stderr=wlog, start_new_session=True)
    except OSError as e:
        log(f"启动 worker 失败：{e}")


def _extract_ref_text(item):
    ref = item.get("ref_msg") or {}
    if ref.get("title"):
        return str(ref["title"])
    mi = ref.get("message_item") or {}
    if mi.get("text_item"):
        return str((mi["text_item"] or {}).get("text") or "")
    if mi.get("voice_item"):
        return str((mi["voice_item"] or {}).get("text") or "")
    return ""


def _classify(msg, log):
    """返回 (type, text, ref_text)。type: text/voice/image/video/file/mixed/unknown。"""
    kinds = []
    texts = []
    ref_text = ""
    unknown_types = []
    for it in msg.get("item_list") or []:
        t = it.get("type")
        if t == 1:
            kinds.append("text")
            texts.append(_clean_text((it.get("text_item") or {}).get("text") or ""))
        elif t == 3:
            v = it.get("voice_item") or {}
            if v.get("text"):
                kinds.append("text")
                texts.append(_clean_text(v["text"]))
            else:
                kinds.append("voice")
        elif t == 2:
            kinds.append("image")
        elif t == 5:
            kinds.append("video")
        elif t == 4:
            kinds.append("file")
        else:
            kinds.append("unknown")
            unknown_types.append(t)
        if not ref_text:
            ref_text = _clean_text(_extract_ref_text(it))
    if unknown_types:
        log(f"收到未知 item 类型编号：{unknown_types}（message_id={msg.get('message_id')}）")
    uniq = set(kinds)
    if not uniq:
        typ = "unknown"
    elif len(uniq) == 1:
        typ = kinds[0]
    elif uniq == {"text"}:
        typ = "text"
    else:
        typ = "mixed"
    return typ, "\n".join(x for x in texts if x), ref_text


def _handle_recall(msg):
    """若该消息已有 inbox 文件且本次带 delete_time_ms，标记 recalled。返回 True 表示已处理。"""
    mid = str(msg.get("message_id"))
    path = os.path.join(INBOX, mid + ".json")
    if msg.get("delete_time_ms") and os.path.exists(path):
        with state_lock():
            rec = read_json(path, None)
            if rec is not None and not rec.get("recalled"):
                rec["recalled"] = True
                atomic_write_json(path, rec)
        serve_log(f"消息已撤回标记 id={mid}")
        return True
    return False


def _wait_with_heartbeat(seconds):
    """长等待（如 -14 暂停）期间每 30 秒续写心跳，避免看门狗把合规暂停误判为假活。"""
    end = time.time() + seconds
    while not _stop.is_set() and time.time() < end:
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "pid": os.getpid()}))
        except OSError:
            pass
        _stop.wait(min(30, max(0, end - time.time())))


def _receiver(session, state):
    my_id = session.get("ilink_user_id")
    cursor = (read_json(CURSOR_FILE, {}) or {}).get("get_updates_buf", "")
    longpoll_ms = None
    retried_14 = False
    fail_streak = 0
    loop_n = 0
    while not _stop.is_set():
        loop_n += 1
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "pid": os.getpid()}))
        except OSError:
            pass
        if loop_n % 200 == 0:
            _prune_history()
            _check_disk_space()
        if _needs_relogin():
            _stop.wait(60)
            continue
        timeout = 45 if longpoll_ms is None else longpoll_ms / 1000.0 + 10
        try:
            resp = http_json("POST", session_base(session) + "/ilink/bot/getupdates",
                             headers=bot_headers(session["bot_token"]),
                             payload={"get_updates_buf": cursor, "base_info": base_info()},
                             timeout=timeout, retry_timeouts=False)
        except TimeoutError:
            continue  # 长轮询超时视为空结果，不算错误
        except Exception as e:
            fail_streak += 1
            delay = min(3 * (2 ** min(fail_streak - 1, 4)), 30)
            serve_log(f"getupdates 请求异常：{type(e).__name__}: {e}，{delay}s 后重试（连续失败 {fail_streak} 次）")
            _stop.wait(delay)
            continue
        code = ret_code(resp)
        if code == -14:
            if not retried_14:
                serve_log(f"getupdates 收到 -14（可能是限流），暂停 {PAUSE_14_SEC}s 后重试一次")
                retried_14 = True
                _wait_with_heartbeat(PAUSE_14_SEC)
                continue
            mark_needs_relogin("getupdates 连续返回 -14，重试后仍失败")
            serve_log("getupdates 连续 -14，已写 NEEDS_RELOGIN，serve 进入空转")
            retried_14 = False
            continue
        if code not in (0, None):
            fail_streak += 1
            delay = min(3 * (2 ** min(fail_streak - 1, 4)), 30)
            serve_log(f"getupdates 失败 ret/errcode={code} errmsg={resp.get('errmsg')}，{delay}s 后重试")
            _stop.wait(delay)
            continue
        retried_14 = False
        fail_streak = 0
        if resp.get("longpolling_timeout_ms"):
            longpoll_ms = int(resp["longpolling_timeout_ms"])
        msgs = resp.get("msgs") or []
        batch_hold = False
        for msg in msgs:
            # 单条隔离：一条毒消息绝不许拖垮整批与 serve 本身（异常会让
            # 游标停在批前、同批被平台反复重投形成崩溃循环）。清洗层已
            # 在源头中和已知毒型，这里是未知毒型的最后防线：记日志留痕、
            # 跳过本条、继续处理同批后续
            try:
                recorded = _process_inbound(session, msg, my_id, state)
            except Exception as e:
                serve_log(f"入站单条处理异常已隔离 id={msg.get('message_id')}："
                          f"{type(e).__name__}: {e}")
                recorded = True  # 内容毒型：跳过本条，游标照常推进防重投死循环
            if recorded is False:
                batch_hold = True  # 存储类落盘失败：本批游标不推进
        # 先写 inbox（在 _process_inbound 内完成），再保存游标。本批有
        # 消息因存储异常未落盘时，游标（内存与落盘）保持旧值，等平台
        # 按旧游标重投整批——已落盘条目靠 message_id 去重吸收，绝不
        # 让 ENOSPC 之类落盘失败变成永久丢消息。
        new_buf = resp.get("get_updates_buf")
        if new_buf and batch_hold:
            serve_log("本批有入站消息落盘失败（存储异常），游标保持不推进，等待平台重投")
        elif new_buf:
            try:
                write_json_locked(CURSOR_FILE, {"get_updates_buf": new_buf,
                                                "updated_at": time.time()})
                cursor = new_buf
            except OSError as e:
                # 游标自身落盘失败：消息已在 inbox 耐久落盘，保持旧
                # 游标等重投（去重吸收），且不许打死接收线程。
                serve_log(f"游标落盘失败（{type(e).__name__}: {e}），保持旧游标等待重投")


def is_director_command(text, prefix="#"):
    """阶段六：# 开头的文本消息是导演指令（判定函数，供 serve 分流与自检共用）。"""
    return bool(prefix) and isinstance(text, str) and text.lstrip().startswith(prefix)


# ---------------- 入站媒体探针（实施第 0 步，被动观测） ----------------
# 含非纯文本 item 的入站消息，把 item 的「形态」追加到 probe_inbound.jsonl，
# 供后续按 message_id 核对协议文档的字段形态。只记形态不记值：字符串一律
# 只记长度（凭证与聊天内容都不落盘，URL 连域名都不记），数值/布尔记原值
# （宽高、时长、大小这类非敏感量）。探针失败静默降级，绝不影响主流程。

_PROBE_CRED_KEYS = {"aes_key", "aeskey", "encrypt_query_param", "full_url", "url"}


def _probe_scalar(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "bool", "value": v}
    if isinstance(v, (int, float)):
        return {"type": type(v).__name__, "value": v}
    if isinstance(v, str):
        return {"type": "str", "len": len(v)}
    if isinstance(v, list):
        return {"type": "list", "len": len(v)}
    return {"type": type(v).__name__}


def _probe_field(v, depth):
    if isinstance(v, dict):
        if depth < 2:
            return _probe_obj(v, depth)
        return {"type": "dict", "keys": sorted(v.keys())}
    return _probe_scalar(v)


def _probe_obj(obj, depth=0):
    return {"keys": sorted(obj.keys()),
            "fields": {k: _probe_field(obj[k], depth + 1) for k in sorted(obj.keys())}}


def _probe_collect_keys(obj, acc, depth=0):
    if depth > 4 or not isinstance(obj, dict):
        return
    for k, v in obj.items():
        acc.add(k)
        if isinstance(v, dict):
            _probe_collect_keys(v, acc, depth + 1)


def _probe_item(it, idx):
    if not isinstance(it, dict):
        return {"index": idx, "raw_type": type(it).__name__}
    t = it.get("type")
    item = {"index": idx,
            "type": t if isinstance(t, int) and not isinstance(t, bool) else None,
            "keys": sorted(it.keys()),
            "payloads": {}}
    if t is not None and not isinstance(t, int):
        item["type_kind"] = type(t).__name__
    for k in sorted(it.keys()):
        if k.endswith("_item") and isinstance(it.get(k), dict):
            item["payloads"][k] = _probe_obj(it[k])
    acc = set()
    _probe_collect_keys(it, acc)
    item["cred_fields_present"] = sorted(acc & _PROBE_CRED_KEYS)
    return item


def _msg_needs_probe(msg):
    """消息含非纯文本内容才探：任一 item 不是标准文本 item（type==1+text_item）。"""
    for it in msg.get("item_list") or []:
        if not isinstance(it, dict):
            return True
        payload_keys = [k for k in it.keys() if k.endswith("_item")]
        if it.get("type") != 1 or not payload_keys:
            return True
        if any(k != "text_item" for k in payload_keys):
            return True
    return False


def probe_inbound(msg):
    """被动探针入口：需要时把形态记录 append 到 PROBE_INBOUND_FILE；自身永不抛错。"""
    try:
        if not _msg_needs_probe(msg):
            return
        rec = {"probe": "inbound_media",
               "received_at": time.time(),
               "message_id": str(msg.get("message_id")),
               "top_keys": sorted(msg.keys()),
               "items": [_probe_item(it, i)
                         for i, it in enumerate(msg.get("item_list") or [])]}
        append_jsonl(PROBE_INBOUND_FILE, rec)
    except Exception as e:
        try:
            serve_log(f"入站探针写入失败（忽略）：{type(e).__name__}: {e}")
        except Exception:
            pass


# ---------------- 入站媒体（实施第 1 步：图片下载解密落盘） ----------------
# 图片 item 自带 CDN 凭证（media.full_url / encrypt_query_param）与解密密钥
# （image_item.aeskey 优先），收件时同步取件解密落 state/media_in/，记录以
# media_file 标记，供回复轮次读图。凭证只随 inbox 内部记录（media_cred）留存，
# serve.log 与一切输出只记失败类别、不记值（延续探针的防泄漏口径）。
# 任何失败都降级为「无文件」：记录不带 media_file，其余流程与从前一致。

_INBOUND_IMG_MAX = 25 * 1024 * 1024  # 密文上限 25MB，超限降级
_INBOUND_DL_TIMEOUT = 90             # 同步下载超时上限（秒）


def _image_magic_ext(data):
    """按魔数判图片格式返回扩展名（不带点）；未知返回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None



def _image_integrity_ok(data, ext):
    """图片完整性（纯函数，供自检）：魔数只验头，截断文件照样过——
    按格式验尾部结构：JPEG 须以 FFD9（EOI）结尾、PNG 须以 IEND 块
    结尾、GIF 须以 0x3B 结尾、WebP 的 RIFF 声明长度须与实际一致。
    任一不符判截断/损坏。"""
    if ext == "jpg":
        return len(data) >= 4 and data[-2:] == b"\xff\xd9"
    if ext == "png":
        return len(data) >= 12 and data[-8:-4] == b"IEND"
    if ext == "gif":
        return len(data) >= 2 and data[-1:] == b";"
    if ext == "webp":
        if len(data) < 12:
            return False
        return int.from_bytes(data[4:8], "little") + 8 == len(data)
    return False

def _inbound_media_key(media):
    """CDNMedia.aes_key 解码为 16 字节原始密钥：base64 解码得 16 字节，
    或 base64 里包着的 32 字符 hex 再解码。无可用密钥返回 None。"""
    ak = (media or {}).get("aes_key")
    if isinstance(ak, str) and ak.strip():
        try:
            raw = base64.b64decode(ak.strip())
        except Exception:
            raw = b""
        if len(raw) == 16:
            return raw
        if len(raw) == 32:
            try:
                inner = bytes.fromhex(raw.decode("ascii"))
                if len(inner) == 16:
                    return inner
            except Exception:
                pass
    return None


def _inbound_image_key(image_item):
    """入站图片解密密钥：优先 image_item.aeskey（32 字符 hex 解码 16 字节，
    官方 types.ts 注释明示 preferred over media.aes_key），次选 media.aes_key。
    无可用密钥返回 None（官方口径此时字节可能为明文下发，靠魔数判定把关）。"""
    aeskey = image_item.get("aeskey")
    if isinstance(aeskey, str) and aeskey.strip():
        try:
            raw = bytes.fromhex(aeskey.strip())
            if len(raw) == 16:
                return raw
        except ValueError:
            pass
    return _inbound_media_key(image_item.get("media"))


_INBOUND_IMG_PER_MSG_MAX = 9  # 单条消息最多取 9 张图（微信多图条数口径）


def _save_inbound_image(item, mid, suffix, log):
    """下载解密单个图片 item 并落盘 state/media_in/<mid><suffix>.<ext>
    （suffix 为 "" 或 ".i<n>"），字节即平台原图、桥侧不做任何缩放。
    返回相对路径；任一步失败返回 None，失败类别只记日志、不记凭证值。"""
    media = item.get("media") or {}
    tag = f"{mid}{suffix}"
    try:
        url = media.get("full_url") or ""
        if not url and media.get("encrypt_query_param"):
            url = (CDN_BASE_URL + "/download?encrypted_query_param="
                   + urllib.parse.quote(str(media["encrypt_query_param"]), safe=""))
        if not url:
            log(f"入站图片降级 id={tag} 类别=无下载引用")
            return None
        try:
            status, raw = _cdn_get(url, timeout=_INBOUND_DL_TIMEOUT)
        except urllib.error.HTTPError as e:
            log(f"入站图片降级 id={tag} 类别=HTTP {e.code}")
            return None
        except Exception as e:
            log(f"入站图片降级 id={tag} 类别={'超时' if _is_timeout_exc(e) else '连接失败'}")
            return None
        if status != 200:
            log(f"入站图片降级 id={tag} 类别=HTTP {status}")
            return None
        if len(raw) > _INBOUND_IMG_MAX:
            log(f"入站图片降级 id={tag} 类别=超限 密文{len(raw)}字节")
            return None
        key = _inbound_image_key(item)
        if key is not None:
            try:
                data = _aes_ecb_decrypt(raw, key)
            except Exception:
                log(f"入站图片降级 id={tag} 类别=解密失败")
                return None
        else:
            data = raw
        ext = _image_magic_ext(data)
        os.makedirs(MEDIA_IN, exist_ok=True)
        if ext is None:
            with open(os.path.join(MEDIA_IN, mid + suffix + ".bin"), "wb") as f:
                f.write(data)
            log(f"入站图片降级 id={tag} 类别=格式未知（已存 .bin）")
            return None
        if not _image_integrity_ok(data, ext):
            # 截断/损坏：不落盘，交给上游降级＋延迟补取（一次为限），
            # 不把坏图喂给回复轮次的读图。
            log(f"入站图片降级 id={tag} 类别=完整性（{ext} 尾部结构不符，疑似截断）")
            return None
        with open(os.path.join(MEDIA_IN, mid + suffix + "." + ext), "wb") as f:
            f.write(data)
        # 存储的相对路径一律用正斜杠（Windows 上 os.path.join 产反斜杠，
        # 会打破 inbox/history 的既定口径；正斜杠在 Windows 同样合法）。
        return "state/media_in/" + mid + suffix + "." + ext
    except Exception as e:
        log(f"入站图片降级 id={tag} 类别=异常 {type(e).__name__}")
        return None


def _fetch_inbound_images(msg, log=serve_log):
    """同步下载解密消息里的全部图片（多图全收）。返回
    (media_files, media_cred)：media_files 为相对 HOME 的路径列表
    （首图 state/media_in/<message_id>.<ext>、其余 .i<n>.<ext>，顺序即
    消息内顺序）；单张失败只跳过该张、其余照常；全失败为 []；消息里
    没有图片 item 时 ([], None)。media_cred 顶层保留首图 aeskey/media
    （旧口径），items 为逐图凭证列表，供延迟补取重建多图消息；
    凭证仅供随 inbox 内部记录留存，绝不进日志/输出。"""
    items = []
    for it in msg.get("item_list") or []:
        if isinstance(it, dict) and it.get("type") == 2 \
                and isinstance(it.get("image_item"), dict):
            items.append(it["image_item"])
    if not items:
        return [], None
    mid = str(msg.get("message_id"))
    cred = {"aeskey": items[0].get("aeskey"),
            "media": items[0].get("media") or {},
            "items": [{"aeskey": it.get("aeskey"),
                       "media": it.get("media") or {}} for it in items]}
    files = []
    for idx, item in enumerate(items[:_INBOUND_IMG_PER_MSG_MAX]):
        suffix = "" if idx == 0 else f".i{idx + 1}"
        rel = _save_inbound_image(item, mid, suffix, log)
        if rel:
            files.append(rel)
    if len(items) > _INBOUND_IMG_PER_MSG_MAX:
        log(f"入站图片超量 id={mid} 共{len(items)}张 只取前{_INBOUND_IMG_PER_MSG_MAX}张")
    return files, cred


def _fetch_inbound_image(msg, log=serve_log):
    """兼容包装：返回首图 (media_file, media_cred)，旧调用口径不变。
    新代码请用 _fetch_inbound_images 取全部图片。"""
    files, cred = _fetch_inbound_images(msg, log=log)
    return (files[0] if files else None), cred


# ---------------- 入站视频（下载解密 + 理解管线） ----------------
# 字段口径（官方 Tencent/openclaw-weixin src/api/types.ts 的 VideoItem 与
# src/media/media-download.ts 的 VIDEO 分支）：video_item 没有 aeskey
# 字段，密钥只走 media.aes_key（base64）；media 为
# CDNMedia{encrypt_query_param, aes_key, encrypt_type, full_url}，另有
# video_size / play_length / video_md5 与 thumb_media（封面 CDN 引用，本桥
# 不用，抽帧自带画面）。官方下载器对视频要求 media.aes_key 存在才下载，
# 解密同为 AES-128-ECB + PKCS#7，落盘 video/mp4。本桥在此之上做理解管线：
# ffprobe 元信息 + ffmpeg 抽帧（均匀点＋场景切换点）+ 音轨语音转写，汇成
# 视频简报随记录留存，帧图供回复轮次读图消费、转写供生成层对上对方
# 说了什么。理解是「抽帧＋转写」的近似，不是逐帧观看——状态如实降级、
# 绝不假装看清听清（五档状态口径见转写节）。
# 凭证只随 inbox 内部记录（video_cred）留存，日志只记失败类别、不记值；
# 下载/解密/格式失败降级：记录不带 video_file；抽帧/转写失败各自降级
# （无帧走兜底句、无转写只评画面）。

_INBOUND_VID_MAX = 60 * 1024 * 1024  # 密文上限 60MB，超限降级
_INBOUND_VID_FRAME_MAX = 8        # 帧数封顶（均匀 5 点＋场景切换点合并去重后）
_VID_SCENE_MAX = 5                # 场景切换候选点上限
_VID_SCENE_THRESHOLD = 0.35       # ffmpeg scene 分数阈值
_VID_SCENE_DETECT_MAX_SEC = 600   # 超过此时长不再跑场景检测（整片解码太慢）


def _probe_video_meta(mp4_abs):
    """ffprobe 一次取齐元信息 → {duration_sec, width, height, has_audio}；
    探针失败返回 {}（调用方按缺失降级，不猜）。"""
    if not shutil.which("ffprobe"):
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", mp4_abs],
            capture_output=True, timeout=30)
        info = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}
    if not isinstance(info, dict):
        return {}
    meta = {}
    try:
        dur = float((info.get("format") or {}).get("duration"))
        if dur > 0:
            meta["duration_sec"] = round(dur, 1)
    except (TypeError, ValueError):
        pass
    vwidth = vheight = 0
    has_audio = False
    for st in info.get("streams") or []:
        if not isinstance(st, dict):
            continue
        if st.get("codec_type") == "video" and not vwidth:
            try:
                vwidth = int(st.get("width") or 0)
                vheight = int(st.get("height") or 0)
            except (TypeError, ValueError):
                vwidth = vheight = 0
        elif st.get("codec_type") == "audio":
            has_audio = True
    if not meta.get("duration_sec") and not vwidth:
        return {}  # 时长与视频流都探不到：不是可读视频，按探针失败处理
    if vwidth:
        meta["width"] = vwidth
        meta["height"] = vheight
    meta["has_audio"] = has_audio
    return meta


def _video_scene_times(mp4_abs, duration):
    """ffmpeg 场景切换检测 → 候选时刻（秒）：过滤首尾 0.3 秒内、相邻
    间隔 ≥1 秒、最多 _VID_SCENE_MAX 个。ffmpeg 缺失/解析失败返回 []。"""
    if not shutil.which("ffmpeg"):
        return []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", mp4_abs,
             "-vf", f"select='gt(scene,{_VID_SCENE_THRESHOLD})',showinfo",
             "-f", "null", "-"],
            capture_output=True, timeout=120)
        text = proc.stderr.decode("utf-8", "replace")
    except Exception:
        return []
    times = []
    for mobj in re.finditer(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)", text):
        t = float(mobj.group(1))
        if t <= 0.3 or t >= duration - 0.3:
            continue
        if times and t - times[-1] < 1.0:
            continue
        times.append(t)
        if len(times) >= _VID_SCENE_MAX:
            break
    return times


def _extract_video_frames(mp4_abs, mid, meta=None, log=serve_log):
    """抽帧：均匀 5 点（约 5%/30%/55%/80%/95%）＋场景切换点合并、0.75 秒
    内去重、按时间排序、封顶 _INBOUND_VID_FRAME_MAX 帧（宽 ≤720 的 JPEG）
    存 MEDIA_IN/<mid>.f<n>.jpg，返回帧图相对路径列表（顺序即时间顺序）。
    ffmpeg 缺失、时长未知或全部抽帧失败都返回 []（调用方降级）。"""
    if not shutil.which("ffmpeg"):
        log(f"入站视频抽帧跳过 id={mid} 类别=无ffmpeg")
        return []
    meta = meta or _probe_video_meta(mp4_abs)
    duration = meta.get("duration_sec") or 0
    if not duration or duration <= 0:
        log(f"入站视频抽帧跳过 id={mid} 类别=时长未知")
        return []
    # 宽度口径：原宽 >720 才缩放（scale 的表达式写法在滤镜图里逗号会被当作
    # 滤镜分隔符，故不用 min() 表达式，改为按已探宽度只在需要时挂纯数值 scale）
    vf = ["-vf", "scale=720:-2"] if (meta.get("width") or 0) > 720 else []
    times = [round(duration * p, 2) for p in (0.05, 0.30, 0.55, 0.80, 0.95)]
    if duration <= _VID_SCENE_DETECT_MAX_SEC:
        times += _video_scene_times(mp4_abs, duration)
    picked = []
    for t in sorted(times):
        if picked and t - picked[-1] < 0.75:
            continue
        picked.append(t)
    if len(picked) > _INBOUND_VID_FRAME_MAX:
        step = (len(picked) - 1) / (_INBOUND_VID_FRAME_MAX - 1)
        picked = [picked[round(i * step)] for i in range(_INBOUND_VID_FRAME_MAX)]
    frames = []
    for i, ts in enumerate(picked, 1):
        out_abs = os.path.join(MEDIA_IN, f"{mid}.f{i}.jpg")
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", mp4_abs,
                 "-frames:v", "1"] + vf + ["-q:v", "3", "-y", out_abs],
                capture_output=True, timeout=60)
        except Exception:
            continue
        ok = proc.returncode == 0 and os.path.exists(out_abs)
        if ok:
            try:
                with open(out_abs, "rb") as f:
                    ok = f.read(3) == b"\xff\xd8\xff"
            except OSError:
                ok = False
        if ok:
            frames.append(f"state/media_in/{mid}.f{i}.jpg")  # 存储路径正斜杠，见图片侧同款说明
        else:
            try:
                os.remove(out_abs)
            except OSError:
                pass
    return frames


# ---------------- 入站视频语音转写 ----------------
# 口径：ffmpeg 先把音轨解成 16kHz 单声道 wav，再由 STT 子进程把 PCM
# 读成 numpy 数组喂 faster-whisper——按文件路径转写在部分 av 版本下
# 必崩，不许改回路径输入。serve 本体不保证装有 faster-whisper，走子
# 进程隔离。结果随 inbox/history 记录留存（video_transcript），不新增
# 文件。状态如实降级：ok / partial（超长只转前段）/ no_audio（无音轨）/
# no_speech（有音轨但无可辨人声，含纯音乐）/ failed（转写不可用）。
# 部署：准备一个装有 faster-whisper 的 venv（如与桥同级的 stt-venv，
# 或用户家目录 ~/stt-venv），或用环境变量 COMPANION_STT_PYTHON 指定
# 解释器；模型缓存目录默认 ~/.cache/huggingface，已有 HF_HOME 时尊重之。

def _resolve_stt_python():
    """STT 解释器定位：env COMPANION_STT_PYTHON → 桥 HOME 同级
    stt-venv → 用户家目录 ~/stt-venv → 当前解释器（其环境装有
    faster-whisper 时）。serve 的运行身份与登录用户不同（如以 root
    运行）时，只靠 expanduser("~") 会解析到错误家目录、venv 必然不
    存在——故同级目录优先。子路径按平台取（POSIX bin/python、
    Windows Scripts/python.exe）。"""
    env = os.environ.get("COMPANION_STT_PYTHON")
    if env:
        return env
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    sib = os.path.join(os.path.dirname(HOME), "stt-venv", sub, exe)
    if os.path.exists(sib):
        return sib
    home_v = os.path.join(os.path.expanduser("~"), "stt-venv", sub, exe)
    if os.path.exists(home_v):
        return home_v
    try:
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is not None:
            return sys.executable
    except Exception:
        pass
    return home_v  # 不存在 → 前置判定按「无 stt 环境」如实降级


_STT_PYTHON = _resolve_stt_python()


def _stt_child_env():
    """转写子进程环境：serve 以无 HOME 或异身份运行（如 systemd 以
    root 运行）时，faster-whisper 默认模型缓存会落到错误家目录
    （模型不在那里、甚至落在易失盘上）。env 已有 HF_HOME 时尊重之；
    否则用户家目录缓存真实存在才注入。"""
    env = dict(os.environ)
    if not env.get("HF_HOME"):
        cand = os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        if os.path.isdir(cand):
            env["HF_HOME"] = cand
    return env


def _stt_setting(key, default):
    """config 的 stt 节读取（model/language），缺失或非法回默认。"""
    try:
        v = (cfg().get("stt") or {}).get(key)
    except Exception:
        v = None
    return v if isinstance(v, str) and v else default


_VID_TRANSCRIBE_MAX_SEC = 180    # 只转写前 180 秒音频
_VID_TRANSCRIBE_TIMEOUT = 180    # 转写子进程超时（秒）
_VID_TRANSCRIPT_MAX_CHARS = 1500

_VID_WHISPER_CHILD = (
    "import json,sys,wave\n"
    "import numpy as np\n"
    "from faster_whisper import WhisperModel\n"
    "with wave.open(sys.argv[1], 'rb') as w:\n"
    "    raw = w.readframes(w.getnframes())\n"
    "x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0\n"
    "m = WhisperModel(sys.argv[3], device='cpu', compute_type='int8')\n"
    "segs, _info = m.transcribe(x, language=sys.argv[2], vad_filter=True)\n"
    "out = [{'text': s.text, 'nsp': round(float(s.no_speech_prob), 3),"
    " 'alp': round(float(s.avg_logprob), 3)} for s in segs]\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)


def _transcribe_video(mp4_abs, meta, log=serve_log, lang=None):
    """转写视频里的人声 → (transcript, status)。任一步失败都返回
    ('', 类别) 并记日志类别，绝不抛异常打断收件；转写文本不进日志。
    meta 为空（探针失败）报 failed、无音轨报 no_audio，两者不混。"""
    mid = os.path.basename(mp4_abs)
    if not meta:
        return "", "failed"
    if not meta.get("has_audio"):
        return "", "no_audio"
    if not shutil.which("ffmpeg"):
        return "", "failed"
    if not os.path.exists(_STT_PYTHON):
        log(f"入站视频转写跳过 id={mid} 类别=无stt环境")
        return "", "failed"
    lang = lang or _stt_setting("language", "zh")
    model = _stt_setting("model", "base")
    duration = float(meta.get("duration_sec") or 0)
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "audio.wav")
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", mp4_abs,
                 "-t", str(_VID_TRANSCRIBE_MAX_SEC),
                 "-vn", "-ac", "1", "-ar", "16000", "-y", wav],
                capture_output=True, timeout=60)
            if proc.returncode != 0 or not os.path.exists(wav):
                log(f"入站视频转写失败 id={mid} 类别=音轨提取失败")
                return "", "failed"
            try:
                proc = subprocess.run(
                    [_STT_PYTHON, "-c", _VID_WHISPER_CHILD, wav, lang, model],
                    capture_output=True, timeout=_VID_TRANSCRIBE_TIMEOUT,
                    env=_stt_child_env())
            except subprocess.TimeoutExpired:
                log(f"入站视频转写失败 id={mid} 类别=超时")
                return "", "failed"
            if proc.returncode != 0:
                log(f"入站视频转写失败 id={mid} 类别=子进程异常")
                return "", "failed"
            segs = json.loads(
                proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception as e:
        log(f"入站视频转写失败 id={mid} 类别=异常 {type(e).__name__}")
        return "", "failed"
    if not isinstance(segs, list):
        return "", "failed"
    parts = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        try:
            nsp = float(s.get("nsp", 0))
            alp = float(s.get("alp", 0))
        except (TypeError, ValueError):
            continue
        if nsp >= 0.6 or alp <= -1.0:
            continue  # 无人声概率高/置信过低的段不算人声（纯音乐防幻觉）
        t = str(s.get("text", "")).strip()
        if t:
            parts.append(t)
    text = "".join(parts).strip()
    if not text:
        return "", "no_speech"
    status = "partial" if duration > _VID_TRANSCRIBE_MAX_SEC else "ok"
    return text[:_VID_TRANSCRIPT_MAX_CHARS], status


# ---------------- 入站视频转写异步化 ----------------
# 收件只做前置判定：终局状态（探针失败/无音轨/无 ffmpeg/无 stt
# 环境）当场定性；可转写的落 pending 起后台线程，转写就绪后回填
# 原记录并（有文本时）写补达记录触发补评。取舍：生成层先按画面
# 及时回应、语音内容随后补达合并——不让整轮干等 whisper 最坏
# 180s，也不退回「没听清装听懂」：pending 期间口径就是只评画面
# 不编台词。whisper 子进程以信号量串行，防多视频并发吃满 CPU。
# 进程重启会打断在途线程，由启动补扫重新排队（回填幂等）。

_TRANSCRIBE_SEM = threading.Semaphore(1)


def _transcribe_precheck(mp4_abs, meta, log=serve_log):
    """转写前置判定：返回 None＝可转写（走异步 pending）；否则返回
    终局状态串（failed/no_audio）。与 _transcribe_video 的前置口径
    一致（那边保留全套判定供同步直调与自检）。"""
    if not meta:
        return "failed"
    if not meta.get("has_audio"):
        return "no_audio"
    if not shutil.which("ffmpeg"):
        return "failed"
    if not os.path.exists(_STT_PYTHON):
        log(f"入站视频转写跳过 id={os.path.basename(mp4_abs)} 类别=无stt环境")
        return "failed"
    return None


def _spawn_transcribe(mp4_abs, mid, meta, log=serve_log):
    """起后台转写线程。线程内异常全吞＋记日志类别，绝不影响收件。"""
    def _run():
        with _TRANSCRIBE_SEM:
            try:
                text, status = _transcribe_video(mp4_abs, meta, log=log)
            except Exception as e:
                log(f"入站视频转写线程异常 id={mid}：{type(e).__name__}")
                return
            try:
                _apply_transcript_ready(mid, text, status, log=log)
            except Exception as e:
                log(f"入站视频转写回填异常 id={mid}：{type(e).__name__}")

    threading.Thread(target=_run, daemon=True,
                     name=f"vid-transcribe-{mid}").start()


def _apply_transcript_ready(mid, transcript, status, log=serve_log):
    """转写就绪回填（仿 _apply_inbound_refetch 模式）：原 inbox 记录
    就地更新转写字段；有可用转写文本时另写一条 video_transcript
    补达记录并触发 worker——原轮次可能已按画面回复过，这条让生成层
    拿到对方说的话后补评内容；原轮次尚未回复时两条同批领取、合并
    理解。无文本的终局状态只回填、不打扰。补达记录已存在则跳过
    （重复完成/补扫重排幂等）。"""
    mid = str(mid)
    path = os.path.join(INBOX, mid + ".json")
    rec = read_json(path, None)
    if not isinstance(rec, dict):
        return
    rec["video_transcript"] = transcript
    rec["video_transcript_status"] = status
    write_json_locked(path, rec)
    if not transcript:
        log(f"入站视频转写完成 id={mid} 状态={status}（无文本，仅回填）")
        return
    note_path = os.path.join(INBOX, mid + "-transcript.json")
    if os.path.exists(note_path):
        return
    note = {"message_id": mid + "-transcript", "ref_message_id": mid,
            "type": "video_transcript", "text": "",
            "received_at": time.time(),
            "from_user_id": rec.get("from_user_id"),
            "context_token": rec.get("context_token"),
            "create_time_ms": rec.get("create_time_ms"),
            "video_file": rec.get("video_file"),
            "video_frames": rec.get("video_frames"),
            "video_meta": rec.get("video_meta"),
            "video_transcript": transcript,
            "video_transcript_status": status}
    write_json_locked(note_path, note)
    append_jsonl(HISTORY_FILE,
                 {"ts": time.time(), "role": "user", "message_id": mid,
                  "type": "video_transcript",
                  "video_transcript": transcript,
                  "video_transcript_status": status})
    log(f"入站视频转写完成 id={mid} 状态={status}，已写补达记录")
    touch_trigger_and_spawn_worker()


def _transcript_pending_sweep(log=serve_log):
    """serve 启动补扫：进程重启打断在途转写线程会让记录永停
    pending——启动时把 24h 内仍 pending、mp4 还在、至多 3 条重新
    排队转写。返回重排条数。"""
    n = 0
    try:
        names = sorted(os.listdir(INBOX))
    except OSError:
        return 0
    for name in names:
        if n >= 3:
            break
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        rec = read_json(os.path.join(INBOX, name), None)
        if not isinstance(rec, dict):
            continue
        if rec.get("video_transcript_status") != "pending":
            continue
        if time.time() - float(rec.get("received_at") or 0) > 86400:
            continue
        vf = rec.get("video_file")
        if not vf:
            continue
        mp4_abs = os.path.join(HOME, vf)
        if not os.path.exists(mp4_abs):
            continue
        meta = rec.get("video_meta") or {}
        if _transcribe_precheck(mp4_abs, meta, log=log) is None:
            _spawn_transcribe(mp4_abs, str(rec.get("message_id")), meta,
                              log=log)
            n += 1
    if n:
        log(f"serve 启动补扫：{n} 条 pending 视频转写已重新排队")
    return n


def _fetch_inbound_video(msg, log=serve_log):
    """同步下载解密消息里的第一个视频并跑理解管线。返回
    (video_file, frames, cred, vinfo)：video_file 为相对 HOME 的 mp4
    路径（state/media_in/<message_id>.mp4），frames 为帧图相对路径列表
    （可为 []：mp4 已落盘但无帧，按兜底处理）；vinfo 为视频简报原料
    {"meta": 元信息, "transcript": 转写文本, "transcript_status": 状态}。
    下载/解密/格式任一失败一律 (None, None, cred, None)；消息里没有
    视频 item 时 (None, None, None, None)。cred 是凭证原文，仅供随
    inbox 内部记录留存。"""
    item = None
    for it in msg.get("item_list") or []:
        if isinstance(it, dict) and it.get("type") == 5 \
                and isinstance(it.get("video_item"), dict):
            item = it["video_item"]
            break
    if item is None:
        return None, None, None, None
    media = item.get("media") or {}
    cred = {"media": media}
    mid = str(msg.get("message_id"))
    try:
        url = media.get("full_url") or ""
        if not url and media.get("encrypt_query_param"):
            url = (CDN_BASE_URL + "/download?encrypted_query_param="
                   + urllib.parse.quote(str(media["encrypt_query_param"]), safe=""))
        if not url:
            log(f"入站视频降级 id={mid} 类别=无下载引用")
            return None, None, cred, None
        key = _inbound_media_key(media)
        if key is None:
            # 官方下载器口径：视频必须有 media.aes_key 才下载，不做明文猜测
            log(f"入站视频降级 id={mid} 类别=无密钥")
            return None, None, cred, None
        est = item.get("video_size")
        est = est if isinstance(est, int) and est > 0 else 8_000_000
        try:
            status, raw = _cdn_get(url, timeout=_media_io_timeout(est))
        except urllib.error.HTTPError as e:
            log(f"入站视频降级 id={mid} 类别=HTTP {e.code}")
            return None, None, cred, None
        except Exception as e:
            log(f"入站视频降级 id={mid} 类别={'超时' if _is_timeout_exc(e) else '连接失败'}")
            return None, None, cred, None
        if status != 200:
            log(f"入站视频降级 id={mid} 类别=HTTP {status}")
            return None, None, cred, None
        if len(raw) > _INBOUND_VID_MAX:
            log(f"入站视频降级 id={mid} 类别=超限 密文{len(raw)}字节")
            return None, None, cred, None
        try:
            data = _aes_ecb_decrypt(raw, key)
        except Exception:
            log(f"入站视频降级 id={mid} 类别=解密失败")
            return None, None, cred, None
        if len(data) < 8 or data[4:8] != b"ftyp":
            log(f"入站视频降级 id={mid} 类别=格式未知")
            return None, None, cred, None
        want_md5 = str(item.get("video_md5") or "").strip().lower()
        if len(want_md5) == 32 and all(c in "0123456789abcdef" for c in want_md5):
            # 平台声明的明文 md5：不符即截断/错件，降级不落盘。字段
            # 缺失或非标准形态时不判（不因字段口径差异误杀正常视频）。
            if hashlib.md5(data).hexdigest() != want_md5:
                log(f"入站视频降级 id={mid} 类别=完整性（video_md5 不符）")
                return None, None, cred, None
        os.makedirs(MEDIA_IN, exist_ok=True)
        mp4_abs = os.path.join(MEDIA_IN, mid + ".mp4")
        with open(mp4_abs, "wb") as f:
            f.write(data)
        video_rel = "state/media_in/" + mid + ".mp4"  # 存储路径正斜杠，见图片侧同款说明
        meta = _probe_video_meta(mp4_abs)
        if not meta and shutil.which("ffprobe"):
            # ffprobe 在场却探不到时长与视频流：文件截断/损坏，删件
            # 降级，不把坏 mp4 喂给理解管线充数（ffprobe 缺失时不判，
            # 属环境限制而非文件问题）。
            try:
                os.remove(mp4_abs)
            except OSError:
                pass
            log(f"入站视频降级 id={mid} 类别=完整性（探针无元信息）")
            return None, None, cred, None
        frames = _extract_video_frames(mp4_abs, mid, meta=meta, log=log)
        if not frames:
            log(f"入站视频降级 id={mid} 类别=抽帧失败（mp4 已落盘、无帧图）")
        # 转写异步化：同步等 whisper 最坏 180s 会堵住收件线程、拖慢
        # 该轮回复启动。可转写的先落 pending 并起后台线程，转写就绪
        # 后 _apply_transcript_ready 回填＋补达；终局状态（无音轨等）
        # 当场定性，不起线程。
        pre = _transcribe_precheck(mp4_abs, meta, log=log)
        if pre is None:
            transcript, tstatus = "", "pending"
            _spawn_transcribe(mp4_abs, mid, meta, log=log)
        else:
            transcript, tstatus = "", pre
        vinfo = {"meta": meta, "transcript": transcript,
                 "transcript_status": tstatus}
        return video_rel, frames, cred, vinfo
    except Exception as e:
        log(f"入站视频降级 id={mid} 类别=异常 {type(e).__name__}")
        return None, None, cred, None


_INBOUND_REFETCH_DELAY_SEC = 180  # 入站补取延迟：避开瞬时网络抖动窗口，又不至于让她等太久
_INBOUND_REFETCH_LAST = [0.0]


def _inbound_refetchable(cred, kind):
    """补取资格（台账 #15/#26 选项 3「延迟一次按 cred 补取」）：cred 内
    media 带可用下载引用才登记——无引用/视频无密钥在首次即已定性为
    永久失败，补取无意义；其余（网络步失败、状态码异常等）都可能是
    瞬时的，延迟重试一次。解密/超限类重试注定再败，但被「一次为限」
    兜住，成本可控。"""
    if not isinstance(cred, dict):
        return False
    media = cred.get("media") or {}
    if not (media.get("full_url") or media.get("encrypt_query_param")):
        return False
    if kind == "video" and _inbound_media_key(media) is None:
        return False
    return True


def _schedule_inbound_refetch(mid, kind, cred, log=serve_log):
    """首次下载失败后登记一次延迟补取。队列落盘 INBOUND_RETRY_FILE，
    同 id+kind 只登记一次；cred 只随队列内部文件留存，不进日志。"""
    if not _inbound_refetchable(cred, kind):
        return False
    with state_lock():
        # 顶层 dict 包装：read_json 只认对象顶层（全桥存储口径）
        queue = (read_json(INBOUND_RETRY_FILE, {}) or {}).get("entries") or []
        if any(e.get("message_id") == str(mid) and e.get("kind") == kind
               for e in queue if isinstance(e, dict)):
            return False
        queue.append({"message_id": str(mid), "kind": kind, "cred": cred,
                      "due_at": time.time() + _INBOUND_REFETCH_DELAY_SEC,
                      "scheduled_at": time.time()})
        atomic_write_json(INBOUND_RETRY_FILE, {"entries": queue})
    log(f"入站{kind}首次下载失败，已登记延迟补取 id={mid}")
    return True


def _apply_inbound_refetch(mid, kind, media_files, video_file, frames,
                           vinfo, log):
    """补取成功回填：原 inbox 记录补媒体字段备查；另写一条
    media_refetch 提示记录（原轮次可能已回复，这条让 worker 有机会
    补看补评）并触发 worker；history 同步补记。"""
    note = {"message_id": mid + "-refetch", "ref_message_id": mid,
            "type": "media_refetch", "text": "", "received_at": time.time()}
    hist = {"ts": time.time(), "role": "user", "message_id": mid,
            "type": "media_refetch"}
    media_file = media_files[0] if media_files else None
    for key, val in (("media_file", media_file), ("media_files", media_files),
                     ("video_file", video_file), ("video_frames", frames)):
        if val:
            note[key] = val
            hist[key] = val
    if vinfo:
        for key, val in (("video_meta", vinfo.get("meta")),
                         ("video_transcript", vinfo.get("transcript")),
                         ("video_transcript_status",
                          vinfo.get("transcript_status"))):
            if val:
                note[key] = val
                hist[key] = val
    path = os.path.join(INBOX, mid + ".json")
    rec = read_json(path, None)
    if isinstance(rec, dict):
        for key in ("media_file", "media_files", "video_file", "video_frames",
                    "video_meta", "video_transcript", "video_transcript_status"):
            if key in note:
                rec[key] = note[key]
        rec["refetched_at"] = time.time()
        write_json_locked(path, rec)
        note["from_user_id"] = rec.get("from_user_id")
        note["context_token"] = rec.get("context_token")
        note["create_time_ms"] = rec.get("create_time_ms")
    write_json_locked(os.path.join(INBOX, mid + "-refetch.json"), note)
    append_jsonl(HISTORY_FILE, hist)
    log(f"入站补取成功 id={mid} kind={kind} file={media_file or video_file}")
    touch_trigger_and_spawn_worker()


def _inbound_refetch_sweep(log=serve_log):
    """延迟补取清扫（节流 15s）：到期条目按 cred 重建媒体消息重走
    下载函数，一次为限。条目先出队再尝试——崩溃/失败都不重复补取；
    放弃时留日志痕迹可查。"""
    now = time.time()
    if now - _INBOUND_REFETCH_LAST[0] < 15:
        return
    _INBOUND_REFETCH_LAST[0] = now
    queue = (read_json(INBOUND_RETRY_FILE, {}) or {}).get("entries") or []
    due = [e for e in queue if isinstance(e, dict)
           and float(e.get("due_at") or 0) <= now]
    if not due:
        return
    rest = [e for e in queue if isinstance(e, dict)
            and float(e.get("due_at") or 0) > now]
    with state_lock():
        atomic_write_json(INBOUND_RETRY_FILE, {"entries": rest})
    for e in due:
        mid, kind, cred = str(e.get("message_id")), e.get("kind"), e.get("cred") or {}
        media_files = []
        video_file = frames = vinfo = None
        try:
            if kind == "image":
                # 多图口径：cred.items 逐图重建整条消息；旧队列条目
                # 只有顶层 aeskey/media 时按单图重建
                item_creds = cred.get("items") or [
                    {"aeskey": cred.get("aeskey"),
                     "media": cred.get("media") or {}}]
                item_list = [{"type": 2, "image_item": {
                    "aeskey": c.get("aeskey"), "media": c.get("media") or {}}}
                    for c in item_creds if isinstance(c, dict)]
                media_files, _ = _fetch_inbound_images(
                    {"message_id": mid, "item_list": item_list}, log=log)
            elif kind == "video":
                item = {"type": 5, "video_item": {"media": cred.get("media") or {}}}
                video_file, frames, _, vinfo = _fetch_inbound_video(
                    {"message_id": mid, "item_list": [item]}, log=log)
        except Exception as exc:
            log(f"入站补取异常 id={mid} kind={kind}：{type(exc).__name__}")
            continue
        if not (media_files or video_file):
            log(f"入站补取失败（一次为限，放弃）id={mid} kind={kind}")
            continue
        _apply_inbound_refetch(mid, kind, media_files, video_file, frames,
                               vinfo, log)


def _process_inbound(session, msg, my_id, state=None):
    """入站单条处理。返回值：True＝本条已耐久落盘或本就不需落盘
    （过滤/撤回/去重命中），接收游标可推进；False＝落盘因存储类
    异常（OSError，如 ENOSPC）失败——游标必须保持不推进、等平台
    按旧游标重投整批，已落盘条目靠 message_id 去重吸收。非存储类
    异常照旧上抛，由接收循环的单条隔离处理（内容毒型跳过本条、
    游标照常推进，防同批反复重投的死循环）。"""
    if msg.get("message_id") is None:
        return True
    mid = str(msg["message_id"])
    if _handle_recall(msg):
        return True
    # 过滤：message_type==1、本人发送、无 group_id，三条都满足才收
    if msg.get("message_type") != 1:
        return True
    if msg.get("group_id"):
        return True
    if my_id and msg.get("from_user_id") != my_id:
        return True
    path = os.path.join(INBOX, mid + ".json")
    if os.path.exists(path):
        return True  # 按 message_id 去重
    # 并发去重 claim（压测实证）：原 exists 检查与下方落盘之间隔着
    # 分类/媒体下载长窗口，多线程/多进程并发重投同一 message_id 时
    # 全部穿过检查、history 重复落账。改用 O_EXCL 原子 claim 文件
    # 占位（*.claim 不入 worker/status 的 *.json 扫描）：抢不到
    # claim 即已有同 id 在处理，直接返回；处理结束（成功落盘后由
    # exists 接力去重、异常时允许重投）在 finally 中释放 claim。
    claim_path = os.path.join(INBOX, mid + ".claim")
    claimed = False
    try:
        _cfd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(_cfd)
        claimed = True
    except FileExistsError:
        return True  # 同 id 已在处理：由对方的结果负责，本条不重复落盘
    except OSError:
        pass  # claim 落盘失败（只读 fs 等）：退回 exists-only 旧口径，不阻断收件
    try:
        _process_inbound_body(session, msg, my_id, state, mid, path)
    except OSError:
        # 落盘链存储类失败（ENOSPC 等）：消息未耐久落盘，回报接收
        # 循环保持游标、等平台重投；claim 在 finally 释放，重投可
        # 重新领取本条。
        return False
    finally:
        if claimed:
            try:
                os.remove(claim_path)
            except OSError:
                pass
    return True


def _process_inbound_body(session, msg, my_id, state, mid, path):
    typ, text, ref_text = _classify(msg, serve_log)
    probe_inbound(msg)  # 入站媒体探针（第 0 步）：只记形态不记值，失败不影响主流程
    # 平台字段归一：context_token/from_user_id 同样清洗孤立代理对字符
    # （写 context.json / inbox 记录时同样会撞 UTF-8 编码错误）；时间戳
    # 类型不可靠，转不动就以到达时间兜底（bughunt-2026-10-24 面 4）
    from_uid = _clean_text(msg.get("from_user_id") or "")
    ctx_tok = _clean_text(msg.get("context_token") or "")
    ctm = _coerce_ms(msg.get("create_time_ms"))
    if ctm is None:
        ctm = int(time.time() * 1000)
    # 阶段六：导演指令（# 开头）分流——不进 inbox/history/聊天流程，交给 tune 处理
    prefix = "#"
    try:
        prefix = str(cfg().get("tune", {}).get("cmd_prefix", "#") or "#")
    except Exception:
        pass
    if typ == "text" and is_director_command(text, prefix):
        drec = {"message_id": mid, "from_user_id": from_uid,
                "context_token": ctx_tok,
                "create_time_ms": ctm, "type": "director",
                "text": text, "received_at": time.time()}
        write_json_locked(os.path.join(DIRECTOR, mid + ".json"), drec)
        append_jsonl(TUNE_LOG_FILE, {"ts": time.time(), "kind": "cmd_received",
                                     "message_id": mid, "text": text[:80]})
        if drec["context_token"]:
            write_context(drec["context_token"])
        set_unreplied(0)
        serve_log(f"导演指令已分流 id={mid} text={text[:40]!r}")
        touch_trigger_and_spawn_worker()
        return
    # 入站图片：同步下载解密落盘（多图全收）；失败降级为无 media_file，
    # 流程不变。media_file 保留首图旧口径，media_files 为全部图片。
    media_files, media_cred = _fetch_inbound_images(msg)
    media_file = media_files[0] if media_files else None
    if not media_file and media_cred:
        # 首次失败且 cred 可用 → 登记一次延迟补取
        _schedule_inbound_refetch(mid, "image", media_cred)
    # 入站视频：同步下载解密落盘 + 元信息 + 抽帧 + 语音转写（异步）；
    # 失败降级为无 video_file/video_frames
    video_file, video_frames, video_cred, video_info = _fetch_inbound_video(msg)
    if not video_file and video_cred:
        _schedule_inbound_refetch(mid, "video", video_cred)
    rec = {"message_id": mid, "from_user_id": from_uid,
           "context_token": ctx_tok,
           "create_time_ms": ctm, "type": typ, "text": text,
           "ref_text": ref_text, "recalled": bool(msg.get("delete_time_ms"))}
    if media_file:
        rec["media_file"] = media_file
    if media_files:
        rec["media_files"] = media_files
    if media_cred:
        rec["media_cred"] = media_cred  # 凭证只随 inbox 内部记录留存，不进 history/日志
    if video_file:
        rec["video_file"] = video_file
    if video_frames:
        rec["video_frames"] = video_frames
    if video_info:
        rec["video_meta"] = video_info.get("meta") or {}
        rec["video_transcript"] = video_info.get("transcript") or ""
        rec["video_transcript_status"] = video_info.get("transcript_status") or ""
    if video_cred:
        rec["video_cred"] = video_cred  # 同 media_cred 口径：只随 inbox 记录留存
    write_json_locked(path, rec)
    # 每收到一条的固定动作：更新 context、记 history、清未回复计数、清 awaiting_reply、触发 worker
    if rec["context_token"]:
        write_context(rec["context_token"])
    hist_rec = {"ts": time.time(), "role": "user", "message_id": mid,
                "type": typ, "text": text}
    if media_file:
        hist_rec["media_file"] = media_file
    if media_files:
        hist_rec["media_files"] = media_files
    if video_file:
        hist_rec["video_file"] = video_file
    if video_frames:
        hist_rec["video_frames"] = video_frames
    if video_info:
        hist_rec["video_meta"] = video_info.get("meta") or {}
        hist_rec["video_transcript"] = video_info.get("transcript") or ""
        hist_rec["video_transcript_status"] = \
            video_info.get("transcript_status") or ""
    append_jsonl(HISTORY_FILE, hist_rec)
    set_unreplied(0)
    with state_lock():
        pj = read_json(PROACTIVE_FILE, None)
        if pj is not None and pj.get("awaiting_reply"):
            pj["awaiting_reply"] = False
            atomic_write_json(PROACTIVE_FILE, pj)
    serve_log(f"inbox 收到 [{typ}] id={mid} ctx={mask_secret(rec['context_token'])} text={text[:60]!r}")
    # 提速：热聊中（她 5 分钟内刚发过）立刻回一个「正在输入」，让他感知已读在回
    try:
        last_send = float((state or {}).get("last_send_ts", 0) or 0)
        if last_send and time.time() - last_send <= 300 and rec["context_token"]:
            threading.Thread(
                target=send_typing,
                args=(session, rec["from_user_id"], 1, rec["context_token"]),
                kwargs={"log": serve_log}, daemon=True).start()
    except Exception:
        pass
    touch_trigger_and_spawn_worker()


DISK_FREE_WARN_BYTES = 500 * 1024 * 1024  # state 所在盘可用低于此值告警


def disk_space_low(path, warn_bytes=DISK_FREE_WARN_BYTES):
    """磁盘余量监测（纯查询，供自检）：返回 (是否偏低, 可用字节)；
    查询失败返回 (False, None)——监测本身不许反噬主流程。"""
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return False, None
    return free < warn_bytes, free


def _check_disk_space():
    try:
        low, free = disk_space_low(STATE)
        if low:
            serve_log(f"磁盘余量告警：state 所在盘可用仅 {free / 1048576:.0f}MB"
                      f"（阈值 {DISK_FREE_WARN_BYTES // 1048576}MB），"
                      f"落盘失败风险上升（ENOSPC 时游标保持不推进、等平台重投）")
    except Exception:
        pass


def _prune_history():
    try:
        keep_days = float(cfg()["history_keep_days"])
    except Exception:
        keep_days = 30
    cutoff = time.time() - keep_days * 86400
    rows = read_jsonl(HISTORY_FILE)

    def _ts(r):
        try:
            return float(r.get("ts", 0))
        except (TypeError, ValueError):
            return float("inf")  # ts 损坏判不了新旧：保留不删（防误删数据）
    kept = [r for r in rows if _ts(r) >= cutoff]
    if len(kept) != len(rows):
        with state_lock():
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                # fsync 对齐 atomic_write_json：原实现写完直接 replace，
                # 断电时已修剪内容可能未落盘。
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, HISTORY_FILE)
            _fsync_dir(os.path.dirname(HISTORY_FILE))


# ---------------- serve：发送线程 ----------------

# 媒体条超时/瞬时传输失败的 outbox 层重试退避（秒）：共多试 2 次（总 3 次）。
# 媒体发送要先经代理整包上传，一次网络坏窗口不应直接判死；重试沿用文件里
# 已落盘的 client_id，微信侧按 client_id 幂等，不会重复投递。文本条不走这里。
MEDIA_RETRY_BACKOFFS = (60, 180)

# CDN 5xx 专用长退避（秒）：多试 3 次（总 4 次），总窗约 25 分钟。CDN/出口
# 侧 5xx 故障窗以分钟计时，(60, 180) 的约 4 分钟窗太短（2026-10-24 深审
# A1）；与其余瞬时类（超时/连接）区分阶梯，档位耗尽才判死进 failed/。
MEDIA_RETRY_BACKOFFS_5XX = (60, 300, 900)


def _media_retry_delay(data, exc):
    """媒体条本次失败是否应留在 pending 稍后重试（纯函数，供自检）：
    返回退避秒数 = 重试；None = 不重试（按原逻辑移入 failed）。
    仅限带 media_file 的条目 + 超时/瞬时传输类异常 + 未用完重试次数。
    CDN 5xx（CdnServerError）走专用长退避阶梯，其余瞬时类走短阶梯。"""
    if not data.get("media_file"):
        return None
    if not _is_transient_exc(exc):
        return None
    ladder = MEDIA_RETRY_BACKOFFS_5XX if isinstance(exc, (CdnServerError, HttpServerError)) \
        else MEDIA_RETRY_BACKOFFS
    attempts = int(data.get("send_attempts") or 0)
    if attempts >= len(ladder):
        return None
    return ladder[attempts]


# 主动媒体改绑挽回的窗口：距原 send_after 不超过 2 小时
PROACTIVE_MEDIA_RESCUE_WINDOW_SEC = 2 * 3600


def proactive_media_rescue(data, ctx, now):
    """主动媒体条「改绑最新 context 挽回续发」的判定（纯函数，供自检）：
    True = 本条可改绑续发一次。条件全部满足才挽回（收窄口径）：
    kind=proactive 且带 media_file；已有 ≥1 次传输类重试记录
    （send_attempts ≥ 1，未重试过的照常按复查作废）；距原 send_after
    （orig_send_after，缺失时用当前 send_after）不超过 2 小时；未挽回过
    （rescued 标记）；ctx 里有与本条不同的新 token。文字条永不走这里。"""
    if data.get("kind") != "proactive" or not data.get("media_file"):
        return False
    if data.get("rescued"):
        return False
    if int(data.get("send_attempts") or 0) < 1:
        return False
    new_tok = (ctx or {}).get("context_token")
    if not new_tok or new_tok == data.get("context_token"):
        return False
    base = float(data.get("orig_send_after") or data.get("send_after") or 0)
    if not base or now - base > PROACTIVE_MEDIA_RESCUE_WINDOW_SEC:
        return False
    return True


def _proactive_failure_marks_stale(data, exc, code):
    """主动条目发送判死后是否标 context stale + 降窗（纯函数，供自检；
    深审 A3）：传输类失败（超时/连接/CDN 5xx）与 context 新鲜度无关，
    媒体条撞上这类故障不得误标 stale、误触降窗——故障的唯一后果应是
    本条迟到或判死。-14（登录过期）历来不标；其余业务性失败（token
    失效类等语义错误）保持原行为。文本条不适用传输豁免（口径不变）。"""
    if data.get("kind") != "proactive" or code == -14:
        return False
    if data.get("media_file") and _is_transient_exc(exc):
        return False
    return True


# reply 组 token 失效整组止损：一条回复发送时若平台以业务错误明确
# 拒绝（context/token 失效类），同组（同 message_id）其余条目共用同一
# token——逐条再试只是逐条撞同一堵墙、逐条刷失败噪声，还可能把同组
# 后半段拖过 context 新鲜期。判定保守口径：-3 是生产健康日志里实测
# 过的发送业务码，列入已知码集；其余业务码须 errmsg 命中 context/
# token 失效语义才触发，未知业务码保持逐条处置原口径。-14 走登录
# 过期通路（整趟已 break），不在此列；瞬时类与 CDN/Http 5xx 一律不
# 触发（那是传输问题、不是 token 失效）。
_CONTEXT_INVALID_ERRCODES = frozenset({-3})
_CONTEXT_INVALID_ERRMSG_MARKERS = ("context", "token", "失效", "过期",
                                     "invalid", "expired")


def _is_context_invalid_failure(exc, code):
    """token/context 失效类失败判定（纯函数，供自检）。"""
    if code is None or code in (0, -14):
        return False
    if _is_transient_exc(exc):
        return False
    if isinstance(exc, (CdnServerError, HttpServerError)):
        return False
    if code in _CONTEXT_INVALID_ERRCODES:
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _CONTEXT_INVALID_ERRMSG_MARKERS)


def _abort_group_pending(group_key, code, exclude_fn=None):
    """同组止损：把 pending 里同 message_id 的其余条目一并移入
    failed（理由：同组回复 token 失效，整组止损），返回移动条数。
    语义判死口径（error 文案不含瞬时标记），清道夫永不复活；并发
    下条目已被他线程移走时跳过不抛错。"""
    try:
        names = sorted(f for f in os.listdir(PENDING) if f.endswith(".json"))
    except OSError:
        return 0
    moved = 0
    for fn in names:
        if exclude_fn is not None and fn == exclude_fn:
            continue
        p = os.path.join(PENDING, fn)
        d = read_json(p, None)
        if not isinstance(d, dict) or str(d.get("message_id")) != group_key:
            continue
        try:
            _move_with_error(p, d, "同组回复 token 失效，整组止损", errcode=code)
            moved += 1
        except OSError:
            continue
    return moved


# ---------------- failed/ 清道夫（深审 A2） ----------------
# failed/ 原是终点站：判死即永久丢失，只能靠人看到看门狗通知后手动补发。
# 清道夫把「瞬时类失败」的媒体条在窗口内自动重排回 pending 续发：错误
# 类别按落盘 error 文案白名单判定（与 _is_transient_exc 的异常类口径对
# 齐，并兼容 A1 之前以 RuntimeError 落盘的 CDN 500 记录）；语义性判死
# （已有新对话/被新消息打断/字段不完整/-14 等设计决定）永不复活。
FAILED_RESCUE_WINDOW_SEC = 24 * 3600  # failed_at 距今 24 小时内才重排（总窗 24h）
FAILED_RESCUE_MAX = 3                 # 每条最多自动重排次数（防坏文件循环）
FAILED_SCAVENGE_INTERVAL_SEC = 300   # 媒体发送线程内的调用节流
# 服务端 5xx 的重排间隔（按已重排次数索引）：首救仍随清道夫下一趟，
# 第二救等 30 分钟、第三救等 2 小时，总覆盖约 2.5 小时。旧口径每
# 300 秒一趟会把 3 次重排在约 10 分钟内烧完，恰与 CDN 故障窗的常见
# 时长（约 40 分钟至 2 小时）错位，把瞬时故障人为转成必须人工捞的
# 终局。连接类瞬时错误不加此延迟（见 _failed_rescue_delay_sec）。
FAILED_RESCUE_SERVER_DELAYS_SEC = (0, 1800, 7200)
_SERVER_ERROR_MARKERS = ("CdnServerError", "CDN 上传失败 HTTP 5",
                         "HttpServerError", "HTTP 5")

_TRANSIENT_ERROR_MARKERS = (
    "CdnServerError", "CDN 上传失败 HTTP 5", "HttpServerError",
    "TimeoutError", "timed out", "Timeout",
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "BrokenPipeError", "RemoteDisconnected", "URLError", "HTTPException",
    "ChunkedEncodingError", "SSLError", "NewConnectionError", "MaxRetryError",
)


def _entry_value_bad(data):
    """outbox 条目字段「值」校验（纯函数，供自检）：need 系列只验字段在
    不在，本函数验值能不能用——send_after/typing_sec 须可数值化（毒成
    字符串时发送趟 float() 穿透抛错、短周期一趟无限重试，且按文件名排
    在前时堵死其后全部正常条目）、text 须为字符串、media_file 若有须
    为字符串。返回损坏字段名，合格返回 None。"""
    for k in ("send_after", "typing_sec"):
        v = data.get(k)
        if v is None:
            continue
        try:
            float(v)
        except (TypeError, ValueError):
            return k
    if not isinstance(data.get("text"), str):
        return "text"
    mf = data.get("media_file")
    if mf is not None and not isinstance(mf, str):
        return "media_file"
    return None


def _failed_rescue_delay_sec(err, rescues):
    """服务端 5xx 条目在第 n 次重排前须等待的秒数（纯函数，供自检）。
    非服务端瞬时错误（连接重置/超时等）不额外等待，维持旧口径。"""
    if not any(m in err for m in _SERVER_ERROR_MARKERS):
        return 0
    delays = FAILED_RESCUE_SERVER_DELAYS_SEC
    return delays[min(max(int(rescues), 0), len(delays) - 1)]


def failed_entry_rescuable(data, now):
    """failed/ 条目是否可自动重排回 pending（纯函数，供自检）：错误属
    瞬时类白名单 + failed_at 在窗口内 + 重排次数未超上限 + 服务端
    5xx 已达到本轮重排间隔（见 FAILED_RESCUE_SERVER_DELAYS_SEC）。
    媒体条须带 media_file；纯文本条另须字段完整（与 _sender_pass 的
    need 口径一致）——此前文本条不在可救范围，sendmessage 撞上一次
    瞬时传输故障（http 层重试已耗尽）即永久丢回复，与媒体条待遇断层。
    -14 与语义判死由白名单/errcode 排除。"""
    if not isinstance(data, dict):
        return False
    if data.get("media_file"):
        # 媒体条也须过定向值校验：media_file 须为字符串、send_after/
        # typing_sec 若在须可数值化、text 若在须为字符串——毒值条目
        # 会被救回 pending 再被发送趟判死或堵队头，白绕一趟。不套
        # _entry_value_bad 全量口径：它要求 text 必为字符串，而媒体条
        # text 只是占位、缺省不构成不可救（最小媒体条口径）。
        if not isinstance(data.get("media_file"), str):
            return False
        for _k in ("send_after", "typing_sec"):
            _v = data.get(_k)
            if _v is None:
                continue
            try:
                float(_v)
            except (TypeError, ValueError):
                return False
        _t = data.get("text")
        if _t is not None and not isinstance(_t, str):
            return False
    else:
        need = ("to_user_id", "text", "context_token", "message_id",
                "send_after", "kind", "typing_sec")
        if any(k not in data for k in need) \
                or data.get("kind") not in ("reply", "proactive", "later", "tune"):
            return False
    if data.get("errcode") == -14:
        return False
    err = str(data.get("error") or "")
    if not any(m in err for m in _TRANSIENT_ERROR_MARKERS):
        return False
    try:
        failed_at = float(data.get("failed_at") or 0)
        rescues = int(data.get("rescues") or 0)
    except (TypeError, ValueError):
        return False  # 时间/计数被毒成不可转类型：判不可救，不抛错丢整轮
    if not failed_at or now - failed_at > FAILED_RESCUE_WINDOW_SEC:
        return False
    if rescues >= FAILED_RESCUE_MAX:
        return False
    if now - failed_at < _failed_rescue_delay_sec(err, rescues):
        return False
    return True


_FAILED_SCAVENGE_LAST = [0.0]


def _failed_scavenge_pass(now=None):
    """failed/ 清道夫一轮：把可重排条目（媒体条与字段完整的文本条）移回
    pending（原 client_id 不变，微信侧幂等）。同一 message_id 有多份可重排时只救 failed_at
    最新的一份——原版与人工重发版同躺 failed/ 时避免双份重投。
    pending 已有同名文件时跳过（那是更新的待发副本，以它为准）。"""
    now = time.time() if now is None else now
    try:
        names = sorted(f for f in os.listdir(FAILED) if f.endswith(".json"))
    except OSError:
        return 0
    candidates = {}
    for fn in names:
        data = read_json(os.path.join(FAILED, fn), None)
        if not failed_entry_rescuable(data, now):
            continue
        if data.get("media_file") and not os.path.exists(str(data.get("media_file"))):
            continue
        key = str(data.get("message_id"))
        prev = candidates.get(key)
        if prev is None or float(data.get("failed_at") or 0) > float(prev[1].get("failed_at") or 0):
            candidates[key] = (fn, data)
    moved = 0
    for fn, data in candidates.values():
        src = os.path.join(FAILED, fn)
        dst = os.path.join(PENDING, fn)
        if os.path.exists(dst):
            continue
        data = dict(data)
        data["rescues"] = int(data.get("rescues") or 0) + 1
        for k in ("error", "errcode", "failed_at"):
            data.pop(k, None)
        data["send_after"] = now
        write_json_locked(src, data)
        try:
            # 无覆盖移动：exists 检查与 replace 之间存在 TOCTOU 窗口——
            # 生成方恰在此时新建同名 pending（同 message_id 的补发条）时
            # os.replace 会把新副本静默覆盖成旧判死副本（bughunt2 竞态
            # 加固）。os.link 在目标已存在时抛 FileExistsError，天然无覆盖，
            # 同文件系统内与 replace 等效。
            os.link(src, dst)
            os.remove(src)
        except FileExistsError:
            continue
        except OSError:
            try:
                os.replace(src, dst)  # link 不可用（跨设备等）时退回原口径
            except OSError:
                continue
        moved += 1
        serve_log(f"failed 清道夫重排 {fn} 回 pending（第 {data['rescues']} 次，"
                  f"原 client_id 续发）")
    return moved


def _move_with_error(src, data, err_text, errcode=None):
    data = dict(data)
    data["error"] = err_text
    if errcode is not None:
        data["errcode"] = errcode
    data["failed_at"] = time.time()
    dst = os.path.join(FAILED, os.path.basename(src))
    write_json_locked(src, data)
    os.replace(src, dst)
    return dst


def _sender(session, state):
    user_id = session.get("ilink_user_id")
    gap_cfg = cfg()["bubble_gap_sec"]
    while not _stop.is_set():
        try:
            _sender_pass(session, user_id, gap_cfg, state, skip_media=True)
        except Exception as e:
            serve_log(f"发送线程异常：{type(e).__name__}: {e}")
        _stop.wait(0.5)


# ---------------- serve 侧数值硬门（台账 #38，第八轮落地） ----------------
# 口径 = 生成侧数值锚点（回复轮次，与 worker.py 自检一致）：气泡 ≤3/轮、单条 ≤90 字、
# 整轮 ≤300 字、问号 ≤2、emoji ≤2、方括号表情 ≤2/轮且近 5 轮合计 ≤4、
# 媒体 ≤1/轮（以上为默认值；实际上限优先读 config.limits，见 _gate_limits）。
# 计数口径与生成侧一致（worker.py 的 _EMOJI_RE /
# _BRACKET_EMOJI_RE 与占位符豁免）。
# 豁免范围（当年暂缓待拍板项，本轮定版）：只管 kind="reply" 轮次；
# proactive 有自己的契约（生成侧与 decide 层把关）、tune 是系统消息、
# later 从不直接发送，均不在本门内。
# 处置：整组作废——组内 pending 条目全部移 failed/（error 记违规规则、
# failed_at 留痕）+ serve.log 一行 + HEALTH 一条 numeric_gate；不部分
# 发送（轮次语义会破碎）、不静默吞（failed/ 与看门狗告警可查）。已
# 发出的条目不可召回，故判定取 pending∪sent 全组口径，两个发送线程
# 在任何时点评估结论一致。

_GATE_MAX_BUBBLES = 3
_GATE_MAX_ITEM_CHARS = 90
_GATE_MAX_TOTAL_CHARS = 300
_GATE_MAX_QUESTIONS = 2
_GATE_MAX_EMOJI = 2
_GATE_MAX_BRACKET = 2
_GATE_BRACKET_WINDOW = 5
_GATE_BRACKET_WINDOW_MAX = 4
_GATE_MAX_MEDIA = 1

# 硬门上限优先读 config.json 的 limits 段（键名与 config.example.json 的
# limits 段对齐，见 docs/configuration.md）；缺失/非法时回落上面的常量
# 默认。读 config 失败（如配置损坏）同样回落默认——硬门是兜底护栏，
# 不能反过来成为崩溃点。
_GATE_LIMIT_KEYS = {
    "max_bubbles": ("max_bubbles", _GATE_MAX_BUBBLES),
    "max_item_chars": ("max_chars_per_bubble", _GATE_MAX_ITEM_CHARS),
    "max_total_chars": ("max_chars_per_reply", _GATE_MAX_TOTAL_CHARS),
    "max_questions": ("max_question_marks", _GATE_MAX_QUESTIONS),
    "max_emoji": ("max_emoji", _GATE_MAX_EMOJI),
    "max_bracket": ("max_bracket_expressions", _GATE_MAX_BRACKET),
    "bracket_window": ("bracket_window_rounds", _GATE_BRACKET_WINDOW),
    "bracket_window_max": ("max_bracket_in_window", _GATE_BRACKET_WINDOW_MAX),
}


def _parse_intent_overrides(raw):
    """limits.intent_overrides 解析：{intent 名: {max_bubbles,
    max_chars_per_bubble, max_chars_per_reply}}——部署者按 intent
    自定的格式豁免，字段可缺省（缺省项回落常态上限）。非法结构
    一律忽略；缺失/空表＝无任何豁免，行为与未引入本机制时一致。
    worker 侧的 check/score/build_prompt/write_outbox 与本门共用
    同一配置（worker 另读其中的 mirror_exempt / flat_len_score
    两项生成侧开关，本门不消费）。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for intent, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        entry = {}
        for name, key in (("max_bubbles", "max_bubbles"),
                          ("max_item_chars", "max_chars_per_bubble"),
                          ("max_total_chars", "max_chars_per_reply")):
            v = spec.get(key)
            if v is None:
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v >= 0:
                entry[name] = v
        out[str(intent)] = entry
    return out


def _gate_limits():
    """当前生效的硬门上限 dict（config.limits 优先、常量兜底）。

    特殊键（不走上面的整数表）：
    - max_media_per_reply：null＝每轮媒体条数不单独设上限（只受气泡
      以外的总量语义约束）；填整数恢复上限；缺失回落常量默认。
    - intent_overrides：部署者按 intent 自定的格式豁免（见
      _parse_intent_overrides）——组内任一条目的 intent 命中时，
      气泡/单条/整轮三项改用该 intent 的覆盖值；其余卫生项
      （问号/emoji/方括号）不放宽。"""
    try:
        lim = cfg().get("limits") or {}
    except Exception:
        lim = {}
    if not isinstance(lim, dict):
        lim = {}
    out = {}
    for name, (key, default) in _GATE_LIMIT_KEYS.items():
        try:
            v = int(lim.get(key, default))
        except (TypeError, ValueError):
            v = default
        out[name] = v if v >= 0 else default
    raw_media = lim.get("max_media_per_reply", _GATE_MAX_MEDIA)
    if raw_media is None:
        out["max_media"] = None
    else:
        try:
            v = int(raw_media)
        except (TypeError, ValueError):
            v = _GATE_MAX_MEDIA
        out["max_media"] = v if v >= 0 else _GATE_MAX_MEDIA
    out["intent_overrides"] = _parse_intent_overrides(
        lim.get("intent_overrides"))
    return out
_GATE_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_GATE_BRACKET_RE = re.compile(r"\[[^\[\]\s]{1,6}\]")
_GATE_BRACKET_PLACEHOLDERS = {"[图片]", "[视频]", "[语音]", "[文件]"}


def _gate_bracket_count(text):
    return sum(1 for m in _GATE_BRACKET_RE.findall(text or "")
               if m not in _GATE_BRACKET_PLACEHOLDERS)


def _gate_recent_brackets(history_records, exclude_mid=None,
                          window=_GATE_BRACKET_WINDOW - 1):
    """最近 window 个已完成 reply 轮次（按 message_id 分组）的方括号
    表情合计，喂近 5 轮滚动口径。exclude_mid 排除本组（其已发送条目
    已进 history、且会经 items 再计一次，防双计）。"""
    rounds = {}
    order = []
    for r in history_records:
        if r.get("role") != "assistant" or r.get("kind") != "reply":
            continue
        mid = str(r.get("message_id"))
        if exclude_mid is not None and mid == str(exclude_mid):
            continue
        if mid not in rounds:
            rounds[mid] = 0
            order.append(mid)
        rounds[mid] += _gate_bracket_count(r.get("text"))
    if window <= 0:
        return 0
    return sum(rounds[m] for m in order[-window:])


def numeric_gate_violation(items, recent_brackets=0):
    """一组 reply 条目的硬门判定：合规返回 None，违规返回规则描述
    （留痕用）。字数计 text 字段（媒体条的占位说明同样计入，与生成
    侧整轮口径一致）；问号计半角 ? 与全角 ？。"""
    lim = _gate_limits()
    # intent 格式豁免（部署者自定）：组内任一条目 intent 命中
    # intent_overrides 即整组三项（气泡/单条/整轮）改用该 intent
    # 的覆盖上限，卫生项不放宽。intent 由生成层随 outbox 条目落盘
    # （见 worker.write_outbox 的 intent 参数与 docs/agent-mode.md）。
    override = None
    for d in items:
        ov = lim["intent_overrides"].get(str(d.get("intent") or ""))
        if ov is not None:
            override = ov
            break
    max_bubbles = lim["max_bubbles"]
    max_item_chars = lim["max_item_chars"]
    max_total_chars = lim["max_total_chars"]
    if override:
        max_bubbles = override.get("max_bubbles", max_bubbles)
        max_item_chars = override.get("max_item_chars", max_item_chars)
        max_total_chars = override.get("max_total_chars", max_total_chars)
    # 气泡数只计非媒体条：媒体条是附件不是气泡，占位 text 仍计入
    # 单条/整轮字数与卫生项（与生成侧整轮口径一致）。
    bubbles = sum(1 for d in items if not d.get("media_file"))
    if bubbles > max_bubbles:
        return f"气泡数 {bubbles}>{max_bubbles}"
    if lim["max_media"] is not None:
        media_n = sum(1 for d in items if d.get("media_file"))
        if media_n > lim["max_media"]:
            return f"媒体数 {media_n}>{lim['max_media']}"
    total = 0
    questions = 0
    emojis = 0
    brackets = 0
    for d in items:
        text = str(d.get("text") or "")
        if len(text) > max_item_chars:
            return f"单条字数 {len(text)}>{max_item_chars}"
        total += len(text)
        questions += text.count("?") + text.count("？")
        emojis += len(_GATE_EMOJI_RE.findall(text))
        brackets += _gate_bracket_count(text)
    if total > max_total_chars:
        return f"整轮字数 {total}>{max_total_chars}"
    if questions > lim["max_questions"]:
        return f"问号数 {questions}>{lim['max_questions']}"
    if emojis > lim["max_emoji"]:
        return f"emoji 数 {emojis}>{lim['max_emoji']}"
    if brackets > lim["max_bracket"]:
        return f"方括号表情 {brackets}>{lim['max_bracket']}"
    if recent_brackets + brackets > lim["bracket_window_max"]:
        return (f"方括号表情近{lim['bracket_window']}轮合计 "
                f"{recent_brackets + brackets}>{lim['bracket_window_max']}")
    return None


def _numeric_gate_group_items(group_key):
    """pending ∪ sent 中某 message_id 的全部条目（发送进行时条目会
    陆续 pending→sent，合并两处才是整组口径）。

    同组以文件名缀 <message_id>- 界定（与 _void_group_media_and_siblings
    的兄弟解析同口径，outbox 文件名恒为 <message_id>-<part>.json）：
    先按名过滤再读，避免全量读盘把发送趟拖成 O(n²)（pressure P5）。"""
    prefix = group_key + "-"
    items = []
    for d in (PENDING, SENT):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if fn.startswith(prefix) and fn.endswith(".json"):
                data = read_json(os.path.join(d, fn), None)
                if isinstance(data, dict):
                    items.append(data)
    return items


def _numeric_gate_void_group(group_key, violation, log=serve_log):
    """整组作废：该组尚在 pending 的 reply 条目全部移 failed/ 留痕，
    返回作废条数；已 sent 的不动（不可召回）。"""
    moved = 0
    try:
        names = sorted(os.listdir(PENDING))
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PENDING, fn)
        data = read_json(path, None)
        if isinstance(data, dict) and str(data.get("message_id")) == group_key \
                and data.get("kind") == "reply":
            _move_with_error(path, data, f"数值硬门拦截：{violation}")
            moved += 1
    if moved:
        log(f"数值硬门拦截整组作废 id={group_key} 规则={violation} 条数={moved}")
        append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": "numeric_gate",
                                   "ok": False, "rule": violation,
                                   "message_id": group_key, "voided": moved})
    return moved


def _numeric_gate_blocks(group_key, log=serve_log):
    """评估一组 reply 并按需作废：合规或组内无 reply 条目返回 False；
    违规时整组作废并返回 True（调用方跳过本条）。"""
    items = _numeric_gate_group_items(group_key)
    reply_items = [d for d in items if d.get("kind") == "reply"]
    if not reply_items:
        return False
    recent = _gate_recent_brackets(
        read_jsonl(HISTORY_FILE), exclude_mid=group_key,
        window=max(0, _gate_limits()["bracket_window"] - 1))
    violation = numeric_gate_violation(reply_items, recent)
    if not violation:
        return False
    _numeric_gate_void_group(group_key, violation, log=log)
    return True


def later_action(data, ctx_received_at, now, unreplied, stop_at):
    """两段式回复的 delayed 任务到期判断（纯函数，供自检）：
    wait=还没到时间；cancel=期间他发了新消息或已达停发上限，任务作废；
    due=到期且无新消息，交给 worker 现场生成后续回复。"""
    if now < float(data.get("send_after") or 0):
        return "wait"
    if unreplied >= stop_at:
        return "cancel"
    if float(ctx_received_at or 0) > float(data.get("created_at") or 0):
        return "cancel"
    return "due"


def _sender_pass(session, user_id, gap_cfg, state, skip_media=False):
    if _paused() or _needs_relogin():
        return
    try:
        _inbound_refetch_sweep()
    except Exception as e:
        serve_log(f"入站补取清扫异常：{type(e).__name__}: {e}")
    stop_at = int(cfg()["unreplied_stop_at"])
    try:
        files = sorted(f for f in os.listdir(PENDING) if f.endswith(".json"))
    except OSError:
        files = []
    # 分组/typing 状态跨轮次保存（state 由 _sender 传入），否则组尾的 typing=2 会丢
    cur_group = state.get("cur_group")
    group_start_ts = state.get("group_start_ts", 0.0)
    group_gap = state.get("group_gap") or random.uniform(gap_cfg[0], gap_cfg[1])
    typing_group = state.get("typing_group")
    last_send_ts = state.get("last_send_ts", 0.0)
    for fn in files:
        if _stop.is_set() or _paused() or _needs_relogin():
            break
        path = os.path.join(PENDING, fn)
        data = read_json(path, None)
        if not isinstance(data, dict):
            if not os.path.exists(path):
                continue  # 已被其他线程/硬门处置移走，名单快照过期，不判损坏
            # 内容损坏/不是 JSON 对象：生成方落盘可能正写到一半，给 10 秒
            # 宽限（按 mtime）；超龄仍非对象则判损坏移 failed——否则本条
            # 每轮被无限跳过、永不判死，且排在其前面的条目发送后会在
            # 下方「下一条预检」读到它时 .get 崩溃整轮（bughunt2 实证）
            try:
                bad_age = time.time() - os.path.getmtime(path)
            except OSError:
                bad_age = 999.0
            if bad_age > 10:
                _move_with_error(path, {}, "文件内容损坏或不是 JSON 对象")
                serve_log(f"outbox 文件 {fn} 内容损坏，移入 failed/")
            continue
        if skip_media and data.get("media_file"):
            # 媒体条归媒体发送线程：本线程跳过，不校验、不动分组/typing
            # 状态——一条媒体在坏网络里长时重试，不再拖住文本回复。
            continue
        need = ("to_user_id", "text", "context_token", "message_id", "send_after", "kind", "typing_sec")
        if any(k not in data for k in need) or data.get("kind") not in ("reply", "proactive", "later", "tune"):
            _move_with_error(path, data if isinstance(data, dict) else {}, "字段不完整或 kind 非法")
            serve_log(f"outbox 文件 {fn} 字段不完整，移入 failed/")
            continue
        bad_val = _entry_value_bad(data)
        if bad_val:
            _move_with_error(path, data, f"字段值损坏({bad_val})")
            serve_log(f"outbox 文件 {fn} 字段 {bad_val} 值损坏，移入 failed/")
            continue
        if data.get("kind") == "later":
            # 两段式回复的延迟任务：从不直接发送，只负责到期唤醒 worker 现场生成
            act = later_action(data, (read_context() or {}).get("received_at", 0),
                               time.time(), get_unreplied(), stop_at)
            if act == "wait":
                continue
            os.replace(path, os.path.join(SENT, fn))
            if act == "cancel":
                serve_log(f"later 任务 {fn} 已取消（期间有新消息或已达停发上限）")
                continue
            # placeholder 正文回填：later 计时文件的 text 按规程恒为空，
            # 占位气泡正文只落在 history（同 message_id 的 assistant 记录，
            # 发送成功时已追加）。不回填则 delayed_due 的 placeholder 恒
            # 为 ""，生成方领续写任务时拿不到第一段说了什么（第四轮线2
            # 端到端实证）。历史里找不到（占位未送达等）时保持空串兜底。
            _ph_text = data.get("text", "")
            if not _ph_text:
                for _h in read_jsonl_tail(HISTORY_FILE, 50):
                    if _h.get("role") == "assistant" \
                            and str(_h.get("message_id")) == str(data["message_id"]) \
                            and _h.get("text"):
                        _ph_text = str(_h["text"])
                        break
            atomic_write_json(DELAYED_DUE_FILE,
                              {"message_id": data["message_id"],
                               "to_user_id": data["to_user_id"],
                               "context_token": data["context_token"],
                               "placeholder": _ph_text,
                               "created_at": data.get("created_at"),
                               "due_at": time.time()})
            serve_log(f"later 任务 {fn} 到期，唤醒 worker 生成后续回复")
            touch_trigger_and_spawn_worker()
            continue
        group_key = str(data["message_id"])
        is_first = group_key != cur_group
        if is_first:
            cur_group = group_key
            group_gap = random.uniform(gap_cfg[0], gap_cfg[1])
            group_start_ts = 0.0
            # 数值硬门（台账 #38）：组首条目发送前整组评估，违规整组作废
            if data.get("kind") == "reply" and _numeric_gate_blocks(group_key):
                continue
        if get_unreplied() >= stop_at:
            serve_log(f"连续未回复计数已达上限 {stop_at}，停止发送，文件留 pending")
            break
        now = time.time()
        send_after = float(data.get("send_after") or 0)
        typing_sec = float(data.get("typing_sec") or 0)
        # 正在输入：到达 send_after - typing_sec 时先 typing=1
        if is_first and typing_sec > 0:
            if now < send_after - typing_sec:
                continue
            if typing_group != group_key:
                if send_typing(session, data["to_user_id"], 1, data["context_token"], log=serve_log):
                    typing_group = group_key
        if now < send_after:
            continue
        if not is_first:
            # 拆条打断：第 1 条已发、发后续条之前来了新消息 → 取消本组剩余条
            ctx = read_context()
            if group_start_ts and float(ctx.get("received_at", 0)) > group_start_ts:
                _move_with_error(path, data, "被新消息打断")
                serve_log(f"outbox {fn} 被新消息打断，移入 failed 并触发 worker")
                if typing_group == group_key:
                    send_typing(session, data["to_user_id"], 2, data["context_token"], log=serve_log)
                    typing_group = None
                touch_trigger_and_spawn_worker()
                continue
            if last_send_ts:
                wait = last_send_ts + group_gap - time.time()
                if wait > 0:
                    _stop.wait(wait)
                    # 等待后复查一次打断
                    ctx = read_context()
                    if group_start_ts and float(ctx.get("received_at", 0)) > group_start_ts:
                        _move_with_error(path, data, "被新消息打断")
                        serve_log(f"outbox {fn} 被新消息打断，移入 failed 并触发 worker")
                        touch_trigger_and_spawn_worker()
                        continue
        # 主动消息发送前复查
        if data["kind"] == "proactive":
            ctx = read_context()
            if ctx.get("stale"):
                _move_with_error(path, data, "context 已 stale，主动消息作废")
                serve_log(f"主动消息 {fn} 因 context stale 作废")
                continue
            if ctx.get("context_token") and ctx["context_token"] != data["context_token"]:
                _move_with_error(path, data, "已有新对话")
                serve_log(f"主动消息 {fn} 因已有新对话作废")
                continue
        # client_id：首次尝试时生成并写回文件，重试/重启后不变
        if not data.get("client_id"):
            data["client_id"] = "wxb-" + uuid.uuid4().hex
            write_json_locked(path, data)
        ctx_snapshot = read_context()
        ctx_age_h = (time.time() - float(ctx_snapshot.get("received_at", time.time()))) / 3600.0 \
            if ctx_snapshot.get("received_at") else 0.0
        try:
            if data.get("media_file"):
                send_media_message(session, data["to_user_id"], data["media_file"],
                                   data.get("media_type", "image"), data["context_token"],
                                   client_id=data["client_id"], log=serve_log,
                                   cache_data=data, cache_path=path)
            else:
                send_message(session, data["to_user_id"], data["text"], data["context_token"],
                             client_id=data["client_id"], log=serve_log)
        except TokenDead:
            _move_with_error(path, data, "sendmessage 连续 -14，登录已过期", errcode=-14)
            append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": data["kind"],
                                       "context_age_hours": round(ctx_age_h, 4), "ok": False,
                                       "errcode": -14})
            if typing_group == group_key:
                typing_group = None
            break
        except Exception as e:
            code = getattr(e, "errcode", None)
            retry_delay = _media_retry_delay(data, e)
            if retry_delay is not None:
                # 媒体条超时：留 pending 退避重试，不判死、不标 context stale
                data["send_attempts"] = int(data.get("send_attempts") or 0) + 1
                data["send_after"] = time.time() + retry_delay
                write_json_locked(path, data)
                serve_log(f"outbox {fn} 媒体发送瞬时失败（第 {data['send_attempts']} 次重试，"
                          f"{retry_delay}s 后再试）：{e}")
                continue
            _move_with_error(path, data, f"{type(e).__name__}: {e}", errcode=code)
            append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": data["kind"],
                                       "context_age_hours": round(ctx_age_h, 4), "ok": False,
                                       "errcode": code})
            serve_log(f"outbox {fn} 发送失败，移入 failed/：{e}")
            # reply 组 token 失效整组止损：同组其余条目不必再逐条撞
            # 同一堵墙，见 _is_context_invalid_failure 保守口径。
            if data.get("kind") == "reply" and _is_context_invalid_failure(e, code):
                aborted = _abort_group_pending(group_key, code, exclude_fn=fn)
                if aborted:
                    serve_log(f"outbox 同组回复 {group_key} token 失效，整组止损，"
                              f"另移 {aborted} 条入 failed/")
            if _proactive_failure_marks_stale(data, e, code):
                mark_context_stale()
                new_max = lower_proactive_max_age(ctx_age_h)
                serve_log(f"主动消息失败，已标记 context stale，max_context_age_hours 下调为 {new_max:.3f}")
            continue
        # 成功
        os.replace(path, os.path.join(SENT, fn))
        if not group_start_ts:
            group_start_ts = time.time()
        prev_send_ts = last_send_ts
        last_send_ts = time.time()
        if data["kind"] != "tune":
            # ⚙️ 系统消息（kind=tune）不进聊天记录、不计未回复数（阶段六 6.1）
            append_jsonl(HISTORY_FILE, {"ts": time.time(), "role": "assistant", "kind": data["kind"],
                                        "message_id": data["message_id"], "text": data["text"]})
            bump_unreplied(1)
        append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": data["kind"],
                                   "context_age_hours": round(ctx_age_h, 4), "ok": True})
        if data["kind"] == "reply":
            ctx = read_context()
            if ctx.get("received_at"):
                fast = bool(prev_send_ts and
                            float(ctx["received_at"]) - prev_send_ts <= 300)
                record_latency(time.time() - float(ctx["received_at"]), fast=fast)
        serve_log(f"outbox 已发送 {fn} kind={data['kind']} ctx={mask_secret(data['context_token'])}")
        # 本组最后一条发完后关 typing：下一文件不是同组，或已是最后一个。
        # skip_media 时媒体文件对分组透明：关 typing 只看下一条文本文件。
        idx = files.index(fn)
        nxt_group = None
        for j in range(idx + 1, len(files)):
            nd = read_json(os.path.join(PENDING, files[j]), {}) or {}
            if not isinstance(nd, dict):
                nd = {}  # 损坏文件不参与分组预判（其本体由主循环判死）
            if skip_media and nd.get("media_file"):
                continue
            nxt_group = str(nd.get("message_id"))
            break
        if typing_group == group_key and nxt_group != group_key:
            send_typing(session, data["to_user_id"], 2, data["context_token"], log=serve_log)
            typing_group = None
    state["cur_group"] = cur_group
    state["group_start_ts"] = group_start_ts
    state["group_gap"] = group_gap
    state["typing_group"] = typing_group
    state["last_send_ts"] = last_send_ts


def _media_sender_pass(session, user_id):
    """媒体条专用发送轮（独立线程，与文本发送隔离）。

    拆分动机：单条媒体一次尝试就可能长时占住线程（CDN 套接字超时按
    大小封顶 900s、内层再重试），单线程时其后所有文本回复都被拖住。
    语义与 _sender_pass 的媒体分支保持等价：字段校验、限流停发、主动
    复查、瞬时失败退避重试（沿用同一 client_id 幂等）、失败标 stale
    都照旧。顺序语义：同组（同 message_id）更靠前的条目还在 pending
    时本条等待，保证文本气泡先于其媒体条到达；组内间隔由各条自己的
    send_after 编码，不再依赖主线程的 group_gap。"""
    if _paused() or _needs_relogin():
        return
    # failed/ 清道夫（深审 A2）：低频节流，瞬时类判死的媒体条自动重排
    if time.time() - _FAILED_SCAVENGE_LAST[0] >= FAILED_SCAVENGE_INTERVAL_SEC:
        _FAILED_SCAVENGE_LAST[0] = time.time()
        try:
            _failed_scavenge_pass()
        except Exception as e:
            serve_log(f"failed 清道夫异常：{type(e).__name__}: {e}")
    stop_at = int(cfg()["unreplied_stop_at"])
    try:
        files = sorted(f for f in os.listdir(PENDING) if f.endswith(".json"))
    except OSError:
        files = []
    datas = {}
    for fn in files:
        d = read_json(os.path.join(PENDING, fn), None)
        if isinstance(d, dict):
            datas[fn] = d
    for fn in files:
        if _stop.is_set() or _paused() or _needs_relogin():
            break
        data = datas.get(fn)
        if not isinstance(data, dict) or not data.get("media_file"):
            continue
        path = os.path.join(PENDING, fn)
        need = ("to_user_id", "text", "context_token", "message_id", "send_after", "kind", "typing_sec")
        if any(k not in data for k in need) or data.get("kind") not in ("reply", "proactive", "later", "tune"):
            _move_with_error(path, data, "字段不完整或 kind 非法")
            serve_log(f"outbox 文件 {fn} 字段不完整，移入 failed/")
            continue
        bad_val = _entry_value_bad(data)
        if bad_val:
            _move_with_error(path, data, f"字段值损坏({bad_val})")
            serve_log(f"outbox 文件 {fn} 字段 {bad_val} 值损坏，移入 failed/")
            continue
        if data.get("kind") == "later":
            continue  # later 是延迟任务、从不直接发送（也从不带媒体）
        if get_unreplied() >= stop_at:
            serve_log(f"连续未回复计数已达上限 {stop_at}，停止发送，文件留 pending")
            break
        now = time.time()
        if now < float(data.get("send_after") or 0):
            continue
        group_key = str(data["message_id"])
        # 同组顺序：更靠前的条目（通常是文本气泡）还在 pending 就等它先走
        if any(other != fn and other < fn
               and str(datas.get(other, {}).get("message_id")) == group_key
               for other in datas):
            continue
        # 数值硬门（台账 #38）：临发前以全组（pending∪sent）口径复核，
        # 兜住媒体先行与跨线程时序（文本线程已在组首评估过一次）
        if data["kind"] == "reply" and _numeric_gate_blocks(group_key):
            continue
        # 拆条打断（reply 媒体）：本组第一条已发出后他又发了新消息 →
        # 图片与主线程对文本后续条的口径一致，取消本条；视频豁免作废、
        # 继续发送（2026-10-25 选项 2 默认：视频生成成本高、多为他点名
        # 要的，不因新消息打断而丢）。组首发送时刻取 history（主线程
        # 进程内 group_start_ts 在本线程不可见）。
        if data["kind"] == "reply" and data.get("media_type") != "video":
            # S4（2026-10-25 二轮深审结案）：started 只取 assistant 记录——
            # 入站 user 记录的 ts 是消息到达/落盘时刻、不是发送时刻，混入
            # 会把阈值拉低到 received_at 以下，造成多条合并批次（批内末条
            # 到达晚于首条 user ts）与亚秒级写入扰动的假阳性作废。无
            # assistant 记录时 started 为空、跳过本检查（同组顺序守卫
            # 已在前置把关）。
            started = [float(e.get("ts", 0)) for e in read_jsonl(HISTORY_FILE)
                       if str(e.get("message_id")) == group_key
                       and e.get("role") == "assistant"]
            if started:
                ctx0 = read_context()
                if float(ctx0.get("received_at", 0)) > min(started):
                    _move_with_error(path, data, "被新消息打断")
                    serve_log(f"outbox {fn} 被新消息打断，移入 failed 并触发 worker")
                    touch_trigger_and_spawn_worker()
                    continue
        # 主动消息发送前复查（与主线程同口径）
        if data["kind"] == "proactive":
            ctx = read_context()
            if ctx.get("stale"):
                _move_with_error(path, data, "context 已 stale，主动消息作废")
                serve_log(f"主动消息 {fn} 因 context stale 作废")
                continue
            if ctx.get("context_token") and ctx["context_token"] != data["context_token"]:
                if proactive_media_rescue(data, ctx, now):
                    # 挽回路（仅此一类）：传输重试期间对话已推进，本条
                    # 媒体改绑最新 context_token 续发一次，落盘标记防重；
                    # 文字部分不重发，台账按 partial 建模（见 proactive.py）。
                    serve_log(f"主动媒体 {fn} 已有新对话，但满足挽回条件，"
                              f"改绑最新 context 续发（原 ctx={mask_secret(data['context_token'])}）")
                    data["context_token"] = ctx["context_token"]
                    data["rescued"] = True
                    data["rescue_at"] = now
                    write_json_locked(path, data)
                else:
                    _move_with_error(path, data, "已有新对话")
                    serve_log(f"主动消息 {fn} 因已有新对话作废")
                    continue
        if not os.path.exists(path):
            continue  # 快照后条目已被处置移走（如数值硬门整组作废）
        # client_id：首次尝试时生成并写回文件，重试/重启后不变
        if not data.get("client_id"):
            data["client_id"] = "wxb-" + uuid.uuid4().hex
            write_json_locked(path, data)
        ctx_snapshot = read_context()
        ctx_age_h = (time.time() - float(ctx_snapshot.get("received_at", time.time()))) / 3600.0 \
            if ctx_snapshot.get("received_at") else 0.0
        try:
            send_media_message(session, data["to_user_id"], data["media_file"],
                               data.get("media_type", "image"), data["context_token"],
                               client_id=data["client_id"], log=serve_log,
                               cache_data=data, cache_path=path)
        except TokenDead:
            _move_with_error(path, data, "sendmessage 连续 -14，登录已过期", errcode=-14)
            append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": data["kind"],
                                        "context_age_hours": round(ctx_age_h, 4),
                                        "ok": False, "errcode": -14})
            break
        except Exception as e:
            code = getattr(e, "errcode", None)
            retry_delay = _media_retry_delay(data, e)
            if retry_delay is not None:
                # 媒体条瞬时失败：留 pending 退避重试，不判死、不标 stale。
                # 首次重试时把原 send_after 存进 orig_send_after——后续
                # send_after 会被退避覆盖，改绑挽回的 2 小时窗口按原值算。
                if not data.get("orig_send_after"):
                    data["orig_send_after"] = float(data.get("send_after") or time.time())
                data["send_attempts"] = int(data.get("send_attempts") or 0) + 1
                data["send_after"] = time.time() + retry_delay
                write_json_locked(path, data)
                serve_log(f"outbox {fn} 媒体发送瞬时失败（第 {data['send_attempts']} 次重试，"
                          f"{retry_delay}s 后再试）：{e}")
                continue
            _move_with_error(path, data, f"{type(e).__name__}: {e}", errcode=code)
            append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": data["kind"],
                                        "context_age_hours": round(ctx_age_h, 4),
                                        "ok": False, "errcode": code})
            serve_log(f"outbox {fn} 发送失败，移入 failed/：{e}")
            # reply 组 token 失效整组止损（与文本趟同口径）。
            if data.get("kind") == "reply" and _is_context_invalid_failure(e, code):
                aborted = _abort_group_pending(group_key, code, exclude_fn=fn)
                if aborted:
                    serve_log(f"outbox 同组回复 {group_key} token 失效，整组止损，"
                              f"另移 {aborted} 条入 failed/")
            if _proactive_failure_marks_stale(data, e, code):
                mark_context_stale()
                new_max = lower_proactive_max_age(ctx_age_h)
                serve_log(f"主动消息失败，已标记 context stale，max_context_age_hours 下调为 {new_max:.3f}")
            continue
        # 成功
        os.replace(path, os.path.join(SENT, fn))
        if data["kind"] != "tune":
            append_jsonl(HISTORY_FILE, {"ts": time.time(), "role": "assistant",
                                        "kind": data["kind"],
                                        "message_id": data["message_id"],
                                        "text": data["text"]})
            bump_unreplied(1)
        append_jsonl(HEALTH_FILE, {"ts": time.time(), "kind": data["kind"],
                                    "context_age_hours": round(ctx_age_h, 4),
                                    "ok": True})
        serve_log(f"outbox 已发送 {fn} kind={data['kind']} ctx={mask_secret(data['context_token'])}")


def _media_sender(session):
    user_id = session.get("ilink_user_id")
    while not _stop.is_set():
        try:
            _media_sender_pass(session, user_id)
        except Exception as e:
            serve_log(f"媒体发送线程异常：{type(e).__name__}: {e}")
        _stop.wait(0.5)


def _sd_notify(msg):
    """向 systemd 发送 sd_notify 消息（如 READY=1 / WATCHDOG=1）。不在 systemd 下运行时静默跳过。"""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode("utf-8"))
    except OSError:
        pass


def _watchdog_pinger():
    """每 10 秒向 systemd 报活，前提是接收线程的心跳文件还新鲜（<70 秒）。
    接收线程真卡死 → 心跳变旧 → 停报 → systemd WatchdogSec 到期杀进程并自动拉起，
    不再等外部看门狗的 5 分钟巡检。整进程冻结时本线程同样停摆，效果相同。
    心跳新鲜阈值 70 秒大于单次长轮询最长阻塞（约 55 秒），不会误杀健康进程。"""
    _stop.wait(2)
    while not _stop.is_set():
        try:
            hb = read_json(HEARTBEAT_FILE, {}) or {}
            if time.time() - float(hb.get("ts", 0)) < 70:
                _sd_notify("WATCHDOG=1")
        except Exception:
            pass
        _stop.wait(10)


def _sweep_stale_claims():
    """serve 启动清扫（第五轮线1观察项落地）：进程被 kill -9 于 claim
    窗口内会残留 INBOX/<mid>.claim，堵住该 message_id 的一切重投。
    本函数在单实例锁已持有、收发线程未启动时调用——此刻盘上任何
    *.claim 必然属于已死进程（活进程的 claim 只在处理瞬间存在且由
    本进程产生），整表清掉；对应消息若未落盘，平台重投时可正常处理，
    已落盘的由 exists 去重接力。返回清扫条数。"""
    n = 0
    try:
        names = os.listdir(INBOX)
    except OSError:
        return 0
    for name in names:
        if name.endswith(".claim"):
            try:
                os.remove(os.path.join(INBOX, name))
                n += 1
            except OSError:
                pass
    if n:
        serve_log(f"serve 启动清扫残留 claim {n} 个")
    return n


def cmd_serve(args):
    ensure_dirs()
    cfg()
    session = load_session()
    if not session or not session.get("bot_token"):
        print("未登录：找不到 state/session.json。你需要做：先 login 并 wait-login。", file=sys.stderr)
        return 1
    # 单实例：flock 独占（非阻塞）。抢锁失败立即以专属退出码
    # EXIT_ALREADY_RUNNING（75）退出：不等待、不无锁继续跑、也不写
    # serve.pid（pid 文件只属于持锁者，见下方写入），更不会走到 notify/
    # 收发线程，因此第二实例对平台零请求。独立退出码的意义（2026-10-24
    # P3）：watchdog 与 systemd（unit 的 RestartPreventExitStatus=75）能把
    # 「已有活实例持锁」与真崩溃分开——前者不计拉起失败、不重启循环。
    # 锁 fd 锚定到模块全局 _INSTANCE_LOCK_FD 显式持有至进程结束（原实现
    # 靠局部变量在 cmd_serve 栈帧存活期间不被回收来持锁，语义正确但
    # 脆弱：任何把这段拆进子函数的重构都会让 fd 被 GC 关掉、锁悄悄失效）。
    global _INSTANCE_LOCK_FD
    inst_fd = os.open(INSTANCE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _flock_ex(inst_fd, nb=True)
    except OSError:
        os.close(inst_fd)
        serve_log("serve 抢锁失败：已有实例在运行，本进程以 75 退出")
        print("serve 已有实例在运行，本次退出（exit 75）。", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    _INSTANCE_LOCK_FD = inst_fd
    _sweep_stale_claims()
    _transcript_pending_sweep()
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    def _sig(_s, _f):
        _stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    serve_log(f"serve 启动 pid={os.getpid()} token={mask_secret(session.get('bot_token'))} "
              f"user={mask_user(session.get('ilink_user_id'))}")
    _prune_history()
    notify(session, "start")
    state = {}
    th_send = threading.Thread(target=_sender, args=(session, state), daemon=True)
    th_send.start()
    th_media = threading.Thread(target=_media_sender, args=(session,), daemon=True)
    th_media.start()
    th_ping = threading.Thread(target=_watchdog_pinger, daemon=True)
    th_ping.start()
    # Type=notify 的就绪门：发送与报活线程已起、马上进入接收循环，此时
    # 才告诉 systemd 启动完成；缺这一步 systemctl start 会一直等到超时。
    _sd_notify("READY=1")
    try:
        _receiver(session, state)
    finally:
        _stop.set()
        notify(session, "stop")
        # 只删属于自己的 pid 文件：无条件删在「pid 文件已被外部改写」
        # 时会把新实例的登记抹掉（当前 flock 串行下本不会发生，属纵深防御）。
        try:
            with open(PID_FILE, encoding="utf-8") as f:
                _pid_owner = f.read().strip()
            if _pid_owner == str(os.getpid()):
                os.remove(PID_FILE)
        except OSError:
            pass
        serve_log("serve 已退出")
    return 0


def cmd_send(args):
    ensure_dirs()
    cfg()
    session = load_session()
    if not session or not session.get("bot_token"):
        print("未登录：找不到 state/session.json。你需要做：先 login 并 wait-login。", file=sys.stderr)
        return 1
    ctx = read_context()
    context = args.context if args.context is not None else ctx.get("context_token")
    if not context:
        print("请先从微信给 bot 发一条消息", file=sys.stderr)
        return 1
    to_user = args.to or session.get("ilink_user_id")
    if not to_user:
        print("缺少目标用户 --to，且 session 里没有 ilink_user_id", file=sys.stderr)
        return 1
    try:
        send_message(session, to_user, args.text, context, log=print)
    except TokenDead as e:
        print(f"发送失败：{e}。你需要做：重新扫码登录。", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"发送失败：{e}", file=sys.stderr)
        return 1
    append_jsonl(HISTORY_FILE, {"ts": time.time(), "role": "assistant", "kind": "reply",
                                "message_id": "manual", "text": args.text})
    bump_unreplied(1)
    print(f"已发送给 {mask_user(to_user)}（context={mask_secret(context)}）")
    return 0


def cmd_send_media(args):
    ensure_dirs()
    cfg()
    session = load_session()
    if not session or not session.get("bot_token"):
        print("未登录：找不到 state/session.json。你需要做：先 login 并 wait-login。", file=sys.stderr)
        return 1
    ctx = read_context()
    context = args.context if args.context is not None else ctx.get("context_token")
    if not context:
        print("请先从微信给 bot 发一条消息", file=sys.stderr)
        return 1
    to_user = args.to or session.get("ilink_user_id")
    if not to_user:
        print("缺少目标用户 --to，且 session 里没有 ilink_user_id", file=sys.stderr)
        return 1
    if not os.path.isfile(args.file):
        print(f"文件不存在：{args.file}", file=sys.stderr)
        return 1
    try:
        send_media_message(session, to_user, args.file, args.type, context, log=print)
    except TokenDead as e:
        print(f"发送失败：{e}。你需要做：重新扫码登录。", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"发送失败：{e}", file=sys.stderr)
        return 1
    append_jsonl(HISTORY_FILE, {"ts": time.time(), "role": "assistant", "kind": "reply",
                                "message_id": "manual",
                                "text": f"[{args.type}] {os.path.basename(args.file)}"})
    bump_unreplied(1)
    print(f"已发送{args.type}给 {mask_user(to_user)}：{os.path.basename(args.file)}"
          f"（context={mask_secret(context)}）")
    return 0


def cmd_typing(args):
    ensure_dirs()
    cfg()
    session = load_session()
    if not session or not session.get("bot_token"):
        print("未登录：找不到 state/session.json。", file=sys.stderr)
        return 1
    ctx = read_context()
    ok = send_typing(session, session.get("ilink_user_id"), args.status,
                     ctx.get("context_token"), log=print)
    print("typing 已发送" if ok else "typing 发送失败（不影响发消息）")
    return 0


def _serve_alive():
    pid = None
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def cmd_status(args):
    ensure_dirs()
    cfg()
    session = load_session()
    print("=== ClawBot Companion 状态 ===")
    if not session:
        print("登录状态：未登录")
        return 1
    print("登录状态：已登录")
    print(f"  bot_token：{mask_secret(session.get('bot_token'))}")
    print(f"  ilink_bot_id：{session.get('ilink_bot_id')}")
    print(f"  ilink_user_id：{mask_user(session.get('ilink_user_id'))}")
    pid = _serve_alive()
    hb = read_json(HEARTBEAT_FILE, {}) or {}
    hb_ts = float(hb.get("ts", 0))
    print(f"serve：{'运行中 pid=' + str(pid) if pid else '未运行'}"
          + (f"，最近心跳 {iso(hb_ts)}（{int(time.time() - hb_ts)} 秒前）" if hb_ts else "，无心跳记录"))
    agent_hb = os.path.join(STATE, "agent.heartbeat")
    if os.path.exists(agent_hb):
        try:
            hts = float(read_json(agent_hb, {}).get("ts", 0) if str(read_json(agent_hb, {})).startswith("{") else 0)
        except Exception:
            hts = 0
        print(f"外部 agent 心跳：{iso(hts) if hts else '已存在但无时间戳'}")
    else:
        print("外部 agent 心跳：未启用")
    processed = set(str(x) for x in (read_json(PROCESSED_FILE, {}) or {}).get("processed", []))
    try:
        inbox_ids = [f[:-5] for f in os.listdir(INBOX) if f.endswith(".json")]
    except OSError:
        inbox_ids = []
    unproc = [i for i in inbox_ids if i not in processed]
    def _cnt(d):
        try:
            return len([f for f in os.listdir(d) if f.endswith(".json")])
        except OSError:
            return 0
    print(f"inbox 未处理数：{len(unproc)}（总数 {len(inbox_ids)}）")
    print(f"outbox：pending {_cnt(PENDING)} / sent {_cnt(SENT)} / failed {_cnt(FAILED)}")
    print(f"NEEDS_RELOGIN：{'是' if _needs_relogin() else '否'}")
    print(f"PAUSED：{'是' if _paused() else '否'}")
    print(f"连续未回复计数：{get_unreplied()}（提醒 {cfg()['unreplied_warn_at']} / 停发 {cfg()['unreplied_stop_at']}）")
    ctx = read_context()
    if ctx.get("context_token"):
        age_h = (time.time() - float(ctx.get("received_at", time.time()))) / 3600.0
        print(f"context：已存在 {age_h:.2f} 小时，stale={'是' if ctx.get('stale') else '否'}，"
              f"掩码 {mask_secret(ctx['context_token'])}")
    else:
        print("context：无（需要先从微信发一条消息）")
    health = read_jsonl(HEALTH_FILE)
    ok_ages = [float(h.get("context_age_hours", 0)) for h in health if h.get("ok")]
    print(f"实测成功的最大 context 存在时长：{max(ok_ages):.2f} 小时" if ok_ages
          else "实测成功的最大 context 存在时长：暂无数据")
    # 阶段二起附加（规格 1.5）：模型用量、延迟、life 摘要（文件不存在则跳过）
    usage = (read_json(USAGE_FILE, {}) or {}).get(time.strftime("%Y-%m-%d", time.localtime()), {})
    if usage:
        calls = usage.get("calls", {})
        total_calls = sum(int(v) for v in calls.values())
        detail = "、".join(f"{k} {v}" for k, v in sorted(calls.items()))
        llm_state = read_json(os.path.join(STATE, "llm_state.json"), {}) or {}
        model = usage.get("model", "未知") + ("（备用）" if llm_state.get("using_fallback") else "")
        print(f"今日模型调用：{total_calls} 次（{detail}），reply 上限 {cfg()['llm']['max_calls_per_day']}，当前模型 {model}")
        lat = sorted(float(x) for x in usage.get("reply_latencies_sec", []))
        if lat:
            med = lat[len(lat) // 2] if len(lat) % 2 else (lat[len(lat) // 2 - 1] + lat[len(lat) // 2]) / 2
            p90 = lat[min(len(lat) - 1, int(len(lat) * 0.9))]
            print(f"今日回复延迟：中位数 {med:.1f}s，P90 {p90:.1f}s（样本 {len(lat)}）")
        gen = int(usage.get("generation", 0))
        batches = int(usage.get("batch", 0))
        if gen:
            print(f"今日重写率：{int(usage.get('rewrite', 0)) / gen:.0%}（重写 {int(usage.get('rewrite', 0))}/{gen}）")
        if batches:
            print(f"今日不回复率：{int(usage.get('no_reply', 0)) / batches:.0%}（{int(usage.get('no_reply', 0))}/{batches} 批）")
    life = read_json(os.path.join(STATE, "life.json"), {}) or {}
    if life.get("date") == time.strftime("%Y-%m-%d", time.localtime()):
        print(f"今日 life：{life.get('relationship_stage', '')} / 忙碌 {life.get('busy_level', '')} / "
              f"心情 {life.get('mood', '')} / {life.get('schedule', '')}")
    if _needs_relogin():
        return 2
    if not pid:
        return 3
    return 0


def cmd_pause(args):
    ensure_dirs()
    with open(PAUSED_FILE, "w", encoding="utf-8") as f:
        f.write(iso() + "\n")
    print("已暂停：照常收消息，但不自动回复、不主动发消息。恢复用 resume。")
    return 0


def cmd_resume(args):
    ensure_dirs()
    try:
        os.remove(PAUSED_FILE)
    except OSError:
        pass
    print("已恢复。")
    return 0


def cmd_logout(args):
    ensure_dirs()
    # 停 serve：Linux 下优先 systemd（本仓库 unit 名 clawbot-companion），
    # 其次 pid；Windows 无 systemctl，跳过、直接走 pid 路径。
    if os.name == "posix" and shutil.which("systemctl"):
        try:
            subprocess.run(["systemctl", "stop", "clawbot-companion"], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    pid = _serve_alive()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for p in (SESSION_FILE, PID_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    print("已退出登录：session.json 已删除，其他数据保留。重新使用需再次 login。")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="微信 iLink Bot 通道桥 v7")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    w = sub.add_parser("wait-login")
    w.add_argument("--verify-code", default=None)
    sub.add_parser("serve")
    s = sub.add_parser("send")
    s.add_argument("--text", required=True)
    s.add_argument("--to", default=None)
    s.add_argument("--context", default=None)
    sm = sub.add_parser("send-media")
    sm.add_argument("--file", required=True)
    sm.add_argument("--type", choices=["image", "video", "file"], default="image")
    sm.add_argument("--to", default=None)
    sm.add_argument("--context", default=None)
    t = sub.add_parser("typing")
    t.add_argument("status", type=int, choices=[1, 2])
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("logout")
    args = p.parse_args(argv)
    try:
        if args.cmd == "login":
            return cmd_login(args)
        if args.cmd == "wait-login":
            return cmd_wait_login(args)
        if args.cmd == "serve":
            return cmd_serve(args)
        if args.cmd == "send":
            return cmd_send(args)
        if args.cmd == "send-media":
            return cmd_send_media(args)
        if args.cmd == "typing":
            return cmd_typing(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "pause":
            return cmd_pause(args)
        if args.cmd == "resume":
            return cmd_resume(args)
        if args.cmd == "logout":
            return cmd_logout(args)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
