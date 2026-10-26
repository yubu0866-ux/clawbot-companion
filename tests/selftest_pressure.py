#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【压力与长稳】自检（通用版）：/tmp 沙箱 + 假平台桩，绝不碰生产 state/。

覆盖：
- P1 连发合并批次：同用户短时多条入站（顺序批）→ 回复落盘 → 单趟发送，
  全管线时延分布（p50/p95/max）、零丢失/零重复（platform 侧 client_id 唯一、
  history assistant 条数与发送数一致）。
- P2 多轮并发入站：多线程并发 _process_inbound（distinct id），零丢失、
  inbox/history 条数对齐；同 id 并发重投只观测（生产 receiver 单线程顺序）。
- P3 主动+回复交叠：发送排空 pending 的同时生产者并发入站+新增 pending
  （reply/proactive 交错），最终排空、计数对齐、无重复发送。
- P4 state_lock 多进程 hammer（bughunt2 B1 的 8 进程口径加压到 16 进程）：
  bump_unreplied / 通用 RMW / append_jsonl 三路，验不丢更新、行不撕裂。
- P5 队列曲线：pending 积压 n=10/50/100/200/300 单趟 _sender_pass 耗时；
  并以「单趟内 pending 文件 read_json 次数 <= 3n+10」做结构断言（发送成功
  后关-typing 预检若逐条重读后续文件即 O(n^2) 次文件读，见 P5 回归）。
- P6 压后毒化检查：沙箱内全部 *.json 可解析、*.jsonl 逐行 dict 可解析。

运行：TZ=Asia/Shanghai python3 tests/selftest_pressure.py
沙箱自清理（CCB_PRESSURE_KEEP=1 时保留供排查）。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "bridge.py")
USER_ID = "user1@im.wechat"
CTX = "CTXPRESSURE_abcdef1234567890"

RESULTS = []
METRICS = {}


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"（{detail}）" if detail and not cond else ""))


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


TMP = tempfile.mkdtemp(prefix="ccb-pressure-")
os.environ["COMPANION_HOME"] = TMP
for d in ("state", "state/director/done", "inbox", "outbox/pending",
          "outbox/sent", "outbox/failed", "state/media_in"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
_cfg = json.load(open(os.path.join(ROOT, "config.example.json"), encoding="utf-8"))
_cfg["bubble_gap_sec"] = [0, 0]
_cfg["unreplied_warn_at"] = 99999
_cfg["unreplied_stop_at"] = 1000000
json.dump(_cfg, open(os.path.join(TMP, "config.json"), "w"), ensure_ascii=False)

spec = importlib.util.spec_from_file_location("bridge_pressure", BRIDGE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.ensure_dirs()

SESSION = {"bot_token": "TOK", "baseurl": "http://127.0.0.1:1",
           "ilink_bot_id": "bot1@im.bot", "ilink_user_id": USER_ID}

_platform = []
_platform_lock = threading.Lock()


def fake_send(session, to_user_id, text, context_token, client_id=None, log=None):
    time.sleep(0.001)
    with _platform_lock:
        _platform.append({"client_id": client_id, "text": text, "ts": time.time()})
    return {"ret": 0}


mod.send_message = fake_send
mod.send_typing = lambda *a, **k: True
mod.touch_trigger_and_spawn_worker = lambda *a, **k: None


def platform_snapshot():
    with _platform_lock:
        return list(_platform)


def platform_clear():
    with _platform_lock:
        _platform.clear()


def wj(path, data):
    mod.write_json_locked(path, data)


def reset_state():
    for sub in ("outbox/pending", "outbox/sent", "outbox/failed", "inbox"):
        d = os.path.join(TMP, sub)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
    for name in ("history.jsonl", "context_health.jsonl", "probe_inbound.jsonl"):
        p = os.path.join(TMP, "state", name)
        if os.path.exists(p):
            os.remove(p)
    wj(mod.COUNTERS_FILE, {"unreplied": 0})
    wj(mod.CONTEXT_FILE, {"context_token": CTX, "received_at": time.time(), "stale": False})
    wj(mod.PROACTIVE_FILE, {"date": None, "sent_count": 0, "awaiting_reply": False,
                            "slot_rolls": {}, "followups": []})
    platform_clear()


def user_msg(mid, text, ctx=CTX):
    return {"message_id": mid, "from_user_id": USER_ID, "message_type": 1,
            "context_token": ctx, "create_time_ms": int(time.time() * 1000),
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def write_pending(name, mid, text, kind="reply", typing_sec=0.5):
    wj(os.path.join(mod.PENDING, name),
       {"to_user_id": USER_ID, "text": text, "context_token": CTX,
        "message_id": mid, "send_after": 0, "kind": kind,
        "typing_sec": typing_sec, "client_id": "ccb-" + name.replace(".json", "")})


def drain_passes(state=None, max_passes=50):
    state = state if state is not None else {}
    for _ in range(max_passes):
        if not [f for f in os.listdir(mod.PENDING) if f.endswith(".json")]:
            break
        mod._sender_pass(SESSION, USER_ID, [0, 0], state, skip_media=True)
    return state


def history():
    return mod.read_jsonl(mod.HISTORY_FILE)


try:
    # ---------- P1 连发合并批次 ----------
    reset_state()
    N1 = 30
    in_start = {}
    for i in range(N1):
        mid = f"P1-{i:03d}"
        in_start[mid] = time.time()
        mod._process_inbound(SESSION, user_msg(mid, f"连发{i}"), USER_ID, state={})
    for i in range(N1):
        write_pending(f"P1-{i:03d}.json", f"P1-{i:03d}", f"回复{i}")
    drain_passes()
    sent1 = platform_snapshot()
    hist1 = history()
    by_text = {s["text"]: s for s in sent1}
    lat1 = []
    for i in range(N1):
        s = by_text.get(f"回复{i}")
        if s:
            lat1.append(s["ts"] - in_start[f"P1-{i:03d}"])
    METRICS["P1_latency_ms"] = {"p50": round(pct(lat1, 0.50) * 1000, 1),
                                "p95": round(pct(lat1, 0.95) * 1000, 1),
                                "max": round(max(lat1) * 1000, 1) if lat1 else 0}
    check("P1a 连发30条入站零丢失（inbox=30、history user=30）",
          len(os.listdir(mod.INBOX)) == N1
          and sum(1 for h in hist1 if h.get("role") == "user") == N1,
          f"inbox={len(os.listdir(mod.INBOX))} hist={len(hist1)}")
    ids1 = [s["client_id"] for s in sent1]
    check("P1b 回复零丢失零重复（platform=30、client_id 全唯一、history assistant=30）",
          len(sent1) == N1 and len(set(ids1)) == N1
          and sum(1 for h in hist1 if h.get("role") == "assistant") == N1,
          f"sent={len(sent1)} uniq={len(set(ids1))}")
    check("P1c pending 排空、sent 目录=30",
          not [f for f in os.listdir(mod.PENDING) if f.endswith(".json")]
          and len(os.listdir(mod.SENT)) == N1,
          f"sent_dir={len(os.listdir(mod.SENT))}")
    print(f"     P1 全管线时延 ms: {METRICS['P1_latency_ms']}")

    # ---------- P2 多轮并发入站 ----------
    reset_state()
    N2_THREADS, N2_PER = 8, 25
    N2 = N2_THREADS * N2_PER

    def inbound_worker(t):
        for i in range(N2_PER):
            mod._process_inbound(SESSION, user_msg(f"P2-{t}-{i:02d}", f"并发{t}-{i}"),
                                 USER_ID, state={})

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=N2_THREADS) as ex:
        list(ex.map(inbound_worker, range(N2_THREADS)))
    wall2 = time.time() - t0
    hist2 = history()
    n_hist_user2 = sum(1 for h in hist2 if h.get("role") == "user")
    check("P2a 并发入站 8线程x25 零丢失（inbox=200、history user=200、无重复 id）",
          len(os.listdir(mod.INBOX)) == N2 and n_hist_user2 == N2
          and len({h.get("message_id") for h in hist2 if h.get("role") == "user"}) == N2,
          f"inbox={len(os.listdir(mod.INBOX))} hist_user={n_hist_user2} wall={wall2:.2f}s")
    METRICS["P2_wall_s"] = round(wall2, 2)
    reset_state()
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: mod._process_inbound(
            SESSION, user_msg("P2-DUP", "重投"), USER_ID, state={}), range(8)))
    dup_user = sum(1 for h in history() if h.get("message_id") == "P2-DUP")
    METRICS["P2_same_id_concurrent_history_user"] = dup_user
    check("P2b 同 id 8 线程并发重投零重复（claim 去重：history user=1、inbox=1、无残留 .claim）",
          dup_user == 1 and len(os.listdir(mod.INBOX)) == 1
          and not [f for f in os.listdir(mod.INBOX) if f.endswith(".claim")],
          f"dup_hist={dup_user} inbox={os.listdir(mod.INBOX)}")

    # ---------- P3 主动+回复交叠 ----------
    reset_state()
    N3_INIT, N3_ADD = 40, 40
    for i in range(N3_INIT):
        write_pending(f"P3-{i:03d}.json", f"P3-{i:03d}", f"交叠{i}",
                      kind="proactive" if i % 2 else "reply")
    send_state = {}
    producer_err = []

    def producer():
        try:
            for i in range(N3_ADD):
                j = N3_INIT + i
                mod._process_inbound(SESSION, user_msg(f"P3-IN-{i:02d}", f"新消息{i}"),
                                     USER_ID, state={})
                write_pending(f"P3-{j:03d}.json", f"P3-{j:03d}", f"交叠{j}",
                              kind="proactive" if j % 2 else "reply")
                time.sleep(0.002)
        except Exception as e:
            producer_err.append(repr(e))

    pt = threading.Thread(target=producer)
    pt.start()
    # 收尾等待稳健化（2026-10-26）：原判定是无总时限的 while 循环 +
    # join(30s) 后不验线程存活——全套连跑高负载下曾闪 1 项时序抖动
    # （计数断言在生产者未收尾时抢跑判定）。改单调时钟 120s 截止：
    # 截止内以「生产者结束且 pending 排空」为完成条件，P3a 另显式
    # 断言生产者已收尾；计数断言本体（80/80/全唯一/排空）不放宽。
    deadline3 = time.monotonic() + 120
    while time.monotonic() < deadline3:
        mod._sender_pass(SESSION, USER_ID, [0, 0], send_state, skip_media=True)
        if not pt.is_alive() and not [f for f in os.listdir(mod.PENDING)
                                      if f.endswith(".json")]:
            break
        if pt.is_alive():
            time.sleep(0.001)
    pt.join(timeout=10)
    sent3 = platform_snapshot()
    ids3 = [s["client_id"] for s in sent3]
    hist3 = history()
    n_hist_asst3 = sum(1 for h in hist3 if h.get("role") == "assistant")
    check("P3a 交叠生产/发送零丢失（platform=80、history assistant=80、生产者已收尾）",
          not producer_err and not pt.is_alive()
          and len(sent3) == N3_INIT + N3_ADD and n_hist_asst3 == N3_INIT + N3_ADD,
          f"sent={len(sent3)} hist_asst={n_hist_asst3} err={producer_err} alive={pt.is_alive()}")
    check("P3b 交叠发送零重复（client_id 全唯一）且 pending 排空",
          len(set(ids3)) == len(ids3)
          and not [f for f in os.listdir(mod.PENDING) if f.endswith(".json")],
          f"uniq={len(set(ids3))}/{len(ids3)}")

    # ---------- P4 state_lock 多进程 hammer（16 进程） ----------
    child = os.path.join(TMP, "hammer_child.py")
    with open(child, "w", encoding="utf-8") as f:
        f.write(
            "import os, sys\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "import bridge as m\n"
            "mode, n, ident = sys.argv[1], int(sys.argv[2]), sys.argv[3]\n"
            "if mode == 'bump':\n"
            "    for _ in range(n): m.bump_unreplied(1)\n"
            "elif mode == 'rmw':\n"
            "    p = os.path.join(m.STATE, 'hammer.json')\n"
            "    for _ in range(n):\n"
            "        with m.state_lock():\n"
            "            d = m.read_json(p, {}) or {}\n"
            "            d['n'] = int(d.get('n', 0)) + 1\n"
            "            m.atomic_write_json(p, d)\n"
            "elif mode == 'append':\n"
            "    p = os.path.join(m.STATE, 'hammer.jsonl')\n"
            "    for i in range(n): m.append_jsonl(p, {'proc': ident, 'i': i})\n")
    env = dict(os.environ, COMPANION_HOME=TMP)
    P4_PROCS = 16

    def hammer(mode, n):
        wj(mod.COUNTERS_FILE, {"unreplied": 0})
        wj(os.path.join(mod.STATE, "hammer.json"), {"n": 0})
        hp = os.path.join(mod.STATE, "hammer.jsonl")
        if os.path.exists(hp):
            os.remove(hp)
        t_ = time.time()
        procs = [subprocess.Popen([sys.executable, child, mode, str(n), str(k)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                 for k in range(P4_PROCS)]
        rcs, errs = [], []
        for p in procs:
            _, e = p.communicate(timeout=300)
            rcs.append(p.returncode)
            if e:
                errs.append(e.decode()[-200:])
        return time.time() - t_, rcs, errs

    wall, rcs, errs = hammer("bump", 50)
    got = mod.get_unreplied()
    METRICS["P4_bump_wall_s"] = round(wall, 2)
    check("P4a bump_unreplied 16进程x50 零丢失（=800）",
          all(r == 0 for r in rcs) and got == P4_PROCS * 50,
          f"got={got} rcs={rcs} err={errs[:1]}")
    wall, rcs, errs = hammer("rmw", 50)
    got = (mod.read_json(os.path.join(mod.STATE, "hammer.json"), {}) or {}).get("n")
    METRICS["P4_rmw_wall_s"] = round(wall, 2)
    check("P4b 通用 RMW 16进程x50 零丢失（hammer.json n=800）",
          all(r == 0 for r in rcs) and got == P4_PROCS * 50,
          f"got={got} err={errs[:1]}")
    wall, rcs, errs = hammer("append", 25)
    lines = [json.loads(x) for x in open(os.path.join(mod.STATE, "hammer.jsonl"), encoding="utf-8") if x.strip()]
    METRICS["P4_append_wall_s"] = round(wall, 2)
    check("P4c append_jsonl 16进程x25 零丢失零撕裂（=400 行、全 dict、(proc,i) 全唯一）",
          all(r == 0 for r in rcs) and len(lines) == P4_PROCS * 25
          and all(isinstance(x, dict) for x in lines)
          and len({(x["proc"], x["i"]) for x in lines}) == P4_PROCS * 25,
          f"lines={len(lines)} err={errs[:1]}")

    # ---------- P5 队列曲线 + 预检读放大结构断言 ----------
    curve = []
    reads_ok = True
    reads_detail = []
    for n in (10, 50, 100, 200, 300):
        reset_state()
        for i in range(n):
            write_pending(f"P5-{i:04d}.json", f"P5-{i:04d}", f"积压{i}")
        orig_read = mod.read_json
        cnt = {"pending_reads": 0}

        def counting_read(path, default=None, _orig=orig_read):
            if str(path).startswith(mod.PENDING):
                cnt["pending_reads"] += 1
            return _orig(path, default)

        mod.read_json = counting_read
        t0 = time.time()
        try:
            mod._sender_pass(SESSION, USER_ID, [0, 0], {}, skip_media=True)
        finally:
            mod.read_json = orig_read
        dt = time.time() - t0
        sent_n = len(platform_snapshot())
        curve.append({"n": n, "ms": round(dt * 1000, 1),
                      "ms_per": round(dt * 1000 / n, 2),
                      "pending_reads": cnt["pending_reads"], "sent": sent_n})
        if cnt["pending_reads"] > 3 * n + 10 or sent_n != n:
            reads_ok = False
            reads_detail.append(f"n={n} reads={cnt['pending_reads']} sent={sent_n}")
    METRICS["P5_curve"] = curve
    for c in curve:
        print(f"     P5 n={c['n']:4d} pass={c['ms']:9.1f}ms per={c['ms_per']:6.2f}ms reads={c['pending_reads']}")
    check("P5 单趟排空各档零丢失且 pending 读次数 O(n)（<=3n+10，非 O(n^2) 预检重读）",
          reads_ok, "; ".join(reads_detail))

    # ---------- P6 压后全量可解析 ----------
    reset_state()
    for i in range(20):
        mod._process_inbound(SESSION, user_msg(f"P6-{i:02d}", f"毒化检查{i}"), USER_ID, state={})
        write_pending(f"P6-{i:02d}.json", f"P6-{i:02d}", f"回{i}",
                      kind="proactive" if i % 3 == 0 else "reply")
    drain_passes()
    bad = []
    n_json = n_jsonl = 0
    for base, _dirs, files in os.walk(TMP):
        for fn in files:
            p = os.path.join(base, fn)
            try:
                if fn.endswith(".json"):
                    json.load(open(p, encoding="utf-8"))
                    n_json += 1
                elif fn.endswith(".jsonl"):
                    for line in open(p, encoding="utf-8"):
                        if line.strip() and not isinstance(json.loads(line), dict):
                            bad.append(p + ":non-dict")
                    n_jsonl += 1
            except Exception as e:
                bad.append(f"{p}:{e}")
    check("P6 压后 state/inbox/outbox 全量可解析（零毒化）", not bad,
          f"bad={bad[:3]} json={n_json} jsonl={n_jsonl}")
    METRICS["P6_files"] = {"json": n_json, "jsonl": n_jsonl}

    # P7 启动清扫残留 claim（第五轮集成）：kill -9 于 claim 窗口残留的
    # *.claim 在 serve 启动（持单实例锁后）整表清掉，已落盘消息不受伤
    keep = os.path.join(mod.INBOX, "P7-keep.json")
    with open(keep, "w", encoding="utf-8") as f:
        f.write("{}")
    for mid in ("P7-keep", "P7-orphan"):
        with open(os.path.join(mod.INBOX, mid + ".claim"), "w") as f:
            f.write("")
    swept = mod._sweep_stale_claims()
    left = [x for x in os.listdir(mod.INBOX) if x.endswith(".claim")]
    check("P7 启动清扫残留 claim（2 个全清、已落盘 json 不动）",
          swept == 2 and not left and os.path.exists(keep),
          f"swept={swept} left={left}")

    # P8 文本/媒体双发送线程真并发交叠（第五轮线6补缺：线1 只压了文本侧）
    reset_state()
    media_sent = []
    media_lock = threading.Lock()

    def fake_send_media(session, to_user_id, media_file, media_type,
                        context_token, client_id=None, log=None, cache_data=None, cache_path=None):
        time.sleep(0.001)
        with media_lock:
            media_sent.append({"client_id": client_id, "media_file": media_file})
        return {"ret": 0}

    orig_sm = mod.send_media_message
    mod.send_media_message = fake_send_media
    dummy_media = os.path.join(TMP, "state", "media_in", "p8-dummy.jpg")
    with open(dummy_media, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"x" * 64)
    N8 = 10
    for i in range(N8):
        write_pending(f"P8T-{i:02d}.json", f"P8T-{i:02d}", f"文本{i}", typing_sec=0)
        wj(os.path.join(mod.PENDING, f"P8M-{i:02d}.json"),
           {"to_user_id": USER_ID, "text": f"图{i}", "context_token": CTX,
            "message_id": f"P8M-{i:02d}", "send_after": 0, "kind": "reply",
            "typing_sec": 0, "client_id": f"ccb-P8M-{i:02d}",
            "media_file": dummy_media, "media_type": "image"})
    errs8 = []

    # 轮询收尾稳健化（2026-10-26，同 P3 口径）：原判定是固定 200 次
    # 迭代上限 + join(60s)——迭代次数与墙钟在高负载下都可能先于排空
    # 耗尽、误判失败。改单调时钟 120s 截止：截止内以本前缀 pending
    # 排空为完成条件；计数断言本体（10+10/全唯一/排空/线程已结束）
    # 不放宽。
    def text_loop():
        try:
            st = {}
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if not [f for f in os.listdir(mod.PENDING)
                        if f.endswith(".json") and f.startswith("P8T")]:
                    break
                mod._sender_pass(SESSION, USER_ID, [0, 0], st, skip_media=True)
        except Exception as e:
            errs8.append(f"text:{e!r}")

    def media_loop():
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if not [f for f in os.listdir(mod.PENDING)
                        if f.endswith(".json") and f.startswith("P8M")]:
                    break
                mod._media_sender_pass(SESSION, USER_ID)
        except Exception as e:
            errs8.append(f"media:{e!r}")

    th8 = [threading.Thread(target=text_loop), threading.Thread(target=media_loop)]
    for t in th8:
        t.start()
    for t in th8:
        t.join(timeout=150)
    mod.send_media_message = orig_sm
    sent8 = platform_snapshot()
    check("P8a 双线程交叠零丢失零重复（文本 10 + 媒体 10、client_id 全唯一）",
          len(sent8) == N8 and len(media_sent) == N8
          and len({s["client_id"] for s in sent8}) == N8
          and len({s["client_id"] for s in media_sent}) == N8,
          f"text={len(sent8)} media={len(media_sent)} errs={errs8}")
    check("P8b 双线程交叠后 pending 排空、sent=20、无线程异常",
          not [f for f in os.listdir(mod.PENDING) if f.endswith(".json")]
          and len(os.listdir(mod.SENT)) == 2 * N8 and not errs8
          and not any(t.is_alive() for t in th8),
          f"sent_dir={len(os.listdir(mod.SENT))} errs={errs8}")
finally:
    passed = sum(1 for _, ok in RESULTS if ok)
    print("\nMETRICS " + json.dumps(METRICS, ensure_ascii=False))
    print(f"==== pressure selftest: {passed}/{len(RESULTS)} PASS ====")
    if not os.environ.get("CCB_PRESSURE_KEEP"):
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if passed == len(RESULTS) else 1)
