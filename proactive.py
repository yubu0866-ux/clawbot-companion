#!/usr/bin/env python3
"""
proactive.py — 主动消息决策引擎（开源通用版）。

用途：只做决策与记账，不生成文本。tick 按门控与触发优先级判断这一刻
该不该主动发消息，due 时输出意图、口味与上下文摘要（brief），由生成方
（llm.mode=api 时 worker/llm 链路、agent 时外部 agent）写一条消息进
outbox（kind=proactive），再用 sent 记账。发送端 bridge 还有第二道复查。
配置依赖：config.json 的 proactive 段（开关、时段 slots、概率、上限、
静默时段 quiet_hours、节奏 rhythm、口味 flavors、媒体主体 subjects、
观察期 observation_* 等全部可配）；生活事件来源为 worker 按 facts.json
生成的 state/life.json（缺失时回落 facts.json 的 life 字段）。
门控与触发优先级（followup > 时段 roll > 长静默 > impulse）见 decide()。

proactive.mode="organic" 时改走有机主动引擎：不再用固定时段窗与固定
概率排班，而是读 life.py 每日按日期种子采样的生活计划
（state/life_today.json）——锚点时刻（起床后/午休/下班后/睡前）自带
随机抖动与当日跳过，她的生活事件到分享时刻触发 impulse，长静默关怀
阈值按日采样，两次主动间隔对数正态采样。安全闸（静默时段、context
新鲜度、刚聊完避让）与 fixed 模式共用；每日总量上限仅在 config 的
daily_max 填数字时存在（null/缺省 = 不限，2026-10-27 定版）。未回复
自 2026-10-27 起不再是闸：连续未回复数（streak）只进情绪模型调制
概率、间隔与简报（见 mood_state），没有并存拦截、熔断与冷却。
mode 缺省或 life.py 不可用时回落 fixed（兼容旧行为）。

CLI: tick [--now TS] / sent --intent X [--about T] [--event S] [--message-id M] / dry-run / status
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---- 跨平台文件锁（portable lock shim）----
# POSIX 用 fcntl.flock；Windows 没有 fcntl，改用 msvcrt.locking 对锁文件
# 第 0 字节加范围锁。语义对齐：默认阻塞等锁，nb=True 时冲突立即抛
# OSError（与 flock LOCK_NB 一致）；msvcrt 的阻塞锁约 10 秒后放弃并抛
# OSError，本桥的锁都很短暂，不构成实际差异。
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
        msvcrt.locking(fh, msvcrt.LK_NBLCK if nb else msvcrt.LK_LOCK, 1)

    def _flock_un(fd):
        fh = fd if isinstance(fd, int) else fd.fileno()
        os.lseek(fh, 0, os.SEEK_SET)
        msvcrt.locking(fh, msvcrt.LK_UNLCK, 1)

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("COMPANION_HOME") or HERE
STATE = os.path.join(HOME, "state")
CONFIG_FILE = os.path.join(HOME, "config.json")
PROACTIVE_FILE = os.path.join(STATE, "proactive.json")
CONTEXT_FILE = os.path.join(STATE, "context.json")
COUNTERS_FILE = os.path.join(STATE, "counters.json")
LIFE_FILE = os.path.join(STATE, "life.json")
HISTORY_FILE = os.path.join(STATE, "history.jsonl")
RECORDS_FILE = os.path.join(STATE, "reply_records.jsonl")
ROLLS_FILE = os.path.join(STATE, "proactive_rolls.jsonl")
INBOX = os.path.join(HOME, "inbox")
FAILED_DIR = os.path.join(HOME, "outbox", "failed")
SENT_DIR = os.path.join(HOME, "outbox", "sent")
PENDING_DIR = os.path.join(HOME, "outbox", "pending")
PAUSED_FILE = os.path.join(STATE, "PAUSED")
NEEDS_RELOGIN_FILE = os.path.join(STATE, "NEEDS_RELOGIN")
FACTS_FILE = os.path.join(HOME, "facts.json")

# life.py（生活圈子/每日计划/主人画像）是同目录模块：按本文件所在路径
# 显式加载（与 worker.py 的 _load 同法），避免 import 缓存串到别处的
# 同名模块；没拷 life.py 时容错回落，有机模式自动不可用、行为等同
# fixed，绝不因缺模块崩溃。
try:
    import importlib.util as _ilu
    _life_spec = _ilu.spec_from_file_location(
        "clawbot_life", os.path.join(HERE, "life.py"))
    _life = _ilu.module_from_spec(_life_spec)
    _life_spec.loader.exec_module(_life)
except Exception:
    _life = None
# 长静默触发参数（默认值，可被 config.proactive.silence_p / silence_window 覆盖）
SILENCE_P = 0.85
SILENCE_WINDOW = (9, 22)  # 长静默触发只在白天到晚上 22 点前


def silence_p(pcfg=None):
    """长静默触发概率：config.proactive.silence_p（扁平键）覆盖，缺失/非法
    回落 SILENCE_P。decide() 的长静默与 impulse 分支都经此函数取值。"""
    if pcfg is None:
        pcfg = cfg().get("proactive") or {}
    try:
        return float(pcfg.get("silence_p", SILENCE_P))
    except (TypeError, ValueError):
        return SILENCE_P


def silence_window(pcfg=None):
    """长静默可触发时段 [起, 止)（小时）：config.proactive.silence_window
    （扁平键，[9, 22] 形式）覆盖，缺失/非法回落 SILENCE_WINDOW。"""
    if pcfg is None:
        pcfg = cfg().get("proactive") or {}
    w = pcfg.get("silence_window")
    if isinstance(w, (list, tuple)) and len(w) == 2:
        try:
            return int(w[0]), int(w[1])
        except (TypeError, ValueError):
            pass
    return SILENCE_WINDOW
# 媒体分形式条数不单独设上限：只受主动总量 daily_max 约束；
# video_sends_today 只作台账统计，不把关


def _quarantine_corrupt(path):
    """损坏 state 文件留痕（与 bridge.read_json 同口径）：改名
    <path>.corrupt-<unix秒> 保全证据，防吞错后空数据写回静默清零。
    最佳努力，失败静默。同秒唯一命名与 bridge 同口径：同文件同秒
    再次损坏时基础名后加 -<pid>、再撞加 -2/-3…，不覆盖前一份证据。"""
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


def read_json(path, default=None):
    # 只认 dict 顶层：本桥全部单文件 JSON 存储按构造都是对象；顶层是
    # 裸标量/数组属损坏形态，透传会让下游 .get 消费者崩溃（实证：
    # counters.json=5 时消费者 AttributeError），与解析失败同口径回
    # default。解析失败/非对象顶层先留痕，OSError（文件不存在等
    # 常态）不留痕。
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return default
    except ValueError:
        _quarantine_corrupt(path)
        return default
    if not isinstance(data, dict):
        _quarantine_corrupt(path)
        return default
    return data


def write_json(path, data):
    # tmp 名必须按写者唯一：固定 ".tmp" 时两个并发写者共用同一临时文件，
    # 一方的 os.replace 会把另一方的 tmp 抢走 → 对方 FileNotFoundError
    # 崩溃（实证），与 bridge.atomic_write_json 同口径
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())  # 与 bridge.atomic_write_json 对齐
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    if hasattr(os, "O_DIRECTORY"):  # 目录 fsync 仅 POSIX
        try:
            dfd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
            os.fsync(dfd)
            os.close(dfd)
        except OSError:
            pass


def read_jsonl(path):
    # 只收 dict 行（与 bridge.read_jsonl 同口径）：非对象行是损坏形态，
    # 透传会让 .get 消费者崩溃（bughunt2-2026-10-25）
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


def cfg():
    return read_json(CONFIG_FILE, {}) or {}


def tz():
    try:
        return ZoneInfo(cfg().get("tz", "Asia/Shanghai"))
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def load_state():
    st = read_json(PROACTIVE_FILE, {}) or {}
    st.setdefault("date", None)
    st.setdefault("sent_count", 0)
    st.setdefault("last_sent_at", None)
    st.setdefault("last_intent", None)
    st.setdefault("awaiting_reply", False)
    st.setdefault("slot_rolls", {})
    st.setdefault("followups", [])
    st.setdefault("enabled", True)
    st.setdefault("started_on", None)
    st.setdefault("log", [])            # 主动消息台账：{ts, intent, replied}
    st.setdefault("cooldown_until", 0)  # 连续被晾后的冷却截止
    st.setdefault("slot_streaks", {})   # 每个时段的连中/连败计数（roll 平衡用）
    st.setdefault("last_silence_date", None)
    st.setdefault("last_impulse_date", None)
    st.setdefault("flavor_history", [])  # 最近用过的主动口味（内容轮换用）
    st.setdefault("pending_flavor", "")
    st.setdefault("pending_form", "")
    st.setdefault("last_video_date", None)
    if "video_sends_today" not in st:
        # 迁移（2026-10-24 当日计数制）：旧 state 只有 last_video_date，
        # 若它就是今天，说明今天已发过 1 条视频，计数从 1 起步。
        # 计数现仅作台账统计（条数上限已取消），迁移只为统计连续
        st["video_sends_today"] = 1 if st.get("last_video_date") == \
            datetime.now(tz()).date().isoformat() else 0
    return st


def save_state(st):
    write_json(PROACTIVE_FILE, st)


def append_roll(entry):
    """roll 结果 durable 落盘（二轮深审 §5）：slot_rolls 只是当日内存态，
    历史上无法复算各时段真实命中率与空耗比例。每次真 roll（时段档与
    silence）append 一行到 proactive_rolls.jsonl；文件超 512KB 时截尾
    只留最近 2000 行。落盘失败绝不影响决策主流程。impulse 的 roll 是
    每 tick 的临时判定、不落 slot_rolls，其命中已见发送台账，不在此列。"""
    try:
        with open(ROLLS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if os.path.getsize(ROLLS_FILE) > 512 * 1024:
            with open(ROLLS_FILE, encoding="utf-8") as f:
                lines = f.readlines()
            tmp = ROLLS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[-2000:])
            os.replace(tmp, ROLLS_FILE)
    except Exception:
        pass


@contextmanager
def state_lock():
    """主动状态的跨进程互斥：tick 与 sent 是两个独立 CLI 进程，
    各自整表读-改-写 proactive.json；cron 运行重叠时后写的旧快照会
    覆盖先写的台账（2026-10-21 18:36 一条 impulse 的 sent 记账成功，
    台账里却没有这条 log，对账也无从标作废）。CLI 入口持锁执行，
    生成消息的 agent 在锁外工作，不会被这把锁拖住。锁文件与
    bridge/worker 的 state_lock 共用 state/.lock，几方写同一文件时
    至少不会互不知情地整表覆盖。"""
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, ".lock"), "a+") as f:
        _flock_ex(f)
        try:
            yield
        finally:
            _flock_un(f)


def _roll_day(st, today):
    if st.get("date") != today:
        st["date"] = today
        st["sent_count"] = 0
        st["slot_rolls"] = {}
        st["video_sends_today"] = 0
    if not st.get("started_on"):
        st["started_on"] = today
    return st


DEFAULT_FLAVORS = ["日常分享", "近况问候", "轻松玩笑", "话题续接",
                   "小见闻分享", "回忆细节", "关心提醒"]


def flavors(pcfg=None):
    if pcfg is None:
        pcfg = cfg().get("proactive") or {}
    fl = pcfg.get("flavors")
    return [str(x) for x in fl] if isinstance(fl, list) and fl else list(DEFAULT_FLAVORS)
FLAVOR_INTENTS = ("morning", "lunch", "evening", "silence", "impulse")


def pick_flavor(st, intent):
    """内容口味轮换：连续的主动消息不要总是同一种打开方式。
    去重窗为近 8 次（2026-10-25 由近 5 扩窗，L7）：窗内出现过的口味
    先排除、其余随机取；全味都在窗内时挑最久没用过的那个（上次出现
    位置最靠前），保证轮转间隔尽量拉满。每种口味都要够得着：旧实现
    按 FLAVORS 顺序取第一个未用过的，排末位的「回忆细节」永远轮不到
    （其余四种不可能同时挤满窗口），2026-10-23 复盘改随机后保留随机。"""
    all_flavors = flavors()
    if intent not in FLAVOR_INTENTS:
        return ""
    recent = (st.get("flavor_history") or [])[-8:]
    eligible = [f for f in all_flavors if f not in recent]
    if eligible:
        return random.choice(eligible)

    def last_idx(f):
        for i in range(len(recent) - 1, -1, -1):
            if recent[i] == f:
                return i
        return -1

    return min(all_flavors, key=last_idx)


def _day_of(value):
    """把 state 里的日期/时间字符串统一解析成本地（config tz）日期。
    防 S2 类 naive/aware 混算地雷：日期串按字面取日；naive 时间串按
    本地时区解释后取日（与旧 .date() 口径一致）；带偏移的时间串先换算
    到本地时区再取日——旧写法直接对 aware 结果取 .date() 用的是偏移
    自身的帧，跨日界时会差一天。解析不了抛 ValueError，由调用方兜底。"""
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt.astimezone(tz()).date()


def daily_cap(st, pcfg, today):
    """基线：daily_max 为 null/缺省时返回 None = 不设总量上限（2026-10-27
    定版：发送量由生活计划的锚点/事件数量与间隔采样自然涌现，不再有
    计数闸）；daily_max 填数字时旧口径全保留——上线头 observation_days
    天为观察期、每天最多 observation_cap 条，之后用 daily_max。实际
    生效上限由 effective_cap 按对方回应率在此基础上向下浮动。"""
    dm = pcfg.get("daily_max")
    if dm is None:
        return None
    try:
        started = _day_of(st.get("started_on") or today)
        days = (_day_of(today) - started).days
    except ValueError:
        days = 99
    if days < int(pcfg.get("observation_days", 2)):
        return min(int(pcfg.get("observation_cap", 4)), int(dm))
    return int(dm)


def reconcile_voided(st):
    """对账：被 bridge 发送端作废的主动消息（发送前发现已有新对话、
    或 context 已 stale）并没有真正送达，不能记成已发送。
    扫 outbox/failed 里的 pro-*.json（带 error 标记），把台账里
    对应条目标 voided 并清掉 replied，让回应率/条数统计回到真实。
    部分送达单独建模（partial）：同组已有条目进了 outbox/sent
    （如文字气泡已达）时，媒体条作废/最终失败只是没补齐，不整组
    作废——标 partial、不清 replied、不回减 sent_count；媒体经改绑
    挽回后补达的（sent 文件带 rescued 标记）同样标 partial。
    返回是否有改动。"""
    changed = False
    try:
        names = os.listdir(FAILED_DIR)
    except OSError:
        names = []
    try:
        sent_names = set(os.listdir(SENT_DIR))
    except OSError:
        sent_names = set()

    def _group_sent(mid):
        return any(n == mid + ".json" or n.startswith(mid + "-")
                   for n in sent_names)

    voided = []
    partials = []
    for fn in names:
        if not (fn.startswith("pro-") and fn.endswith(".json")):
            continue
        try:
            d = read_json(os.path.join(FAILED_DIR, fn), {}) or {}
        except Exception:
            continue
        mid = str(d.get("message_id", ""))
        if not mid.startswith("pro-"):
            continue
        try:
            vts = float(mid[4:])
        except ValueError:
            vts = None
        err = str(d.get("error", ""))
        is_void_err = ("作废" in err or "已有新对话" in err or "stale" in err)
        if _group_sent(mid):
            partials.append((mid, vts))
        elif is_void_err:
            voided.append((mid, vts))
    # 改绑挽回后补达：sent 里的媒体文件带 rescued 标记
    for fn in sorted(sent_names):
        if not (fn.startswith("pro-") and fn.endswith(".json")):
            continue
        try:
            d = read_json(os.path.join(SENT_DIR, fn), {}) or {}
        except Exception:
            continue
        if d.get("rescued"):
            mid = str(d.get("message_id", ""))
            if mid.startswith("pro-"):
                try:
                    vts = float(mid[4:])
                except ValueError:
                    vts = None
                partials.append((mid, vts))

    def _apply(e):
        nonlocal changed
        e["voided"] = True
        # E2（2026-10-25 落地）：作废落双时间戳——原 ts 决定计数归属日，
        # voided_at 记作废发生时刻，两者分开后跨日对账才可复算。
        e["voided_at"] = time.time()
        e["replied"] = None
        e.pop("latency_sec", None)
        # 展示计数同步回减：mark_sent 时先计了数，作废意味着没送达；
        # 只回减记账日期与当前 state 日期相同的条目，跨日对账不动今日数。
        try:
            entry_day = datetime.fromtimestamp(
                float(e.get("ts", 0)), tz()).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            entry_day = None
        if entry_day is not None and entry_day == st.get("date"):
            st["sent_count"] = max(0, int(st.get("sent_count", 0)) - 1)
        changed = True

    # 先按 message_id 精确匹配（mark_sent 落盘的本轮 id）：不受记账时刻与
    # message_id 时间戳口径差影响。一条台账只被作废一次。
    rest = []
    for mid, vts in voided:
        hit = None
        if mid:
            for e in st.get("log", []):
                if not e.get("voided") and e.get("message_id") == mid:
                    hit = e
                    break
        if hit is not None:
            _apply(hit)
        elif vts is not None:
            rest.append(vts)
    # 存量条目没有 message_id：按时间邻近兜底。窗口放宽到 -900：
    # message_id 常取自含 jitter 的 send_after，可比记账时刻晚最多约
    # 10 分钟（2026-10-23 实测一条 delta=-53 的作废视频条因此漏对账）。
    for vts in sorted(rest):
        best = None
        for e in st.get("log", []):
            if e.get("voided"):
                continue
            delta = float(e.get("ts", 0)) - vts
            if -900 <= delta <= 1200 and \
                    (best is None or abs(delta) < abs(best[0])):
                best = (delta, e)
        if best is not None:
            _apply(best[1])
    # partial 落标：与 voided 同样的匹配口径（message_id 精确优先、
    # 存量无 id 条目时间邻近兜底）；已作废的条目不再标 partial。
    def _apply_partial(e):
        nonlocal changed
        if e.get("voided") or e.get("partial"):
            return
        e["partial"] = True
        changed = True

    rest_p = []
    for mid, vts in partials:
        hit = None
        if mid:
            for e in st.get("log", []):
                if not e.get("voided") and e.get("message_id") == mid:
                    hit = e
                    break
        if hit is not None:
            _apply_partial(hit)
        elif vts is not None:
            rest_p.append(vts)
    matched_p = set()
    for vts in sorted(rest_p):
        best = None
        for e in st.get("log", []):
            if e.get("voided"):
                continue
            delta = float(e.get("ts", 0)) - vts
            if -900 <= delta <= 1200 and \
                    (best is None or abs(delta) < abs(best[0])):
                best = (delta, e)
        if best is not None:
            _apply_partial(best[1])
            matched_p.add(vts)
    # 冻结误标转正：条目已被标 voided、但同组确有气泡进了 sent 的，
    # 是 partial 建模上线（2026-10-23）前被旧口径整组作废、之后又被
    # 上面「已 voided 不再标 partial」守卫永久冻结的误标（同日实测
    # 两条：主文字已送达且他都回复了，台账却记作废未回复）。这类条目
    # 转正为 partial：清 voided、sent_count 回补（与 _apply 的回减
    # 镜像）、按 history 里他在此后的首次发言补结算 replied/latency。
    # 归属加一道保险：partial 组的时间戳须比任何真作废组都更贴近
    # 该条目，否则归属存疑、不转正。二次运行幂等（条目已非 voided）。
    void_vts = [v for _, v in voided if v is not None]
    hist_rows = None
    for vts in sorted(v for v in rest_p if v not in matched_p):
        best = None
        for e in st.get("log", []):
            if not e.get("voided"):
                continue
            delta = float(e.get("ts", 0)) - vts
            if -900 <= delta <= 1200 and \
                    (best is None or abs(delta) < abs(best[0])):
                best = (delta, e)
        if best is None:
            continue
        e = best[1]
        ets = float(e.get("ts", 0))
        if any(abs(ets - vv) < abs(best[0]) for vv in void_vts):
            continue
        e.pop("voided", None)
        e.pop("voided_at", None)
        e["partial"] = True
        try:
            entry_day = datetime.fromtimestamp(ets, tz()).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            entry_day = None
        if entry_day is not None and entry_day == st.get("date"):
            st["sent_count"] = int(st.get("sent_count", 0)) + 1
        if hist_rows is None:
            hist_rows = read_jsonl(HISTORY_FILE)
        replies = [float(h.get("ts", 0)) for h in hist_rows
                   if h.get("role") == "user"
                   and float(h.get("ts", 0) or 0) > vts]
        if replies:
            e["replied"] = True
            e["latency_sec"] = min(replies) - vts
        elif time.time() - ets > 12 * 3600:
            e["replied"] = False
        changed = True
    return changed


def logged_sent_count(st):
    """按台账重算某一天的真实已记账主动数：只数未作废条目。

    sent_count 是 mark_sent 时的先行计数，作废靠 reconcile 回减；
    若某条作废在旧版本时就已标好（没赶上回减），status 用这个数校正，
    保证展示口径与 sent_last_24h 一样剔除 voided。"""
    day = st.get("date")
    if not day:
        return 0
    n = 0
    for e in st.get("log", []):
        if e.get("voided"):
            continue
        try:
            d = datetime.fromtimestamp(float(e.get("ts", 0)), tz()).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
        if d == day:
            n += 1
    return n


def derive_day_counts(st, days=3):
    """E2 对账真相源：近 days 天各日期的真实已记账主动数，一律按条目
    自身 ts 的本地日期归属、剔除 voided，读时从台账派生——sent_count
    只是当日缓存（status 会按 logged_sent_count 校正），跨日作废的
    条目在归属日自动消失，绝不回滚作废当日的今日数。"""
    today = datetime.now(tz()).date()
    out = {(today - timedelta(days=i)).isoformat(): 0
           for i in range(days)}
    for e in st.get("log", []):
        if e.get("voided"):
            continue
        try:
            d = datetime.fromtimestamp(
                float(e.get("ts", 0)), tz()).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
        if d in out:
            out[d] += 1
    return out


def used_events_today(st, today):
    """今日已记账主动用过的 life 事件签名（log 条目的 event，followup 另有
    about）：供生成侧选事件时避开同事件同梗。作废条目也算用过——文案
    虽没送达，梗已经在生成侧消耗过，换一件更稳妥；同一签名只列一次。"""
    out = []
    for e in st.get("log", []):
        try:
            d = datetime.fromtimestamp(float(e.get("ts", 0)), tz()).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
        if d != today:
            continue
        for key in ("event", "about"):
            v = str(e.get(key) or "").strip()
            if v and v not in out:
                out.append(v)
    return out


def resolve_replies(st, now, ctx_received_at, window_h=12, cooldown_h=24):
    """结算台账里未定性的主动消息：他在此后发过消息 = 已回复；
    超过 window_h 小时还没动静 = 被晾。结算结果只喂回应率统计与情绪
    模型（mood_state）；2026-10-27 起不再因连续被晾设冷却——
    cooldown_h 参数保留仅为调用兼容，不再写 cooldown_until。"""
    changed = False
    for e in st.get("log", []):
        if e.get("voided"):
            continue  # 已作废，没送达，不参与结算
        if e.get("replied") is None:
            if ctx_received_at and float(ctx_received_at) > float(e.get("ts", 0)):
                e["replied"] = True
                e["latency_sec"] = float(ctx_received_at) - float(e.get("ts", 0))
                changed = True
            elif now - float(e.get("ts", 0)) > window_h * 3600:
                e["replied"] = False
                changed = True
    return changed


def reply_stats(st):
    resolved = [e for e in st.get("log", []) if e.get("replied") is not None][-10:]
    if not resolved:
        return {"n": 0, "rate": None, "median_latency": None}
    ok = sum(1 for e in resolved if e.get("replied"))
    lats = sorted(float(e["latency_sec"]) for e in resolved
                  if e.get("replied") and e.get("latency_sec") is not None)
    return {"n": len(resolved), "rate": ok / len(resolved),
            "median_latency": lats[len(lats) // 2] if lats else None}


# ---------------- 情绪模型（2026-10-27 定版） ----------------
# 未回复不再是任何一道闸：没有并存拦截、没有熔断、没有冷却。台账结算
# 出的连续未回复数（streak）只作调制信号——她发起的概率按 factor
# 衰减、发送间隔按 streak 拉长、生成简报带上情绪标签；他一回复 streak
# 立即清零，跨日先减半（睡一觉气消了一半），兴奋的生活事件还能再冲淡。
# factor 有下限（0.3），概率永不归零：代码里不存在因未回复停发的路径。
MOOD_FACTOR_FLOOR = 0.3
MOOD_FACTOR_BASE = 0.85


def _mood_label(streak):
    if streak <= 0:
        return ""
    if streak == 1:
        return "有一点被晾着的小情绪"
    if streak == 2:
        return "连着两条没回，主动时语气放轻一点"
    if streak <= 4:
        return "被晾了几条了，主动时更小心翼翼，别追问"
    return "好几条没回了，只轻轻碰一下，不连发不追问"


def mood_state(st, today):
    """情绪模型记账（st["mood"] = {streak, date, seen_ts}）：按台账顺序
    消费新结算的条目——replied=True 清零、False 加一；未结算（None）
    的条目挡住扫描（结算按时间成前缀，不会乱序计入）。跨日首次调用
    先把 streak 减半。返回 {streak, factor, label}；本函数只改内存，
    落盘由调用方负责（decide 的调用点已统一 save）。"""
    mood = st.get("mood")
    if not isinstance(mood, dict):
        mood = {"streak": 0, "date": today, "seen_ts": 0.0}
        st["mood"] = mood
    if mood.get("date") != today:
        mood["streak"] = int(mood.get("streak", 0) or 0) // 2
        mood["date"] = today
    seen = float(mood.get("seen_ts") or 0)
    streak = int(mood.get("streak", 0) or 0)
    for e in st.get("log", []):
        ts = float(e.get("ts", 0) or 0)
        if ts <= seen or e.get("voided"):
            continue
        r = e.get("replied")
        if r is None:
            break
        streak = 0 if r else streak + 1
        seen = ts
    mood["streak"] = streak
    mood["seen_ts"] = seen
    return {"streak": streak,
            "factor": max(MOOD_FACTOR_FLOOR, MOOD_FACTOR_BASE ** streak),
            "label": _mood_label(streak)}


def effective_cap(st, pcfg, today):
    """生效上限：基线（观察期 3 / 常规 daily_max）之内按回应率下调——
    样本 >=5：回应率 >=40% 用基线，20%-40% 降到 4，更低降到 2
    （2026-10-22 放开前的旧档是 2 / 1）；
    回应好只体现在不降额、提前毕业与间隔收紧，不突破基线。
    观察期里若已有 >=3 条且条条被回复，提前放开到常规基线。"""
    base = daily_cap(st, pcfg, today)
    if base is None:
        return None  # 不设上限：没有基线可浮动
    stats = reply_stats(st)
    # 提前毕业必须先判：观察期内条条被回就放开到 daily_max；
    # 若先走下面 n>=5 的回应率分支，会直接返回观察期基线，把毕业挡死
    resolved = [e for e in st.get("log", []) if e.get("replied") is not None]
    if base < int(pcfg.get("daily_max", 3)) and len(resolved) >= 3 \
            and all(e.get("replied") for e in resolved):
        return int(pcfg.get("daily_max", 3))
    if stats["n"] >= 5:
        if stats["rate"] >= 0.4:
            return base
        return min(base, 4) if stats["rate"] >= 0.2 else 2
    return base


def sent_last_24h(st, now):
    return sum(1 for e in st.get("log", [])
               if not e.get("voided")
               and now - float(e.get("ts", 0)) < 86400)


def _last_reply_ts():
    recs = read_jsonl(RECORDS_FILE)
    return float(recs[-1].get("ts", 0)) if recs else 0.0


def user_profile():
    """从他最近 14 天的消息学作息：活跃小时、每天首/末条消息的中位时刻。
    样本不足（<20 条或 <5 天）返回 None，用 config 的固定时段。"""
    cutoff = time.time() - 14 * 86400
    rows = [h for h in read_jsonl(HISTORY_FILE)
            if h.get("role") == "user" and float(h.get("ts", 0)) >= cutoff]
    if len(rows) < 20:
        return None
    by_day = {}
    hours = {}
    for h in rows:
        dt = datetime.fromtimestamp(float(h["ts"]), tz())
        by_day.setdefault(dt.date(), []).append(dt.hour + dt.minute / 60.0)
        hours[dt.hour] = hours.get(dt.hour, 0) + 1
    if len(by_day) < 5:
        return None
    firsts = sorted(min(v) for v in by_day.values())
    lasts = sorted(max(v) for v in by_day.values())
    total = sum(hours.values())
    active = {h for h, n in hours.items() if n >= max(2, total * 0.06)}
    return {"first_med": firsts[len(firsts) // 2],
            "last_med": lasts[len(lasts) // 2],
            "active_hours": active, "days": len(by_day), "total": total}


def _hhmm(minutes):
    minutes = int(max(0, min(1439, minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def slot_window(name, slot, local, profile):
    """时段窗口（分钟数起止）：有他的作息档案时，早安跟着他起床、晚安跟着
    他睡前走；周末早安再推迟 1 小时。没档案用 config 的固定窗口。"""
    w = slot.get("window") or ["00:00", "00:00"]
    start = int(w[0][:2]) * 60 + int(w[0][3:5])
    end = int(w[1][:2]) * 60 + int(w[1][3:5])
    if profile:
        if name == "morning":
            start = int(max(7.0, min(10.0, profile["first_med"] - 0.5)) * 60)
            if local.weekday() >= 5:
                start += 60
            end = start + 120
        elif name == "goodnight":
            start = int(max(21.5, min(23.4, profile["last_med"] - 1.0)) * 60)
            end = min(1439, start + 70)
    return start, end


def _peer_id():
    """收件人 id：从最近一条 inbox 或已发 outbox 里取（不读 session）。"""
    try:
        files = sorted((os.path.join(INBOX, f) for f in os.listdir(INBOX)
                        if f.endswith(".json")), key=os.path.getmtime)
        for path in reversed(files):
            m = read_json(path, {}) or {}
            if m.get("from_user_id"):
                return m["from_user_id"]
    except OSError:
        pass
    return ""


def proactive_mode(pcfg=None):
    """主动引擎模式：config.proactive.mode == "organic" 且 life.py 可用时
    为 organic，其余一律 fixed（缺省兼容旧行为）。"""
    if pcfg is None:
        pcfg = cfg().get("proactive") or {}
    if _life is not None and str(pcfg.get("mode") or "fixed") == "organic":
        return "organic"
    return "fixed"


def _organic_pick(st, pcfg, now, local, ctx, mood=None):
    """有机模式触发判定（只在全部安全闸已通过、且无到期 followup 时调用）。

    按今日生活计划（life.ensure_today）依次看：
    1. 计划锚点：未跳过、已到点且在追赶窗（默认 120 分钟）内的锚点 →
       以其 intent（morning/lunch/evening/goodnight）触发；过窗未发的
       锚点直接作废（像真人：忙过了就不会补发一句迟到的早安）。
    2. 生活事件：share_at 已到、未分享过的事件 → impulse，about 为事件
       本身；每日事件分享上限 organic.event_daily_max（默认 3）。
    3. 长静默关怀：主人静默超过今日采样阈值（计划里的
       silence_threshold_h，默认在 2.5-6 小时内采样）且在可打扰时段内，
       按 silence_p 掷一次（当日只掷一次）。
    4. 自发小分享：低概率（organic.spontaneous_p，默认 0.02/tick）、
       主人已静默 ≥0.5 小时、且在他大概率在的时段。
    返回 {"intent","about","trigger"} 或 {"reason"}；命中/作废的锚点与
    事件记入 st["organic"]["fired"] 并落盘（decide 在锁内执行）。"""
    oc = pcfg.get("organic") or {}
    if mood is None:
        mood = {"streak": 0, "factor": 1.0, "label": ""}
    streak = int(mood.get("streak", 0) or 0)
    factor = float(mood.get("factor", 1.0) or 1.0)
    plan = _life.ensure_today(now)
    today = plan.get("date") or local.date().isoformat()
    org = st.get("organic")
    if not isinstance(org, dict) or org.get("date") != today:
        # quiet_rare_dates 是跨日滚动 7 天的账，不能随当日状态一起清掉
        keep = (org or {}).get("quiet_rare_dates") or [] \
            if isinstance(org, dict) else []
        org = {"date": today, "fired": [], "silence_date": None,
               "quiet_rare_dates": keep, "held": {}}
        st["organic"] = org
    fired = org.setdefault("fired", [])
    org.setdefault("held", {})
    changed = False

    def oc_f(key, default):
        try:
            return float(oc.get(key, default))
        except (TypeError, ValueError):
            return default

    # 有机间隔：mark_sent 已按采样间隔写好 next_allowed_at，这里只认它
    nxt = float(st.get("next_allowed_at") or 0)
    if nxt and now < nxt:
        return {"reason": "距下一次可主动的时间还没到"}
    catchup = oc_f("moment_catchup_min", 120.0) * 60
    # 1) 计划锚点
    for m in plan.get("moments") or []:
        mid = str(m.get("id") or "")
        if not mid or mid in fired:
            continue
        mts = float(m.get("ts", 0) or 0)
        if now > mts + catchup:
            fired.append(mid)  # 过窗作废，不补发
            changed = True
            continue
        if m.get("skip") or now < mts:
            continue
        # 情绪调制（锚点犹豫）：被晾攒到 2 条以上，锚点先犹豫半小时
        # 再发——只犹豫一次、且追赶窗还够时才犹豫，临近过窗照常发。
        # 这是延迟不是拦截：她最后还是会出声。
        held = org.setdefault("held", {})
        if streak >= 2 and mid not in held \
                and mts + catchup - now >= 40 * 60:
            held[mid] = now + 30 * 60
            changed = True
            continue
        if now < float(held.get(mid, 0) or 0):
            continue
        fired.append(mid)
        changed = True
        if changed:
            save_state(st)
        return {"intent": str(m.get("intent") or "impulse"), "about": "",
                "trigger": {"type": "plan", "moment": m.get("kind"),
                            "label": f"今日计划锚点 {m.get('time', '')}"}}
    # 2) 生活事件分享
    evt_max = int(oc_f("event_daily_max", 3))
    evt_done = sum(1 for f in fired if f.startswith(f"{today}:evt"))
    if evt_done < evt_max:
        for e in plan.get("events") or []:
            eid = str(e.get("id") or "")
            if not eid or eid in fired:
                continue
            if now >= float(e.get("share_at", 0) or 0):
                exciting = float(e.get("excitement") or 0) >= 0.75
                # 情绪调制（事件犹豫与冲淡）：普通事件在被晾时按
                # factor 犹豫（不消耗，下个 tick 再掷）；兴奋事件
                # （excitement 高）不犹豫，分享出去后 streak 冲淡 1。
                if streak >= 2 and not exciting \
                        and random.random() > factor:
                    continue
                fired.append(eid)
                if exciting and streak > 0:
                    mst = st.get("mood")
                    if isinstance(mst, dict):
                        mst["streak"] = max(
                            0, int(mst.get("streak", 0) or 0) - 1)
                save_state(st)
                return {"intent": "impulse", "about": str(e.get("text") or ""),
                        "trigger": {"type": "life_event",
                                    "event": e.get("text"),
                                    "event_kind": e.get("kind"),
                                    "who": e.get("who")}}
    # 3) 长静默关怀（阈值按日采样，当日只掷一次）
    user_age_h = (now - float(ctx.get("received_at", now))) / 3600.0
    thr = float(plan.get("silence_threshold_h") or 4.0)
    if (org.get("silence_date") != today and user_age_h >= thr
            and silence_window(pcfg)[0] <= local.hour < silence_window(pcfg)[1]):
        org["silence_date"] = today
        hit = random.random() < silence_p(pcfg) * factor
        save_state(st)
        if hit:
            return {"intent": "silence", "about": "",
                    "trigger": {"type": "silence_care",
                                "threshold_h": thr}}
        # 未命中不直接收尾：下面还有轻触/自发分享的机会
    # 3.5) 轻触：被晾攒到 4 条以上、且超过他通常回复间隔的 1.5 倍还没
    # 动静时，偶尔（organic.light_touch_p，默认 0.02/tick）轻轻碰一下：
    # 内容取今日已到点的生活事件或当前日程，不追问；仍受最上面的有机
    # 间隔约束。这是低概率通道，不是任何意义上的停发补偿闸。
    if streak >= 4:
        med = reply_stats(st).get("median_latency")
        overdue = (user_age_h * 3600 >= float(med) * 1.5) if med \
            else (user_age_h >= thr)
        prof0 = _life.owner_profile(now)
        act0 = prof0.get("active_hours") or []
        ok_hour = (local.hour in act0) \
            if act0 and not prof0.get("insufficient") \
            else (silence_window(pcfg)[0] <= local.hour
                  < silence_window(pcfg)[1])
        if overdue and ok_hour \
                and random.random() < oc_f("light_touch_p", 0.02):
            done = [e for e in plan.get("events") or []
                    if float(e.get("share_at", 0) or 0) <= now]
            about = str(done[-1].get("text") or "") if done \
                else _life.current_block(plan, now)
            return {"intent": "impulse", "about": about,
                    "trigger": {"type": "light_touch", "streak": streak}}
    # 4) 自发小分享
    prof = _life.owner_profile(now)
    active = prof.get("active_hours") or []
    active_ok = (local.hour in active) if active and not prof.get("insufficient") \
        else (silence_window(pcfg)[0] <= local.hour < silence_window(pcfg)[1])
    if (active_ok and user_age_h >= 0.5
            and random.random() < oc_f("spontaneous_p", 0.02)):
        return {"intent": "impulse", "about": "",
                "trigger": {"type": "spontaneous"}}
    if changed:
        save_state(st)
    return {"reason": "本轮没有触发"}


def _quiet_rare_ok(st, pcfg, now, local):
    """静默时段的「稀有例外」判定（仅 organic 模式、默认关）：真人偶尔
    也会在深夜突然想起一句话，但这必须是极少数。开启需显式配置
    organic.quiet_rare.enabled=true；命中还要求近 7 天用过的例外次数
    小于 max_per_week（默认 1）。判定只掷骰与查账，不落盘——真正记账
    在 decide 确定 due 后做，避免没发出去也占名额。"""
    if proactive_mode(pcfg) != "organic":
        return False
    qr = (pcfg.get("organic") or {}).get("quiet_rare") or {}
    if qr.get("enabled", False) is not True:
        return False
    org = st.get("organic") or {}
    dates = org.get("quiet_rare_dates") or []
    today_ord = local.date().toordinal()
    used = sum(1 for d in dates
               if isinstance(d, str) and d and
               today_ord - datetime.fromisoformat(d).date().toordinal() < 7) \
        if dates else 0
    try:
        max_week = int(qr.get("max_per_week", 1))
        p = float(qr.get("p", 0.05))
    except (TypeError, ValueError):
        return False
    return used < max_week and random.random() < p


def decide(now=None):
    """返回 dict：{due, reason} 或 {due:True, intent, about, brief, ...}。"""
    now = now or time.time()
    pcfg = cfg().get("proactive") or {}
    st = _roll_day(load_state(), datetime.now(tz()).date().isoformat()
                   if now is None else
                   datetime.fromtimestamp(now, tz()).date().isoformat())
    today = st["date"]
    # followup 过期清理：约定的时间过去太久（默认 48 小时）就不再提，免得迟到好几天的关心
    rhythm0 = pcfg.get("rhythm") or {}
    expire_h = float(rhythm0.get("followup_expire_hours", 48))
    fresh = [f for f in st.get("followups", [])
             if now - float(f.get("after_ts", now)) <= expire_h * 3600]
    if len(fresh) != len(st.get("followups", [])):
        st["followups"] = fresh
    save_state(st)
    local = datetime.fromtimestamp(now, tz())

    def no(reason):
        return {"due": False, "reason": reason}

    if not pcfg.get("enabled", True) or not st.get("enabled", True):
        return no("主动消息已关闭")
    if os.path.exists(PAUSED_FILE):
        return no("已暂停")
    if os.path.exists(NEEDS_RELOGIN_FILE):
        return no("登录已过期")
    ctx = read_json(CONTEXT_FILE, {}) or {}
    if not ctx.get("context_token") or ctx.get("stale"):
        return no("context 不可用")
    ctx_age_h = (now - float(ctx.get("received_at", now))) / 3600.0
    if ctx_age_h > float(pcfg.get("max_context_age_hours", 24)):
        return no(f"context 太旧（{ctx_age_h:.1f} 小时）")
    # 先对账：被发送端作废的主动消息没送达，先从台账里标出来再结算
    if reconcile_voided(st):
        save_state(st)
    # 先结算他对既往主动消息的回应（喂回应率统计、浮动上限与情绪模型）
    if resolve_replies(st, now, float(ctx.get("received_at", 0)),
                       window_h=float(rhythm0.get("reply_window_hours", 12)),
                       cooldown_h=float(rhythm0.get("cooldown_hours", 24))):
        save_state(st)
    # 情绪模型同步：streak 只调制概率/间隔/简报，永不拦截（见 mood_state）
    mood = mood_state(st, today)
    save_state(st)
    # awaiting 解锁：上一条主动已结算（回了或被晾超过窗口），等待标记自动失效，
    # 否则一条被晾的消息会让她从此永久沉默（冷却另有门控，不靠这里防打扰）
    if st.get("awaiting_reply"):
        log = st.get("log", [])
        last = log[-1] if log else None
        settled = (last is not None and last.get("replied") is not None) or \
            (st.get("last_sent_at") and not log
             and now - float(st["last_sent_at"]) > 12 * 3600)
        if settled:
            st["awaiting_reply"] = False
            save_state(st)
    # 到期 followup 提前判定：是他自己提过的事，优先级高于随机问候——
    # 不被随机间隔误伤（静默时段等礼貌门控仍生效）；2026-10-27 起它与
    # 条数上限之间也不再有豁免耦合：daily_max 填数字时上限对所有
    # intent 一致生效，缺省（null）时本就没有上限这道闸。
    due_follow_pre = [f for f in st.get("followups", [])
                      if float(f.get("after_ts", 0)) <= now]
    q0, q1 = (pcfg.get("quiet_hours") or [0, 7])[:2]
    quiet_rare = False
    if q0 <= local.hour < q1:
        # 静默时段是硬闸：默认任何模式都不许过。唯一例外是 organic 模式
        # 显式开启的 quiet_rare（默认关、周上限 1 次、低概率），且例外只
        # 豁免这一道闸——后面的其余门控（context/活动避让/填数字时的
        # 总量上限）一律照旧。
        if _quiet_rare_ok(st, pcfg, now, local):
            quiet_rare = True
        else:
            return no("静默时段")
    cap = effective_cap(st, pcfg, today)
    if cap is not None and sent_last_24h(st, now) >= cap:
        return no(f"滚动 24 小时已达上限 {cap} 条")
    gap_blocked_until = 0.0
    if st.get("last_sent_at") and not due_follow_pre:
        nxt = float(st.get("next_allowed_at") or 0)
        if nxt and now < nxt:
            # 先不判死：随机间隔只是「别太密」的节拍，不是硬线。时段问候
            # 有窗口——若唯一挡路的是这道间隔、且等它走完窗口就关了，这句
            # 问候注定整窗错过（2026-10-22 晚 evening 实证：followup 18:42
            # 记账把 next_allowed_at 推到 20:00:41，窗口 20:00 结束）。
            # 是否豁免留到触发段按时段窗口尾段判据定；silence/impulse
            # 不享受豁免，最后仍会被这道间隔挡住。
            gap_blocked_until = nxt
        if not nxt and now - float(st["last_sent_at"]) < \
                float(pcfg.get("min_gap_hours", 3)) * 3600:
            return no("距上次主动太近")
    # （2026-10-27 撤除：未回复并存上限与未回复熔断两道闸。被晾的条数
    # 从此只进情绪模型当调制信号——概率衰减、间隔拉长、简报带情绪，
    # 见 mood_state 与 _organic_pick；config 里的 unresolved_max /
    # unreplied_stop 键自此不再被读取，老配置带着它们也无副作用。）
    # 「主动话题刚聊完」标记：他回过上一条主动、且他最新一条消息还在
    # 1 小时内。2026-10-23 复盘 P1 之前它在这里全局拦截一切 intent
    # （早期版本此门曾全局拦截一切 intent），与 impulse 口径
    # （他沉默 ≥0.5h 即可 roll）效果冲突；P1 起只作用于时段问候——
    # 在下方时段循环里与 45 分钟避让并列判断，impulse / silence /
    # followup 不再被它拦截（其余全域门控一律照旧）。它与时段循环里
    # 的 45 分钟避让是两条不同的机制：那条看 last_activity（并入她
    # 的回复与主动发送）、窗口 45 分钟；这条只看「他回过主动之后的
    # 新消息」、窗口 1 小时。
    topic_just_done = bool(
        st.get("last_sent_at")
        and float(ctx.get("received_at", 0)) > float(st["last_sent_at"])
        and now - float(ctx.get("received_at", 0)) < 3600)
    last_activity = max(float(ctx.get("received_at", 0)), _last_reply_ts(),
                        float(st.get("last_sent_at") or 0))
    if last_activity and now - last_activity < float(pcfg.get("active_skip_min", 30)) * 60:
        return no("刚刚还在聊")

    # ---- 触发（优先级：followup > 时段 > 长静默；organic 模式见下） ----
    intent = None
    about = ""
    organic_trigger = {}
    profile = user_profile()
    due_follow = due_follow_pre
    _org = None
    if not due_follow and not quiet_rare and proactive_mode(pcfg) == "organic":
        # 有机模式：触发完全由今日生活计划 + 生活事件 + 采样阈值决定，
        # 不再走固定时段窗/固定概率的排班（下方 else 分支）。安全闸都在
        # 本节之前、与 fixed 共用，未命中直接按其 reason 收尾。
        _org = _organic_pick(st, pcfg, now, local, ctx, mood=mood)
        if not _org.get("intent"):
            return no(_org.get("reason") or "本轮没有触发")
    if due_follow:
        due_follow.sort(key=lambda f: float(f.get("after_ts", 0)))
        intent, about = "followup", str(due_follow[0].get("about", ""))
    elif quiet_rare:
        intent, about = "impulse", ""
        organic_trigger = {"type": "quiet_rare"}
    elif _org is not None:
        intent, about = _org["intent"], _org.get("about", "")
        organic_trigger = _org.get("trigger") or {}
    else:
        slots = pcfg.get("slots") or {}
        now_min = local.hour * 60 + local.minute
        for name, slot in slots.items():
            s_min, e_min = slot_window(name, slot, local, profile)
            if not (s_min <= now_min <= e_min):
                continue
            if gap_blocked_until:
                # 时段尾段豁免判据：next_allowed_at 落在本时段窗口结束之后
                # （等间隔走完窗口已关）才豁免；间隔在窗口内就能走完的不
                # 豁免，并且不消耗今天的 roll——等下个 tick 间隔走完再来
                # 正常 roll。其余门控（45 分钟刚聊完等）一律照旧。
                day_start = datetime(local.year, local.month, local.day,
                                     tzinfo=tz()).timestamp()
                if gap_blocked_until <= day_start + e_min * 60:
                    continue
            if topic_just_done:
                continue  # 主动话题刚聊完 1 小时内不补发时段问候（P1：此门控只管时段档，不消耗 roll）
            if now - last_activity < 2700:
                continue  # 刚聊完不到 45 分钟，不补发时段问候（没话找话感）
            roll = (st.get("slot_rolls") or {}).get(name)
            if roll and roll.get("date") == today:
                # 自愈：命中 roll 超过 20 分钟却查不到今日同 intent 的发送记录，
                # 说明那一轮的发送链路没走完（cron 运行时被挤压/中断），roll 被白白
                # 消耗——删掉重 roll，只在窗口内生效（出了窗口本循环根本进不来）。
                # 有发送记录（含作废）就算完成，绝不重发；未命中过的 miss 不重 roll。
                done_today = any(
                    e.get("intent") == name
                    and datetime.fromtimestamp(float(e.get("ts", 0)), tz()).date().isoformat() == today
                    for e in st.get("log", []))
                if not (roll.get("hit") and not done_today
                        and now - float(roll.get("rolled_at") or now) > 1200):
                    continue  # 今天已 roll 过（命中未发也不重复 roll）
            # 连败保底/连中降频：纯概率会造成一周不说早安、或连着五天晚安的
            # 扎堆与干旱；连败 2 天今天必中，连中 2 天今天概率减半
            streaks = st.setdefault("slot_streaks", {})
            sk = streaks.setdefault(name, {"hit": 0, "miss": 0})
            if int(sk.get("miss", 0)) >= 2:
                hit = True
                p_eff = 1.0  # 连败保底必中，有效概率即 1（roll 落盘记账用）
            else:
                p_eff = float(slot.get("p", 0.3))
                if int(sk.get("hit", 0)) >= 2:
                    p_eff *= 0.5
                # 情绪调制：被晾越多时段问候越收敛（保底必中不受调制）
                p_eff *= mood["factor"]
                hit = random.random() < p_eff
            if hit:
                sk["hit"] = int(sk.get("hit", 0)) + 1
                sk["miss"] = 0
            else:
                sk["miss"] = int(sk.get("miss", 0)) + 1
                sk["hit"] = 0
            st.setdefault("slot_rolls", {})[name] = {"date": today, "hit": hit,
                                                     "rolled_at": now}
            save_state(st)
            append_roll({"ts": now, "date": today, "slot": name,
                         "hit": hit, "p": p_eff})
            if hit:
                intent = name
                break
        if not intent and gap_blocked_until:
            # 没有时段命中（含不在任何窗口、或被尾段判据挡下）：随机间隔
            # 照旧生效，silence/impulse 不享受时段尾段豁免
            return no("距下一次可主动的时间还没到")
        if not intent:
            # 长静默：傍晚的沉默更值得出现（3 小时），其余时段用 config 的 5 小时；
            # 有作息档案时只在他通常活跃的小时出现
            rhythm = pcfg.get("rhythm") or {}
            if 17 <= local.hour < 22:
                silence_h = float(rhythm.get("silence_evening_hours", 3))
            else:
                silence_h = float(pcfg.get("silence_hours", 5))
            if local.weekday() >= 5:
                # 周末白天他通常更闲，静默阈值跟傍晚看齐
                silence_h = min(silence_h, float(rhythm.get("silence_evening_hours", 3)))
            user_age_h = (now - float(ctx.get("received_at", now))) / 3600.0
            roll = (st.get("slot_rolls") or {}).get("silence")
            already = bool(roll and roll.get("date") == today)
            if already and roll.get("hit"):
                # 同一自愈逻辑：命中 roll 超 20 分钟无发送记录 → 不算 roll 过
                done_today = any(
                    e.get("intent") == "silence"
                    and datetime.fromtimestamp(float(e.get("ts", 0)), tz()).date().isoformat() == today
                    for e in st.get("log", []))
                if not done_today and now - float(roll.get("rolled_at") or now) > 1200:
                    already = False
                    roll = None
                    st["slot_rolls"].pop("silence", None)
            elif already:
                # P2 当日第二次机会（2026-10-23 复盘乙类提案，用户批准）：
                # 首次 roll 未命中后，静默再持续满 1 小时，允许当日第二
                # 次 roll（同概率 SILENCE_P）；attempts 记当日未命中的
                # roll 次数（旧格式记录无此字段、按 1 次计），未命中每日
                # 至多 2 次，绝不出现第三次未命中 roll。命中后的自愈
                # （上分支删 roll 重 roll）对第二次同样适用，且自愈重
                # roll 不计入 attempts（2026-10-24 用户定甲口径：自愈
                # 只在生成/发送链路持续故障时连环触发，任一次送达即
                # 停）；长静默分档（昨天被晾才隔日）不经这里，仍由
                # 下方 sil_recent 判定，语义不变。
                if int(roll.get("attempts", 1)) < 2 \
                        and now - float(roll.get("rolled_at") or now) >= 3600:
                    already = False
            active_ok = (not profile) or (local.hour in profile["active_hours"])
            # 长静默防重复：昨天那条"好久没理我"若被晾了，今天不再用同一招；
            # 昨天被接住了，今天可以照常出现
            sil_recent = False
            for e in reversed(st.get("log", [])):
                if e.get("intent") == "silence":
                    d = datetime.fromtimestamp(float(e.get("ts", 0)), tz()).date()
                    sil_recent = (d.toordinal() == local.date().toordinal() - 1
                                  and e.get("replied") is False)
                    break
            if (not already and not sil_recent and user_age_h >= silence_h and active_ok
                    and silence_window(pcfg)[0] <= local.hour < silence_window(pcfg)[1]):
                hit = random.random() < silence_p(pcfg) * mood["factor"]
                prev_attempts = int(roll.get("attempts", 1)) \
                    if roll and roll.get("date") == today else 0
                st.setdefault("slot_rolls", {})["silence"] = {
                    "date": today, "hit": hit, "rolled_at": now,
                    "attempts": prev_attempts + 1}
                save_state(st)
                append_roll({"ts": now, "date": today, "slot": "silence",
                             "hit": hit, "p": silence_p(pcfg) * mood["factor"],
                             "attempts": prev_attempts + 1})
                if hit:
                    intent = "silence"
        if not intent:
            # 随机小心情：脱离时段网格的随手分享。活跃时段每个 tick 小概率
            # roll 一次（不落盘，未中下个 tick 再 roll），每天最多 impulse_daily_max 次
            rhythm = pcfg.get("rhythm") or {}
            user_age_h = (now - float(ctx.get("received_at", now))) / 3600.0
            active_ok = (not profile) or (local.hour in profile["active_hours"])
            imp_today = sum(1 for e in st.get("log", [])
                            if e.get("intent") == "impulse"
                            and datetime.fromtimestamp(float(e.get("ts", 0)), tz())
                            .date().isoformat() == today)
            if (imp_today < int(rhythm.get("impulse_daily_max", 1)) and active_ok
                    and user_age_h >= 0.5
                    and silence_window(pcfg)[0] <= local.hour < silence_window(pcfg)[1]
                    and random.random() < float(rhythm.get("impulse_p", 0.03)) * mood["factor"]):
                intent = "impulse"
    if not intent:
        return no("本轮没有触发")

    life = read_json(LIFE_FILE, {}) or {}
    if not life:
        # 生活设定回落来源：facts.json 的 life 字段（persona/facts 驱动）
        facts_doc = read_json(FACTS_FILE, {}) or {}
        life = facts_doc.get("life") or {}
    organic_brief = None
    if organic_trigger and _life is not None:
        # 有机模式的生成契约：给生成层的不再是「时段标签」，而是触发缘由
        # （trigger：计划锚点/生活事件/静默关怀/自发分享）+ 她当下的生活
        # 状态摘要（life_state）+ 今日生活计划的事件与心情；life_schedule /
        # life_events / mood_now 等旧键同步改从今日计划取，保持字段兼容。
        plan = _life.ensure_today(now)
        block = _life.current_block(plan, now)
        organic_brief = {
            "trigger": organic_trigger,
            "life_state": {
                "mood": plan.get("mood", ""), "busy_level": plan.get("busy_level", ""),
                "day_kind": plan.get("day_kind", ""), "now_block": block,
                "mood_carry": plan.get("mood_carry") or "",
                "mood_streak": {"n": mood["streak"], "label": mood["label"]},
                "events_done": [e.get("text", "") for e in plan.get("events") or []
                                if float(e.get("share_at", 0) or 0) <= now],
                "owner_adapt": plan.get("owner_adapt") or {}},
            "schedule": block,
            "events": [e.get("text", "") for e in plan.get("events") or []],
            "mood_now": plan.get("mood", ""), "busy_level": plan.get("busy_level", "")}
    hist = read_jsonl(HISTORY_FILE)[-4:]
    # 发送时刻再抹一层随机：followup 用小抖动，时段/静默类用 0-10 分钟
    # 抖动，避免总是踩着整点网格出现，同时不再把发送窗口拉得太长——
    # 窗口内他只要先发来一句话，bridge 发送前复查就以「已有新对话」作废
    # （实测 4 条作废的主动抖动全在 10-25 分钟，送达的全在 8 分钟内；
    # 2026-10-22 当天早安与 14:21 的 impulse 均因此作废）。impulse 例外：
    # 它本就是「随手分享」，只留 0-3 分钟的象征性抖动。
    if intent == "followup":
        jrange = pcfg.get("send_jitter_min") or [0, 10]
    elif intent == "impulse":
        jrange = [0, 3]
    else:
        cfg_jrange = (pcfg.get("rhythm") or {}).get("jitter_min") or [0, 10]
        jitter_hi = min(int(cfg_jrange[1]), 10)
        jrange = [min(int(cfg_jrange[0]), jitter_hi), jitter_hi]
    jitter = random.randint(int(jrange[0]), int(jrange[1]))
    # 偶尔二连发：像真人想起第二句，隔 2-5 分钟补一条（只限闲聊类意图）
    rhythm = pcfg.get("rhythm") or {}
    double = bool(intent in ("silence", "evening", "lunch")
                  and random.random() < float(rhythm.get("double_p", 0.18)))
    # 形式裁定：photo_p/video_p 是两路都可达时的相对权重（同为 0 时
    # 回落文字）。可达集：照片 morning/evening/silence，视频
    # evening/silence/impulse；都不在（lunch/goodnight/followup）保持
    # 文字，followup 的形式由 pending_form 契约决定、不走裁定。
    # 主体：从 config 的 proactive.photo_subjects / video_subjects
    # （默认 ["本人出镜", "生活场景"]）按 self_p 权重抽「本人出镜」，
    # 未命中在余下主体里按日期轮换。台账计数只作统计不拦截。
    form = "text"
    photo_subject = ""
    video_subject = ""
    photo_ok = intent in ("morning", "evening", "silence")
    video_ok = intent in ("evening", "silence", "impulse")
    wp = float(rhythm.get("photo_p", 1.0)) if photo_ok else 0.0
    wv = float(rhythm.get("video_p", 1.0)) if video_ok else 0.0
    if quiet_rare:
        wp = wv = 0.0  # 深夜稀有例外只发文字，不半夜发图/视频
    if wp + wv > 0:
        if wp and wv:
            form = "photo" if random.random() * (wp + wv) < wp else "video"
        elif wp:
            form = "photo"
        else:
            form = "video"

    def _subject(kind):
        subjects = pcfg.get(f"{kind}_subjects") or ["本人出镜", "生活场景"]
        subjects = [str(s) for s in subjects] or ["生活场景"]
        if random.random() < float(rhythm.get(f"{kind}_self_p", 0.7)):
            return subjects[0]
        rest = subjects[1:] or subjects
        return rest[local.day % len(rest)]

    if form == "photo":
        photo_subject = _subject("photo")
        double = False
    elif form == "video":
        video_subject = _subject("video")
        double = False
    if quiet_rare:
        org = st.get("organic") or {}
        dates = list(org.get("quiet_rare_dates") or [])
        dates.append(today)
        org["quiet_rare_dates"] = dates[-8:]
        st["organic"] = org
        save_state(st)
    flavor = pick_flavor(st, intent)
    if flavor:
        st["pending_flavor"] = flavor
        save_state(st)
    if form != "text":
        st["pending_form"] = form
        save_state(st)
    return {"due": True, "intent": intent, "about": about, "double": double,
            "form": form, "photo_subject": photo_subject,
            "video_subject": video_subject, "flavor": flavor,
            "to_user_id": _peer_id(),
            "context_token": ctx.get("context_token", ""),
            "send_after": now + jitter * 60,
            "brief": {
                "life_schedule": organic_brief["schedule"] if organic_brief
                                 else life.get("schedule", ""),
                "life_events": organic_brief["events"] if organic_brief
                               else life.get("small_events", []),
                "used_events_today": used_events_today(st, today),
                "mood_now": organic_brief["mood_now"] if organic_brief
                            else life.get("mood_now", ""),
                "busy_level": organic_brief["busy_level"] if organic_brief
                              else life.get("busy_level", ""),
                "trigger": organic_brief["trigger"] if organic_brief else {},
                "life_state": organic_brief["life_state"] if organic_brief else {},
                "recent_history": [{"role": h.get("role", ""),
                                    "text": str(h.get("text", ""))[:40]}
                                   for h in hist]}}


_FORM_RANK = {"text": 0, "photo": 1, "video": 2}


def _delivered_form(message_id, intended):
    """E3 埋点：按 outbox 实际条目推本次的实际送达形态——先看 sent、
    再看 pending（记账时可能尚在发送队列）；组内有媒体条按 media_type
    定形态（video 优先），只有文字条记 text。组还没落盘（记账先行、
    无从核对）时按 intended 记，不虚构降级。"""
    if not message_id:
        return intended
    mid = str(message_id)
    for d in (SENT_DIR, PENDING_DIR):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        media = None
        has_text = False
        for fn in names:
            stem = fn[:-5] if fn.endswith(".json") else fn
            if stem != mid and not stem.startswith(mid + "-"):
                continue
            e = read_json(os.path.join(d, fn), {}) or {}
            if str(e.get("message_id", "")) != mid:
                continue
            mt = e.get("media_type")
            if mt == "video":
                media = "video"
            elif mt == "image" and media != "video":
                media = "photo"
            elif (e.get("text") or "").strip():
                has_text = True
        if media or has_text:
            return media or "text"
    return intended


def mark_sent(intent, about="", event="", message_id=""):
    now = time.time()
    today = datetime.now(tz()).date().isoformat()
    st = _roll_day(load_state(), today)
    st["sent_count"] = int(st.get("sent_count", 0)) + 1
    st["last_sent_at"] = now
    st["last_intent"] = intent
    st["awaiting_reply"] = True
    # 下一次可主动的时间随机化（间隔取 config 的 rhythm.gap_hours，
    # 2026-10-22 用户定版 [0.4, 1.0] 小时），避免节拍器感。
    # 注：2026-10-21 曾有「他一贯秒回则间隔收紧到 0.75-1.5 小时」的浮动
    # 分支，那是基线还是 2.5-4.5 小时时定的；基线改为 [0.4, 1.0] 后，
    # 该分支反而把间隔拉长到定版之外（与原意反转），2026-10-23 复盘移除。
    _pcfg = cfg().get("proactive") or {}
    rhythm = _pcfg.get("rhythm") or {}
    if proactive_mode(_pcfg) == "organic":
        # 有机间隔：对数正态采样——多数时候一两个小时，偶尔很快（像想起
        # 第二句）、偶尔大半天安静；只有下限是硬的（采样区间下界），
        # 没有固定节拍。区间由 organic.gap_sample_min_h/max_h 配置。
        oc = _pcfg.get("organic") or {}

        def _gf(key, default):
            try:
                return float(oc.get(key, default))
            except (TypeError, ValueError):
                return default

        lo = max(0.05, _gf("gap_sample_min_h", 0.3))
        hi = max(lo + 0.1, _gf("gap_sample_max_h", 5.0))
        gap_h = math.exp(random.uniform(math.log(lo), math.log(hi)))
        # 情绪调制（间隔拉长）：发送前台账尾部连续未回复（含待结算）
        # 每多一条，这次间隔 ×1.5 累加，封顶 ×3.5；他都回了就是 ×1。
        # 与 resolve_replies 同口径判「已回复」：context 收到时间晚于
        # 该条发送时刻即算回过。只是拉长，永不构成停发。
        _ctx = read_json(CONTEXT_FILE, {}) or {}
        _ctx_at = float(_ctx.get("received_at", 0) or 0)
        _prev = 0
        for _e in reversed(st.get("log", [])):
            if _e.get("voided"):
                continue
            if _e.get("replied") is True:
                break
            if _ctx_at and _ctx_at > float(_e.get("ts", 0) or 0):
                break
            _prev += 1
        gap_h *= min(1.0 + 0.5 * _prev, 3.5)
        st["next_allowed_at"] = now + gap_h * 3600
    else:
        gap = rhythm.get("gap_hours") or [2.5, 4.5]
        st["next_allowed_at"] = now + random.uniform(float(gap[0]), float(gap[1])) * 3600
    if intent == "silence":
        st["last_silence_date"] = today
    if intent == "impulse":
        st["last_impulse_date"] = today
    # 台账带上本次文案的事件签名（--event）与 followup 话题（--about）：
    # tick 的 brief 会以 used_events_today 回喂，让生成侧避开今日已用事件，
    # 不再出现两条主动复用同一 life 梗（2026-10-22 两条 impulse 同梗实测）。
    entry = {"ts": now, "intent": intent, "replied": None}
    if message_id:
        entry["message_id"] = message_id
    if event:
        entry["event"] = event
    if about:
        entry["about"] = about
    # E3 埋点（2026-10-25 落地）：裁定形态与实际送达形态双落盘，降级
    # 率与分因从此可按周聚合，不再只活在瞬时字段 pending_form 里。
    # intended 取记账当口的 pending_form（decide 裁定值，空 = 文字）；
    # delivered 按 outbox 实际条目推（_delivered_form）；形态降序
    # （video>photo>text）即记 degrade_reason——发送链内可归因的降级
    # 只有媒体失败一类（ctx_stale 走整组作废、不进送达统计；cap 降级
    # 在现行流程不存在）。
    form_intended = st.get("pending_form") or "text"
    form_delivered = _delivered_form(message_id, form_intended)
    entry["form_intended"] = form_intended
    entry["form_delivered"] = form_delivered
    if _FORM_RANK.get(form_delivered, 0) < _FORM_RANK.get(form_intended, 0):
        entry["degrade_reason"] = "media_fail"
    st.setdefault("log", []).append(entry)
    st["log"] = st["log"][-30:]
    if st.get("pending_flavor"):
        st["flavor_history"] = ((st.get("flavor_history") or [])
                                + [st["pending_flavor"]])[-8:]
        st["pending_flavor"] = ""
    if st.get("pending_form") == "video":
        st["last_video_date"] = today
        st["video_sends_today"] = int(st.get("video_sends_today", 0)) + 1
    st["pending_form"] = ""
    if intent == "followup" and about:
        st["followups"] = [f for f in st.get("followups", [])
                           if str(f.get("about", "")) != about]
    save_state(st)
    return st


SAMPLES = {
    "followup": "（接之前的话题）你昨天提的那件事，今天怎么样啦",
    "morning": "早呀，刚醒，你那边天气怎么样",
    "lunch": "午饭时间到，你中午吃的啥",
    "evening": "下班路上，天色挺好看的，拍给你看？——只发文字也可",
    "goodnight": "我先休息啦，你也早点睡，晚安",
    "silence": "一下午没消息，在忙什么呢",
    "impulse": "刚看到一个有意思的视频，分享给你",
}


def main(argv=None):
    p = argparse.ArgumentParser(prog="proactive.py")
    sub = p.add_subparsers(dest="cmd")
    t = sub.add_parser("tick")
    t.add_argument("--now", type=float, default=None)
    s = sub.add_parser("sent")
    s.add_argument("--intent", required=True)
    s.add_argument("--about", default="")
    s.add_argument("--event", default="")
    s.add_argument("--message-id", default="")
    sub.add_parser("dry-run")
    sub.add_parser("status")
    args = p.parse_args(argv)
    if args.cmd == "tick":
        with state_lock():
            print(json.dumps(decide(now=args.now), ensure_ascii=False))
        return 0
    if args.cmd == "sent":
        with state_lock():
            st = mark_sent(args.intent, args.about, args.event,
                           args.message_id)
        print(json.dumps({"ok": True, "sent_count": st["sent_count"],
                          "awaiting_reply": st["awaiting_reply"]},
                         ensure_ascii=False))
        return 0
    if args.cmd == "dry-run":
        with state_lock():
            gate = decide()
        print("当前门控：" + ("可发送" if gate.get("due") else gate.get("reason", "")))
        for k, v in SAMPLES.items():
            print(f"[{k}] 示例：{v}")
        return 0
    if args.cmd == "status":
        with state_lock():
            st = load_state()
            changed = reconcile_voided(st)
            corrected = logged_sent_count(st)
            if int(st.get("sent_count", 0)) != corrected:
                st["sent_count"] = corrected
                changed = True
            if changed:
                save_state(st)
            pcfg = cfg().get("proactive") or {}
            today = datetime.now(tz()).date().isoformat()
            stats = reply_stats(st)
            print(json.dumps({"state": st, "mode": proactive_mode(pcfg),
                              "effective_cap": effective_cap(st, pcfg, today),
                              "sent_last_24h": sent_last_24h(st, time.time()),
                              "day_counts": derive_day_counts(st),
                              "reply_stats": stats}, ensure_ascii=False))
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
