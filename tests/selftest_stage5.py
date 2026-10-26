#!/usr/bin/env python3
"""阶段五真人感引擎自检（通用版）：事实冲突/心情链/两段式/梗与供给批。
全部用临时文件/打桩，不污染真实 state。运行：python3 tests/selftest_stage5.py"""
import importlib.util
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


worker = _load("worker_s5", "worker.py")
bridge = worker.bridge

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {extra}")


TMP = tempfile.mkdtemp(prefix="ccb-stage5-")
today = worker.today_local().date().isoformat()

# ---------- 1. bible 事实冲突能被自检拦下（5.13-1） ----------
cfg = bridge.cfg()
base_ctx = {"cfg": cfg, "intent": "chat", "batch_text": "你中午吃的啥",
            "batch_question": False, "answered": True, "persona": "",
            "recent_replies": [], "facts": [{"fact": "不吃香菜", "date": today}],
            "target_len": 20, "crisis": False}
v = worker.check_candidate(["我中午吃的香菜牛肉面，真香"], dict(base_ctx))
check("S1a 与 bible 事实冲突（爱吃香菜）被拦下", any("冲突" in x for x in v), str(v))
v = worker.check_candidate(["我不吃香菜的，你又忘了"], dict(base_ctx))
check("S1b 正常提及禁忌食物不误拦", not any("冲突" in x for x in v), str(v))

# ---------- 2. mood 只能相邻变化（5.13-2 前半） ----------
life_tmp = os.path.join(TMP, "life.json")
with open(life_tmp, "w", encoding="utf-8") as f:
    json.dump({"date": today, "mood_now": "烦躁"}, f)
worker.LIFE_FILE = life_tmp
ok, _ = worker.set_mood_now("开心")
check("S2a 烦躁→开心 跳变被拒绝", not ok)
ok, _ = worker.set_mood_now("有点闷")
check("S2b 烦躁→有点闷 相邻变化通过", ok)
with open(life_tmp, encoding="utf-8") as f:
    check("S2c mood_now 已落盘", json.load(f)["mood_now"] == "有点闷")

# ---------- 3. 两段式：vent 不触发 / 概率与次数闸门（5.13-2 后半、5.3） ----------
worker.load_life_orig = worker.load_life
worker.load_life = lambda: {"schedule": "下午开会，晚上改稿", "busy_level": "mid",
                            "mood_now": "平静", "date": today}
worker.TWO_STAGE_FILE = os.path.join(TMP, "two_stage.json")
bridge.get_unreplied_orig = bridge.get_unreplied
bridge.get_unreplied = lambda: 0
d = worker.two_stage_decision("vent", roll=0.0)
check("S3a vent 不触发两段式", not d["trigger"], str(d))
d = worker.two_stage_decision("chat", roll=0.5)
check("S3b 概率未命中不触发", not d["trigger"], str(d))
d = worker.two_stage_decision("chat", roll=0.05)
check("S3c 条件满足且概率命中触发，占位语对得上日程",
      d["trigger"] and d["placeholder"] == "在开会，等下", str(d))
d = worker.two_stage_decision("chat", roll=0.05)
d2 = worker.two_stage_decision("chat", roll=0.05)
check("S3d 每天最多触发 2 次", d["trigger"] and not d2["trigger"], f"{d} / {d2}")
worker.load_life = worker.load_life_orig
bridge.get_unreplied = bridge.get_unreplied_orig

# ---------- 4. later 任务到期判断（5.13-3） ----------
later = {"send_after": 100, "created_at": 50}
check("S4a 未到期 → wait", bridge.later_action(later, 0, 90, 0, 9) == "wait")
check("S4b 期间有新消息 → cancel", bridge.later_action(later, 60, 110, 0, 9) == "cancel")
check("S4c 到期无新消息 → due", bridge.later_action(later, 40, 110, 0, 9) == "due")
check("S4d 达停发上限 → cancel", bridge.later_action(later, 40, 110, 9, 9) == "cancel")

# ---------- 5. 毛病频率闸门（5.13-4） ----------
counters_tmp = os.path.join(TMP, "counters.json")
bridge.COUNTERS_FILE = counters_tmp
total = worker.reply_count_total()
with open(counters_tmp, "w", encoding="utf-8") as f:
    json.dump({"last_flaw_at_reply": total}, f)
check("S5a 刚出过毛病 → 不放行", not worker.flaw_gate()["allowed"])
with open(counters_tmp, "w", encoding="utf-8") as f:
    json.dump({"last_flaw_at_reply": max(0, total - 25)}, f)
g = worker.flaw_gate()
check("S5b 距上次 ≥20 条 → 放行", g["allowed"] or total < 20, str(g))
r = worker.flaw_gate(used=True)
with open(counters_tmp, encoding="utf-8") as f:
    saved = json.load(f)
check("S5c used 记录落盘", r.get("recorded") and saved.get("last_flaw_at_reply") == total)

# ---------- 6. 冷却词与讨好检测（5.13-5） ----------
ctx = dict(base_ctx)
ctx["cooling"] = ["乖"]
v = worker.check_candidate(["乖，先去吃饭"], ctx)
check("S6a 命中冷却词被拦下", any("冷却词" in x for x in v), str(v))
ctx = dict(base_ctx)
ctx["recent_praise"] = 8
v = worker.check_candidate(["你真棒，这事办得漂亮"], ctx)
check("S6b 近 10 轮夸太多还夸 → 讨好过度被拦下", any("讨好" in x for x in v), str(v))
ctx = dict(base_ctx)
ctx["recent_praise"] = 2
v = worker.check_candidate(["你真棒，这事办得漂亮"], ctx)
check("S6c 夸得不多时不误拦", not any("讨好" in x for x in v), str(v))
ctx = dict(base_ctx)
ctx["batch_question"] = True
ctx["batch_text"] = "光合作用是什么"
v = worker.check_candidate(
    ["光合作用是指绿色植物利用光能把二氧化碳和水转化成有机物并释放氧气的过程，"
     "主要发生在叶绿体里，是地球上最重要的化学反应之一，听懂了吗"], ctx)
check("S6d 知识问答教科书式被拦下", any("教科书" in x for x in v), str(v))

# ---------- 7. 梗的 7 天与退休规则（5.13-6） ----------
worker.JOKES_FILE = os.path.join(TMP, "jokes.json")
now = time.time()
worker.save_jokes([
    {"text": "三天前用过的梗", "created_at": now - 86400 * 10,
     "last_used_at": now - 86400 * 3, "uses": 1},
    {"text": "八天前用过的梗", "created_at": now - 86400 * 30,
     "last_used_at": now - 86400 * 8, "uses": 2},
    {"text": "用满五次的梗", "created_at": now - 86400 * 30,
     "last_used_at": now - 86400 * 30, "uses": 5},
    {"text": "还没用过的新梗", "created_at": now, "last_used_at": None, "uses": 0},
])
texts = [j["text"] for j in worker.joke_candidates(now=now)]
check("S7a 7 天内用过的梗不候选", "三天前用过的梗" not in texts, str(texts))
check("S7b 8 天前的梗可候选", "八天前用过的梗" in texts, str(texts))
check("S7c 用满 5 次退休", "用满五次的梗" not in texts, str(texts))
check("S7d 新梗可候选", "还没用过的新梗" in texts, str(texts))

# ---------- 8. 察觉变化：连续短回复 ----------
hist_tmp = os.path.join(TMP, "history.jsonl")
rows = [{"role": "user", "text": "今天去客户那儿开会，回来还得赶图，累死了",
         "ts": now - 3600 * 5},
        {"role": "user", "text": "晚饭在楼下随便吃了点面", "ts": now - 3600 * 4},
        {"role": "user", "text": "嗯", "ts": now - 300},
        {"role": "user", "text": "哦", "ts": now - 200},
        {"role": "user", "text": "好", "ts": now - 100}]
with open(hist_tmp, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
bridge.HISTORY_FILE = hist_tmp
check("S8a 连发短回复被察觉", worker.quiet_today())
rows[2]["text"] = "刚洗完澡，准备睡了"
rows[3]["text"] = "小猫又在跑酷"
rows[4]["text"] = "明天还要早起开会呢"
with open(hist_tmp, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
check("S8b 正常长度不误报", not worker.quiet_today())

# ---------- 9. 去 AI 感供给批（L1–L6 代码侧） ----------
import random as _rnd9

# S9a 表情全池计数+冷却：[偷笑] 与 😉 用量 >2 标冷却，全池含 0 次项
recs_tmp = os.path.join(TMP, "records_s9.jsonl")
with open(recs_tmp, "w", encoding="utf-8") as f:
    for rep in ("[偷笑] 好呀[偷笑]", "[偷笑] 嗯😉😉😉", "[抱抱] 好", "没表情"):
        f.write(json.dumps({"reply": rep, "intent": "chat", "skipped": False},
                           ensure_ascii=False) + "\n")
worker.RECORDS_FILE = recs_tmp
pool = worker.emoji_pool_counts()
check("S9a-1 方括号全池计数（[偷笑]×3、[抱抱]×1、[机智]×0）",
      pool["bracket"]["[偷笑]"]["count"] == 3
      and pool["bracket"]["[抱抱]"]["count"] == 1
      and pool["bracket"]["[机智]"]["count"] == 0
      and len(pool["bracket"]) == 10, str(pool["bracket"]))
check("S9a-2 用量>2 标冷却（[偷笑] 与 😉）",
      pool["bracket"]["[偷笑]"]["cooling"] and pool["emoji"]["😉"]["cooling"]
      and not pool["bracket"]["[抱抱]"]["cooling"], str(pool))
check("S9a-3 emoji_recent 串带冷却与未用清单",
      "（冷却）" in worker.bracket_emoji_recent()
      and "未用" in worker.bracket_emoji_recent(),
      worker.bracket_emoji_recent())

# S9b 表态种子：只取既有口味 facts、今日段不作种子、同日稳定
facts_tmp = os.path.join(TMP, "facts_s9.json")
with open(facts_tmp, "w", encoding="utf-8") as f:
    json.dump({"facts": [
        {"fact": "奶茶三分糖去冰加珍珠", "date": "2026-10-21"},
        {"fact": "爱吃辣，辣椒牛肉是心头好", "date": "2026-10-21"},
        {"fact": "今天穿蓝色外套", "date": today},
        {"fact": "身高 168", "date": "2026-10-21"}]}, f, ensure_ascii=False)
worker.FACTS_FILE = facts_tmp
pref = worker.today_preference()
check("S9b 表态种子取既有口味事实且同日稳定",
      pref and pref["fact"] in ("奶茶三分糖去冰加珍珠", "爱吃辣，辣椒牛肉是心头好")
      and worker.today_preference() == pref, str(pref))

# S9c 小事件配比：任意种子下 2 条里至少 1 条人际类
ok9c = True
for seed in range(50):
    evs = worker._pick_small_events(_rnd9.Random(seed))
    if len(evs) != 2 or len(set(evs)) != 2 or not any(
            any(k in e for k in worker.life_config()["people_keywords"])
            for e in evs):
        ok9c = False
check("S9c 每日小事件至少 1 条人际类", ok9c)

# S9c-2 偶尔档场景事件门控（第七轮线4）：未开门控绝不出现、开门控会出现
# 且人际类配比不破
occ = set(worker.life_config()["occasional_events"])
ok9c2a = all(not (set(worker._pick_small_events(_rnd9.Random(s))) & occ)
             for s in range(50))
seen_occ = set()
ok9c2b = True
for s in range(50):
    evs = worker._pick_small_events(_rnd9.Random(s), occasional_ok=True)
    seen_occ |= set(evs) & occ
    if not any(any(k in e for k in worker.life_config()["people_keywords"])
               for e in evs):
        ok9c2b = False
check("S9c-2 偶尔档事件门控（关不出现/开会出现且配比不破）",
      ok9c2a and ok9c2b and len(seen_occ) >= 2,
      f"关={ok9c2a} 开配比={ok9c2b} 见偶={len(seen_occ)}")

# S9d 近 7 天未提及人物：只认她说过的话，他提的不算
hist9 = os.path.join(TMP, "history_s9.jsonl")
with open(hist9, "w", encoding="utf-8") as f:
    f.write(json.dumps({"role": "assistant", "text": "同事说她下月来玩",
                        "ts": now - 3600}, ensure_ascii=False) + "\n")
    f.write(json.dumps({"role": "user", "text": "家人今天怎么样",
                        "ts": now - 1800}, ensure_ascii=False) + "\n")
bridge.HISTORY_FILE = hist9
quiet = worker.people_quiet_7d(["室友", "同事", "家人"])
check("S9d 未提及人物只看她的发言（室友/家人 quiet、同事不在）",
      quiet == ["室友", "家人"], str(quiet))

# S9e 可回指清单：user_facts 按 since 取近 3、未闭环 7 天内有效、过期丢弃
uf_tmp = os.path.join(TMP, "user_facts_s9.json")
with open(uf_tmp, "w", encoding="utf-8") as f:
    json.dump({"user_facts": [
        {"fact": "他下周要出差", "since": "2026-10-24", "status": "active"},
        {"fact": "他养了只猫", "since": "2026-10-20", "status": "active"},
        {"fact": "旧的一条", "since": "2026-10-01", "status": "done"}]},
        f, ensure_ascii=False)
worker.BASE_FACTS_FILE = uf_tmp
sums_tmp = os.path.join(TMP, "summaries_s9.jsonl")
with open(sums_tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps({"date": today, "text": "x",
                        "unfinished": "周日约会还没定时间"},
                       ensure_ascii=False) + "\n")
worker.SUMMARIES_FILE = sums_tmp
rec = worker.recall_list()
check("S9e-1 回指清单（facts 近期在前、未闭环带出、上轮原话要点）",
      [x["fact"] for x in rec["user_facts_recent"]] == ["他下周要出差", "他养了只猫"]
      and rec["unfinished"]["topic"] == "周日约会还没定时间"
      and rec["last_user_point"]["text"] == "家人今天怎么样", str(rec))
with open(sums_tmp, "w", encoding="utf-8") as f:
    old = (worker.today_local().date() - worker.timedelta(days=10)).isoformat() \
        if hasattr(worker, "timedelta") else "2026-10-15"
    f.write(json.dumps({"date": old, "text": "x", "unfinished": "过期的事"},
                       ensure_ascii=False) + "\n")
check("S9e-2 未闭环超 7 天过期不带出",
      worker.recall_list()["unfinished"] is None, str(worker.recall_list()))

# S9f 情绪信号：负面词命中或近轮 vent 都标 comfort；平常不标
sig = worker.emotion_signal(["今天加班到现在，累死了"])
check("S9f-1 负面信号词命中 → comfort 场景",
      sig["scene"] == "comfort" and sig["negative_hits"], str(sig))
sig2 = worker.emotion_signal(["周末去爬山呀"])
check("S9f-2 无信号不误标", sig2["scene"] == "", str(sig2))

# S9g 两段式复活：mid 无关键词日程也可触发（通用占位）、low 仍不触发、
# 今日计数展示口径正确
worker.load_life_s9 = worker.load_life
worker.load_life = lambda: {"schedule": "上午处理杂事，下午赶一个稿子",
                            "busy_level": "mid", "mood_now": "平静",
                            "date": today}
bridge.get_unreplied_s9 = bridge.get_unreplied
bridge.get_unreplied = lambda: 0
ts_tmp = os.path.join(TMP, "two_stage_s9.json")
worker.TWO_STAGE_FILE = ts_tmp
d = worker.two_stage_decision("chat", roll=0.05)
check("S9g-1 mid 无关键词日程命中概率可触发（在忙，等下）",
      d["trigger"] and d["placeholder"] == "在忙，等下", str(d))
check("S9g-2 触发落盘且今日计数=1", worker.two_stage_today() == 1,
      str(worker.two_stage_today()))
worker.load_life = lambda: {"schedule": "白天没什么事，晚上打算自己做顿好的",
                            "busy_level": "low", "mood_now": "平静",
                            "date": today}
d = worker.two_stage_decision("chat", roll=0.05)
check("S9g-3 low 真闲着不装忙", not d["trigger"] and d["reason"] == "当下不忙",
      str(d))
with open(ts_tmp, "w", encoding="utf-8") as f:
    json.dump({"date": "2020-01-01", "count": 2}, f)
check("S9g-4 状态日期过期时今日计数按 0", worker.two_stage_today() == 0)
worker.load_life = worker.load_life_s9
bridge.get_unreplied = bridge.get_unreplied_s9

print(f"\n==== stage5 selftest: {PASS} PASS / {FAIL} FAIL ====")
sys.exit(1 if FAIL else 0)
