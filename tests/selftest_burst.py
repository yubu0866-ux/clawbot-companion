#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连发（burst）场景自检：验证 agent 流程「落盘前复查并入 + 轮尾续接」
所依赖的代码原语在连发时间线下的行为。

背景（2026-10-22 实聊定位）：连发拆轮的主因是生成期间到达的消息只能
另起一轮。修复让 hook 流程在落盘前用 `worker.py pending --exclude`
复查并入、轮尾用 `worker.py pending` 续接。本自检用沙箱 HOME 回放
连发时间戳序列，断言：
- pending 的收集顺序、exclude 语义、撤回标记、processed 标后消失；
- 旧流程（无复查）把 3 条连发拆成 2 轮、新流程（复查并入）只用 1 轮，
  且末条等待时间显著缩短（离散时间线模型，生成耗时取实测中位量级）；
- write_outbox 对合并批次的幂等与锚点（文件名用最新消息 id）；
- 通用版说明：源套件 B8–B13/B15 是对个人版规程文档的逐字文本锁，
  通用版无该文档（agent 契约见 docs/agent-mode.md），
  已剔除；其语义由 B7 write_outbox 幂等行为锁与 B14 pack 检查承接。

运行：python3 tests/selftest_burst.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"（{detail}）" if detail and not cond else ""))


def base_config():
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["llm"]["mode"] = "api"  # pending/write_outbox 不走生成，仅防导入期分支
    return cfg


def make_home():
    home = tempfile.mkdtemp(prefix="ccb-burst-")
    for d in ("state", "inbox", "outbox/pending", "outbox/sent", "outbox/failed"):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(base_config(), f, ensure_ascii=False)
    with open(os.path.join(home, "state", "processed.json"), "w", encoding="utf-8") as f:
        json.dump({"processed": []}, f)
    return home


def env_for(home):
    env = dict(os.environ)
    env["COMPANION_HOME"] = home
    return env


def run_pending(home, *args):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "worker.py"), "pending", *args],
                       env=env_for(home), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"pending 失败：{r.stderr.strip()}")
    return json.loads(r.stdout)


def write_inbox(home, mid, create_ms, text="你好", recalled=False):
    msg = {"message_id": mid, "from_user_id": "u1", "context_token": "tok",
           "create_time_ms": create_ms, "type": "text", "text": text,
           "ref_text": "", "recalled": recalled}
    with open(os.path.join(home, "inbox", mid + ".json"), "w", encoding="utf-8") as f:
        json.dump(msg, f, ensure_ascii=False)


def mark_processed(home, ids):
    p = os.path.join(home, "state", "processed.json")
    data = json.load(open(p, encoding="utf-8"))
    data["processed"] += [i for i in ids if i not in data["processed"]]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


T0 = 1792600000000  # 模拟时间线基准（ms）
A, B, C, D, E = "9000000000000000001", "9000000000000000002", "9000000000000000003", \
                "9000000000000000004", "9000000000000000005"

home = make_home()

# ---------- B1 收集：连发前两条按 create_time_ms 顺序返回 ----------
write_inbox(home, A, T0, "好")
write_inbox(home, B, T0 + 7000, "今天怎么睡这么早")
got = run_pending(home)
check("B1 pending 按到达顺序返回连发消息", [m["message_id"] for m in got] == [A, B],
      str([m["message_id"] for m in got]))

# ---------- B2 落盘前复查：生成期间到的第三条能被 exclude 复查捞出 ----------
write_inbox(home, C, T0 + 28000, "小猫是不是又要打疫苗了")
got = run_pending(home, "--exclude", f"{A},{B}")
check("B2 落盘前复查只返回生成期间新到的消息", [m["message_id"] for m in got] == [C],
      str([m["message_id"] for m in got]))
got = run_pending(home, "--exclude", f"{A},{B},{C}")
check("B2b 全部并入后复查为空", got == [], str(got))

# ---------- B3 撤回消息带 recalled 标记（不并入内容、只标 processed） ----------
write_inbox(home, D, T0 + 30000, "发错了", recalled=True)
got = run_pending(home, "--exclude", f"{A},{B},{C}")
check("B3 撤回消息带 recalled=true 返回", len(got) == 1 and got[0]["recalled"] is True, str(got))

# ---------- B4 标 processed 后消失；B5 轮尾续接能捞到再晚到的消息 ----------
mark_processed(home, [A, B, C, D])
check("B4 已标 processed 的消息不再返回", run_pending(home) == [])
write_inbox(home, E, T0 + 45000, "等我洗完澡就来")
got = run_pending(home)
check("B5 轮尾续接复查返回处理期间之后新到的消息", [m["message_id"] for m in got] == [E],
      str([m["message_id"] for m in got]))
mark_processed(home, [E])
check("B5b 续接处理完后复查为空（本轮可结束）", run_pending(home) == [])

# ---------- B6 轮数与等待：旧流程 vs 新流程（离散时间线模型） ----------
# 时间线（秒）：A@0、B@7、C@28；收集在 t=5（唤醒≈3s+防抖≈2s），
# 单轮生成耗时 G=40s（实测生成相中位 57–96s，此处取偏小值不夸大收益）。
# 旧流程：收集@5 得 [A]，落盘@45；第二轮收集得 [B,C]，落盘@85+。
# 新流程：收集@5 得 [A]；生成期间 B、C 到达；落盘前复查@45 并入 [B,C]，
# 修订耗时约 5s，落盘@50。轮数 2→1，C 的等待 57s→22s。
G, COLLECT_AT, REVISE = 40, 5, 5
home2 = make_home()
write_inbox(home2, A, T0, "好")
first = run_pending(home2)  # t=5 收集
assert [m["message_id"] for m in first] == [A]
write_inbox(home2, B, T0 + 7000, "今天怎么睡这么早")   # 生成期间到达
write_inbox(home2, C, T0 + 28000, "小猫是不是又要打疫苗了")
# 旧流程：无复查 → 落盘后另起一轮
mark_processed(home2, [A])
old_round2 = run_pending(home2)
old_rounds = 1 + (1 if old_round2 else 0)
old_c_wait = (COLLECT_AT + G) + G - 28  # 第一轮落盘后第二轮再付整轮
# 新流程：落盘前复查并入（home2 重新回放同一时间线）
home3 = make_home()
write_inbox(home3, A, T0, "好")
run_pending(home3)
write_inbox(home3, B, T0 + 7000, "今天怎么睡这么早")
write_inbox(home3, C, T0 + 28000, "小猫是不是又要打疫苗了")
fold = run_pending(home3, "--exclude", A)
new_batch = [A] + [m["message_id"] for m in fold]
new_rounds = 1 if len(new_batch) == 3 else 2
new_c_wait = (COLLECT_AT + G + REVISE) - 28
check("B6 旧流程把 3 条连发拆成 2 轮", old_rounds == 2, f"old_rounds={old_rounds}")
check("B6b 新流程落盘前并入后只用 1 轮", new_rounds == 1 and new_batch[-1] == C,
      f"batch={new_batch}")
check("B6c 末条等待至少省 30s", new_c_wait <= old_c_wait - 30,
      f"old={old_c_wait}s new={new_c_wait}s")

# ---------- B7 write_outbox：合并批次锚点=最新消息 + 幂等 ----------
os.environ["COMPANION_HOME"] = home3
spec = importlib.util.spec_from_file_location("worker_burst", os.path.join(ROOT, "worker.py"))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
batch = [{"message_id": A, "from_user_id": "u1", "context_token": "t1"},
         {"message_id": B, "from_user_id": "u1", "context_token": "t2"},
         {"message_id": C, "from_user_id": "u1", "context_token": "t3"}]
wrote = worker.write_outbox(batch, ["回复一", "回复二"], 1.0, 2)
pend = sorted(os.listdir(os.path.join(home3, "outbox", "pending")))
check("B7 合并批次写出且锚点为最新消息 id", wrote and pend == [C + "-1.json", C + "-2.json"],
      str(pend))
payload = json.load(open(os.path.join(home3, "outbox", "pending", C + "-1.json"), encoding="utf-8"))
check("B7b 气泡带最新消息的 context_token 与 message_id",
      payload.get("message_id") == C and payload.get("context_token") == "t3", str(payload))
wrote2 = worker.write_outbox(batch, ["回复一", "回复二"], 1.0, 2)
check("B7c 重复写被幂等跳过", wrote2 is False)

def run_round_brief(home):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "worker.py"), "round-brief"],
                       env=env_for(home), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"round-brief 失败：{r.stderr.strip()}")
    return json.loads(r.stdout)


home4 = make_home()
seed = [{"reply": "好呀[偷笑]"}, {"reply": "嗯嗯[偷笑]"}, {"reply": "来啦[抱抱]"},
        {"reply": "图来了[图片]"}, {"reply": "这条跳过[偷笑]", "skipped": True}]
with open(os.path.join(home4, "state", "reply_records.jsonl"), "w", encoding="utf-8") as f:
    for i, s in enumerate(seed):
        f.write(json.dumps({"ts": i + 1, "batch_ids": [str(i)], "user_text": "",
                            "intent": "chat", "proactive": False, **s},
                           ensure_ascii=False) + "\n")
brief4 = run_round_brief(home4)
er = brief4.get("emoji_recent", "")
check("B11c brief 含 emoji_recent 统计行（品种+次数、占位符与跳过轮不计）",
      "[偷笑]×2" in er and "[抱抱]×1" in er and "2种" in er and "图片" not in er,
      er)

# ---------- B14 打包简报 pack + user_facts 长期档案（2026-10-23 瘦身/固化） ----------
# 甲：prep 一次输出 pack（life/memories/facts 今日段/summaries/history 紧凑
# 窗口/records 紧凑版/user_facts 摘要），执行者不再分别读易变文件与 tail。
# 乙：user_facts 只带 active、按文件顺序（重要度）截 15 条，superseded 不带。

def run_prep(home):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "worker.py"), "prep"],
                       env=env_for(home), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"prep 失败：{r.stderr.strip()}")
    return json.loads(r.stdout)


home5 = make_home()
import datetime as _dt
_today = _dt.date.today().isoformat()
with open(os.path.join(home5, "state", "life.json"), "w", encoding="utf-8") as f:
    json.dump({"date": _today, "schedule": "下午改稿"}, f, ensure_ascii=False)
with open(os.path.join(home5, "state", "memory.json"), "w", encoding="utf-8") as f:
    json.dump({"memories": ["他的一条长期记忆"]}, f, ensure_ascii=False)
with open(os.path.join(home5, "state", "facts.json"), "w", encoding="utf-8") as f:
    json.dump({"facts": [{"fact": "今天的穿搭记录", "date": _today},
                         {"fact": "昨天的旧事实", "date": "2000-01-01"}]},
              f, ensure_ascii=False)
with open(os.path.join(home5, "state", "history.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"role": "user", "text": "", "media_file": "state/media_in/x.jpg",
                        "img_desc": "一张猫的照片", "message_id": "m1"},
                       ensure_ascii=False) + "\n")
    f.write(json.dumps({"role": "assistant", "text": "好可爱"}, ensure_ascii=False) + "\n")
with open(os.path.join(home5, "state", "reply_records.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"ts": 1, "batch_ids": ["m1"], "user_text": "看猫",
                        "reply": "好可爱呀", "intent": "chat", "skipped": False,
                        "proactive": False}, ensure_ascii=False) + "\n")
_uf = [{"fact": f"他的事实{i}", "since": "2026-10-21", "source": "测试", "status": "active"}
       for i in range(16)]
_uf.append({"fact": "已过时的事实", "since": "2026-10-21", "source": "测试",
            "status": "superseded"})
with open(os.path.join(home5, "facts.json"), "w", encoding="utf-8") as f:
    json.dump({"user_facts": _uf}, f, ensure_ascii=False)
prep5 = run_prep(home5)
pack5 = prep5.get("pack", {})
check("B14 prep 输出含 pack 打包简报（七段齐全）",
      all(k in pack5 for k in ("life", "memories", "facts_today", "summaries",
                               "history_window", "records", "user_facts")),
      str(sorted(pack5.keys())))
check("B14b pack.facts_today 只带今日段、life/memories 原样带入",
      pack5.get("facts_today") == ["今天的穿搭记录"]
      and pack5.get("life", {}).get("schedule") == "下午改稿"
      and pack5.get("memories") == ["他的一条长期记忆"],
      str(pack5.get("facts_today")))
check("B14c pack.user_facts 只带 active 且按重要度截 15 条",
      len(pack5.get("user_facts", [])) == 15
      and pack5["user_facts"][0] == "他的事实0"
      and "已过时的事实" not in pack5["user_facts"],
      str(len(pack5.get("user_facts", []))))
check("B14d pack.history_window 带媒体描述标记、records 为紧凑版",
      any("[图：一张猫的照片]" in line for line in pack5.get("history_window", []))
      and pack5.get("records", [{}])[0].get("intent") == "chat"
      and pack5.get("records", [{}])[0].get("reply") == "好可爱呀",
      str(pack5.get("history_window")))

failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
sys.exit(1 if failed else 0)
