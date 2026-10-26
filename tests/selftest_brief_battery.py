#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线2 供给侧简报质量评测电池（第五轮）：合成 state 跑真实 round_brief /
build_pack / emotion_signal / today_preference / people_quiet_7d /
recall_list / two_stage_* / emoji_pool_counts，逐场景审错供/漏供/多供。

沙箱纪律：worker.py 副本 + 合成 state 全部落在 /tmp 临时目录，
绝不碰生产 state/、不重启 serve；结束自清理。运行：
TZ=Asia/Shanghai python3 tests/selftest_brief_battery.py
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

os.environ["TZ"] = "Asia/Shanghai"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = tempfile.mkdtemp(prefix="ccb-brief-battery-")
os.environ["COMPANION_HOME"] = SANDBOX

# worker.py 副本进沙箱（连同其 _load 的同目录模块），HERE/STATE 全指向沙箱；
# 三件套用 example 文件改名落位，内容包复制到沙箱 content/ 目录
for fn in ("worker.py", "bridge.py", "llm.py", "tune.py"):
    shutil.copy(os.path.join(ROOT, fn), os.path.join(SANDBOX, fn))
for src_name, dst_name in (("config.example.json", "config.json"),
                           ("persona.example.json", "persona.json"),
                           ("facts.example.json", "facts.json")):
    shutil.copy(os.path.join(ROOT, src_name), os.path.join(SANDBOX, dst_name))
os.makedirs(os.path.join(SANDBOX, "content"), exist_ok=True)
for fn in ("scene_cards.md", "wardrobe.md", "looks.md", "expressions.md"):
    shutil.copy(os.path.join(ROOT, "content", fn),
                os.path.join(SANDBOX, "content", fn))
STATE = os.path.join(SANDBOX, "state")
for d in ("state", "inbox", "outbox/pending", "outbox/sent", "outbox/failed"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)

_spec = importlib.util.spec_from_file_location(
    "worker_battery", os.path.join(SANDBOX, "worker.py"))
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)
bridge = worker.bridge

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"（{detail}）" if detail and not cond else ""))


def scenario(name):
    """场景装饰器：异常不中断整组电池，记为该场景 FAIL 并带证据。"""
    def deco(fn):
        try:
            fn()
        except Exception as e:
            check(f"{name}（场景执行异常）", False, f"{type(e).__name__}: {e}")
        return fn
    return deco


# ---------- 合成 state 工具 ----------
def clear_state():
    for fn in os.listdir(STATE):
        p = os.path.join(STATE, fn)
        if os.path.isfile(p):
            os.remove(p)


def wjson(name, obj):
    with open(os.path.join(STATE, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def wjsonl(name, rows):
    with open(os.path.join(STATE, name), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def today_date():
    return worker.today_local().date()


def noon_ts(d):
    """某天正午时间戳（避开 00:00-01:00 跨日竞态的老坑，钉在中午）。"""
    start, _ = worker.day_bounds(d)
    return start.timestamp() + 12 * 3600


def set_life(**over):
    life = {"date": today_date().isoformat(), "mood_now": "平静",
            "schedule": "上午在家画画", "busy_level": "low"}
    life.update(over)
    wjson("life.json", life)
    return life


NOW = time.time()
TODAY = today_date()
TODAY_S = TODAY.isoformat()
YDAY = TODAY - timedelta(days=1)
YDAY_S = YDAY.isoformat()


# ---------- B00 冷启动：空 state 不崩、全字段默认 ----------
@scenario("B00 冷启动")
def b00():
    clear_state()
    brief = worker.round_brief()
    check("B00-1 空 state 简报默认（心情平静/无梗/quiet 关/无套话警报）",
          brief["mood_now"] == "平静" and brief["jokes"] == []
          and brief["quiet_today"] is False and brief["probe_alert"] is None,
          str(brief))
    pool = brief["emoji_pool"]
    check("B00-2 空 state 表情池全零（窗口 0、全池 10 种）",
          pool["window_rounds"] == 0 and len(pool["bracket"]) == 10
          and all(c["count"] == 0 for c in pool["bracket"].values()),
          str(pool))
    pack = worker.build_pack()
    check("B00-3 空 state pack 默认（无人提及=全员 quiet/无回指/无断档）",
          pack["facts_today"] == [] and pack["today_preference"] is None
          and pack["people_quiet_7d"] == ["室友", "同事", "家人"]
          and pack["recall"]["unfinished"] is None
          and pack["recall"]["last_user_point"] is None
          and pack["summary_gap"] == "", str(pack)[:200])
    card_names = []
    for line in open(os.path.join(ROOT, "content", "scene_cards.md"),
                     encoding="utf-8"):
        m = re.match(r"###\s+(.+?)\s*$", line)
        if m:
            card_names.append(m.group(1).strip())
    pm = re.search(r"固定场景 \d+ 个：([^；]+)", pack["scene_cards"])
    pack_names = pm.group(1).split("/") if pm else []
    check("B00-4 空 state 场景卡名单与 content/scene_cards.md 标题集合全等",
          pack_names == card_names and len(card_names) == 3,
          f"pack={pack_names} cards={card_names}")
    sig = worker.emotion_signal([])
    check("B00-5 空批情绪信号静默、两段式今日计数 0",
          sig["scene"] == "" and sig["negative_hits"] == []
          and worker.two_stage_today() == 0, str(sig))


# ---------- B01 他累了：负面词命中 → comfort ----------
@scenario("B01 他累了")
def b01():
    clear_state()
    set_life(mood_now="有点累")
    wjsonl("reply_records.jsonl",
           [{"ts": NOW, "reply": "好呀", "intent": "chat", "skipped": False}])
    sig = worker.emotion_signal(["今天加班到现在，累死了"])
    check("B01-1 负面词命中触发 comfort（累/加班）且带自述情绪词与降低玩笑注",
          sig["scene"] == "comfort" and "累" in sig["negative_hits"]
          and "加班" in sig["negative_hits"]
          and sig.get("her_emotion_words") and "降低玩笑" in sig.get("note", ""),
          str(sig))
    check("B01-2 her_mood_now 透传 life 心情",
          sig["her_mood_now"] == "有点累", str(sig))


# ---------- B02 情绪低落无显性词：靠近轮 vent intent，两侧 ----------
@scenario("B02 低落靠intent")
def b02():
    clear_state()
    set_life()
    wjsonl("reply_records.jsonl",
           [{"ts": NOW - 60, "reply": "抱抱", "intent": "vent",
             "skipped": False}])
    sig = worker.emotion_signal(["嗯嗯，你继续说"])
    check("B02-1 无负面词但近轮 vent → comfort（intent 通道）",
          sig["scene"] == "comfort" and sig["negative_hits"] == []
          and sig["recent_intents"] == ["vent"], str(sig))
    rows = [{"ts": NOW - 60, "reply": "抱抱", "intent": "vent",
             "skipped": False}]
    rows += [{"ts": NOW - 50 + i, "reply": "好", "intent": "chat",
              "skipped": False} for i in range(5)]
    wjsonl("reply_records.jsonl", rows)
    sig = worker.emotion_signal(["周末去爬山呀"])
    check("B02-2 vent 滑出近 5 轮后不再触发 comfort",
          sig["scene"] == "" and sig["recent_intents"] == ["chat"] * 5,
          str(sig))


# ---------- B03 开玩笑调侃：不该触发的都静默 ----------
@scenario("B03 调侃")
def b03():
    clear_state()
    set_life(mood_now="开心")
    wjsonl("reply_records.jsonl",
           [{"ts": NOW, "reply": "哈哈", "intent": "chat", "skipped": False}])
    sig = worker.emotion_signal(["哈哈哈你家猫太逗了，笑死我了"])
    check("B03-1 玩笑话不误标 comfort（笑死不在负面词表）",
          sig["scene"] == "" and sig["negative_hits"] == [], str(sig))
    brief = worker.round_brief()
    check("B03-2 调侃轮简报心情透传、无套话警报",
          brief["mood_now"] == "开心" and brief["probe_alert"] is None,
          str(brief))


# ---------- B04 问事实问题：不标情绪、回指清单指向上轮原话 ----------
@scenario("B04 问事实")
def b04():
    clear_state()
    set_life()
    q = "光合作用到底是什么呀"
    wjsonl("history.jsonl", [
        {"ts": NOW - 120, "role": "assistant", "text": "在呢"},
        {"ts": NOW - 60, "role": "user", "text": q}])
    sig = worker.emotion_signal([q])
    check("B04-1 事实提问不误标 comfort", sig["scene"] == "", str(sig))
    rec = worker.recall_list()
    lp = rec["last_user_point"] or {}
    check("B04-2 last_user_point 指向他最近一条原话且日期为今天",
          lp.get("text") == q and lp.get("date") == TODAY_S, str(rec))


# ---------- B05 他发了图：history 窗口媒体标记两侧 ----------
@scenario("B05 他发了图")
def b05():
    clear_state()
    set_life()
    wjsonl("history.jsonl", [
        {"ts": NOW - 120, "role": "user", "text": "看这只猫",
         "media_file": "state/media_in/a.jpg", "img_desc": "橘猫趴在窗台上"},
        {"ts": NOW - 60, "role": "user", "text": "还有这张",
         "media_file": "state/media_in/b.jpg"}])
    win = worker.build_pack()["history_window"]
    joined = "\n".join(win)
    check("B05-1 有描述带 [图：描述]、无描述带 [图] 标记",
          "[图：橘猫趴在窗台上]" in joined and "[图]" in joined, joined)


# ---------- B06 聊吃喝：today_preference 稳定轮换/今日排除/无候选 ----------
@scenario("B06 吃喝表态种子")
def b06():
    clear_state()
    set_life()
    wjson("facts.json", {"facts": [
        {"fact": "奶茶三分糖去冰加珍珠", "date": "2026-10-20"},
        {"fact": "爱吃辣，辣椒牛肉是心头好", "date": "2026-10-20"},
        {"fact": "今天吃火锅", "date": TODAY_S},
        {"fact": "身高 168", "date": "2026-10-20"}]})
    p1 = worker.today_preference()
    cands = ("奶茶三分糖去冰加珍珠", "爱吃辣，辣椒牛肉是心头好")
    check("B06-1 种子只取既有口味事实（今日段/非口味不入选）且同日稳定",
          p1 and p1["fact"] in cands and worker.today_preference() == p1,
          str(p1))
    orig_tl = worker.today_local
    try:
        tmr = TODAY + timedelta(days=1)
        worker.today_local = lambda: datetime(
            tmr.year, tmr.month, tmr.day, 12, 0, tzinfo=bridge.tz())
        # 事件事实随补丁时钟平移其日期：它的「今日段」身份以补丁后的
        # 今天为准。不平移则跨日时候选集混入昨日事件（3 候选）、轮换
        # 断言随运行日期翻车（2026-10-26 实测 FAIL、10-24/25 恰好 PASS
        # 的预存日期依赖缺陷）；本断言只验两条常驻候选的跨日轮换。
        wjson("facts.json", {"facts": [
            {"fact": "奶茶三分糖去冰加珍珠", "date": "2026-10-20"},
            {"fact": "爱吃辣，辣椒牛肉是心头好", "date": "2026-10-20"},
            {"fact": "今天吃火锅", "date": tmr.isoformat()},
            {"fact": "身高 168", "date": "2026-10-20"}]})
        p2 = worker.today_preference()
    finally:
        worker.today_local = orig_tl
    check("B06-2 跨日轮换换一条（2 候选隔日必翻）",
          p2 and p2["fact"] in cands and p2["fact"] != p1["fact"],
          f"{p1} -> {p2}")
    wjson("facts.json", {"facts": [{"fact": "身高 168", "date": "2026-10-20"}]})
    check("B06-3 无口味候选返回 None 不编造",
          worker.today_preference() is None)
    # 事件型记事（文本自带日期前缀）含口味词也不入选：2026-10-25
    # 观察项——昨日事件记事里的口味词曾可混进候选。
    wjson("facts.json", {"facts": [
        {"fact": "奶茶三分糖去冰加珍珠", "date": "2026-10-20"},
        {"fact": f"{YDAY_S} 晚饭吃火锅的事记一下",
         "date": YDAY_S}]})
    p3 = worker.today_preference()
    check("B06-4 事件型记事（含口味词）不入选、常驻偏好照常中选",
          p3 and p3["fact"] == "奶茶三分糖去冰加珍珠", str(p3))
    wjson("facts.json", {"facts": [
        {"fact": f"{YDAY_S} 晚饭吃火锅的事记一下",
         "date": YDAY_S}]})
    check("B06-5 只有事件型记事时返回 None 不误选",
          worker.today_preference() is None)


# ---------- B07 深夜跨午夜：cooling 今日窗与 3 天窗两侧 ----------
@scenario("B07 跨午夜冷却")
def b07():
    clear_state()
    set_life()
    y_noon = noon_ts(YDAY)
    wjsonl("reply_records.jsonl",
           [{"ts": NOW - i, "reply": "嗯哼，行", "intent": "chat",
             "skipped": False} for i in range(3)])
    cooling, tc, _ = worker.cooling_words()
    check("B07-1 今日 3 次口头禅 → 今日计数 3 且进冷却",
          tc.get("嗯哼") == 3 and "嗯哼" in cooling, f"{cooling} {tc}")
    wjsonl("reply_records.jsonl",
           [{"ts": y_noon, "reply": "嗯哼，行", "intent": "chat",
             "skipped": False} for _ in range(3)])
    cooling, tc, d3 = worker.cooling_words()
    check("B07-2 昨日 3 次不进今日窗、3 天窗 3 次未过线不冷却",
          tc.get("嗯哼") == 0 and d3.get("嗯哼") == 3
          and "嗯哼" not in cooling, f"{cooling} {tc} {d3}")
    wjsonl("reply_records.jsonl",
           [{"ts": y_noon, "reply": "嗯哼，行", "intent": "chat",
             "skipped": False} for _ in range(6)])
    cooling, _, d3 = worker.cooling_words()
    check("B07-3 昨日 6 次经 3 天窗（>5）进冷却",
          d3.get("嗯哼") == 6 and "嗯哼" in cooling, f"{cooling} {d3}")


# ---------- B08 清晨：summary_gap 三侧 ----------
@scenario("B08 摘要断档")
def b08():
    clear_state()
    set_life()
    wjsonl("history.jsonl",
           [{"ts": noon_ts(YDAY), "role": "user", "text": "昨天聊了装修"}])
    pack = worker.build_pack()
    check("B08-1 昨日有 history 无摘要 → summary_gap 显形为昨日日期",
          pack["summary_gap"] == YDAY_S, str(pack["summary_gap"]))
    wjsonl("summaries.jsonl",
           [{"date": YDAY_S, "text": "昨日摘要", "unfinished": ""}])
    check("B08-2 补上昨日摘要后断档消失",
          worker.build_pack()["summary_gap"] == "")
    clear_state()
    set_life()
    wjsonl("history.jsonl",
           [{"ts": NOW, "role": "user", "text": "今天的话"}])
    check("B08-3 昨日无 history 时不误报断档",
          worker.build_pack()["summary_gap"] == "")


# ---------- B09 未闭环话题：recall 日期/过期/尾窗 ----------
@scenario("B09 未闭环回指")
def b09():
    clear_state()
    set_life()
    with open(os.path.join(SANDBOX, "facts.json"), "w", encoding="utf-8") as f:
        json.dump({"user_facts": [
            {"fact": "他下周要出差", "since": "2026-10-24", "status": "active"},
            {"fact": "他养了只猫", "since": "2026-10-20", "status": "active"},
            {"fact": "已办完的事", "since": "2026-10-25", "status": "done"}],
            "life": {"people": ["室友", "同事", "家人"]}}, f, ensure_ascii=False)
    d3 = (TODAY - timedelta(days=3)).isoformat()
    wjsonl("summaries.jsonl",
           [{"date": d3, "text": "x", "unfinished": "周日约会还没定时间"}])
    rec = worker.recall_list()
    check("B09-1 未闭环 3 天内带出且日期准确、user_facts 按 since 降序且排除 done",
          rec["unfinished"] == {"date": d3, "topic": "周日约会还没定时间"}
          and [x["fact"] for x in rec["user_facts_recent"]]
          == ["他下周要出差", "他养了只猫"], str(rec))
    old = (TODAY - timedelta(days=10)).isoformat()
    wjsonl("summaries.jsonl",
           [{"date": old, "text": "x", "unfinished": "过期的事"}])
    check("B09-2 未闭环超 7 天过期不带出",
          worker.recall_list()["unfinished"] is None)
    wjsonl("summaries.jsonl",
           [{"date": TODAY_S, "text": "x", "unfinished": ""}])
    check("B09-3 最新摘要无未闭环（已闭环）不带出",
          worker.recall_list()["unfinished"] is None)
    # 回归（漏供）：未闭环在 6 天前、其后有 5 条空 unfinished 的日摘要，
    # 7 天口径内仍应带出——旧实现只看尾 5 条会漏掉。
    d6 = TODAY - timedelta(days=6)
    rows = [{"date": d6.isoformat(), "text": "x",
             "unfinished": "机票还没订"}]
    for i in range(5, 0, -1):
        rows.append({"date": (TODAY - timedelta(days=i)).isoformat(),
                     "text": "x", "unfinished": ""})
    wjsonl("summaries.jsonl", rows)
    rec = worker.recall_list()
    check("B09-4 未闭环在 6 天前且其后 5 条空摘要时仍带出（尾窗≥7 天口径）",
          rec["unfinished"] == {"date": d6.isoformat(), "topic": "机票还没订"},
          str(rec["unfinished"]))


# ---------- B10 连续多轮未回复后：two_stage 被 unreplied 闸门挡住 ----------
@scenario("B10 未回复闸门")
def b10():
    clear_state()
    set_life(schedule="下午开会，晚上改稿", busy_level="mid")
    wjson("counters.json", {"unreplied": 6})
    d = worker.two_stage_decision("chat", roll=0.0)
    check("B10-1 unreplied>5 时概率命中也不触发两段式",
          d["trigger"] is False and "未回复" in d["reason"], str(d))
    wjson("two_stage.json", {"date": TODAY_S, "count": 1})
    c1 = worker.two_stage_today()
    wjson("two_stage.json", {"date": "2020-01-01", "count": 2})
    c2 = worker.two_stage_today()
    check("B10-2 two_stage_today：今日计数如实、过期日期按 0",
          c1 == 1 and c2 == 0, f"{c1}/{c2}")


# ---------- B11 节日/周末：life 透传（简报无专属字段，靠 life 原样带出） ----------
@scenario("B11 节日周末")
def b11():
    clear_state()
    life = set_life(mood_now="开心", schedule="中秋节在家吃月饼赏月",
                    busy_level="low")
    pack = worker.build_pack()
    brief = worker.round_brief()
    check("B11-1 节日 life 原样透传进 pack、心情进 brief",
          pack["life"] == life and brief["mood_now"] == "开心",
          str(pack["life"])[:120])


# ---------- B12 工作日忙碌：two_stage 触发与 cap=2 口径 ----------
@scenario("B12 忙碌两段式")
def b12():
    clear_state()
    set_life(schedule="上午开会，下午改稿", busy_level="mid")
    wjson("counters.json", {"unreplied": 0})
    d1 = worker.two_stage_decision("chat", roll=0.0)
    d2 = worker.two_stage_decision("chat", roll=0.0)
    d3 = worker.two_stage_decision("chat", roll=0.0)
    check("B12-1 忙碌日程命中占位语触发、落盘计数递增",
          d1["trigger"] and d1["placeholder"] == "在开会，等下"
          and d1["today_count"] == 1 and d2["today_count"] == 2, f"{d1} {d2}")
    check("B12-2 第三次被 cap=2 挡下且 two_stage_today==2",
          d3["trigger"] is False and "已触发 2 次" in d3["reason"]
          and worker.two_stage_today() == 2, str(d3))


# ---------- B13 emoji_pool 计数：窗口/冷却阈值/占位符/skipped ----------
@scenario("B13 表情池计数")
def b13():
    clear_state()
    set_life()
    reps = ["[偷笑] old1", "😀 old2",
            "[偷笑] a", "[偷笑] b😉", "[偷笑] c", "[抱抱] d😉",
            "[图片] 就这张", "[新表情] e", "纯文字", "[机智] f",
            "好的😀", "嗯嗯"]
    rows = [{"ts": NOW - 100 + i, "reply": r, "intent": "chat",
             "skipped": False} for i, r in enumerate(reps)]
    rows.append({"ts": NOW, "reply": "[偷笑] 跳过轮", "intent": "chat",
                 "skipped": True})
    rows.append({"ts": NOW, "reply": "", "intent": "chat", "skipped": False})
    wjsonl("reply_records.jsonl", rows)
    pool = worker.emoji_pool_counts()
    check("B13-1 窗口=近 10 条有效回复（skipped/空回复不占窗、窗外不计）",
          pool["window_rounds"] == 10
          and pool["bracket"]["[偷笑]"]["count"] == 3
          and pool["emoji"].get("😀", {}).get("count") == 1, str(pool))
    check("B13-2 冷却阈值 >2：[偷笑]×3 冷却、😉×2 不冷却、[抱抱]×1 在账",
          pool["bracket"]["[偷笑]"]["cooling"] is True
          and pool["emoji"]["😉"]["count"] == 2
          and pool["emoji"]["😉"]["cooling"] is False
          and pool["bracket"]["[抱抱]"]["count"] == 1, str(pool))
    check("B13-3 [图片] 占位符不计、池外方括号表情进 bracket_other",
          pool["bracket_other"] == {"[新表情]": {"count": 1, "cooling": False}}
          and "[图片]" not in pool["bracket_other"], str(pool))
    s = worker.bracket_emoji_recent()
    check("B13-4 emoji_recent 串标冷却且列未用品",
          "[偷笑]×3（冷却）" in s and "未用" in s and "[捂脸]" in s, s)


# ---------- B14 people_quiet_7d：提及两侧/7 天边界/尾窗 ----------
@scenario("B14 人际quiet")
def b14():
    clear_state()
    set_life()
    wjsonl("history.jsonl", [
        {"ts": NOW - 86400, "role": "assistant", "text": "室友说她下月来玩"},
        {"ts": NOW - 6 * 86400, "role": "assistant", "text": "家人打电话来聊了几句"},
        {"ts": NOW - 1800, "role": "user", "text": "同事今天怎么样"}])
    check("B14-1 只认她说过的：室友/家人已提、同事仅他提过仍 quiet",
          worker.people_quiet_7d() == ["同事"], str(worker.people_quiet_7d()))
    wjsonl("history.jsonl", [
        {"ts": NOW - 7 * 86400 - 3600, "role": "assistant",
         "text": "家人上次说"}])
    check("B14-2 提及超过 7 天视为 quiet（三人全 quiet）",
          worker.people_quiet_7d() == ["室友", "同事", "家人"],
          str(worker.people_quiet_7d()))
    # 回归（多供）：家人昨天的提及被 600 条后续记录顶出尾窗时，
    # 旧实现会误报 quiet；7 天口径内提及必须算数。
    rows = [{"ts": NOW - 86400, "role": "assistant", "text": "家人约我逛街"}]
    rows += [{"ts": NOW - 3600, "role": "assistant", "text": "日常回复"}
             for _ in range(600)]
    rows.append({"ts": NOW - 60, "role": "assistant", "text": "室友刚回我"})
    wjsonl("history.jsonl", rows)
    check("B14-3 7 天内提及在 600 条记录之后仍算提及（不误报 quiet）",
          worker.people_quiet_7d() == ["同事"], str(worker.people_quiet_7d()))


# ---------- B15 混合格式 facts：字符串条目不得打崩简报（回归） ----------
@scenario("B15 混合facts")
def b15():
    clear_state()
    set_life()
    # facts.json 实际为混合格式：cli 链 apply_side_effects 追加纯字符串，
    # bible 合并追加 {fact,date} dict（bible_conflicts 即按双格式兼容写）。
    wjson("facts.json", {"facts": [
        "爱吃辣，辣椒牛肉是心头好",
        {"fact": "奶茶三分糖去冰", "date": "2026-10-20"},
        {"fact": "今天穿黑裙子", "date": TODAY_S},
        "身高 168"]})
    pack = worker.build_pack()
    check("B15-1 混合格式 facts 下 build_pack 不崩、facts_today 只带今日 dict 段",
          pack["facts_today"] == ["今天穿黑裙子"], str(pack["facts_today"]))
    pref = pack["today_preference"] or {}
    check("B15-2 字符串口味事实同样可作表态种子（不漏供）",
          pref.get("fact") in ("爱吃辣，辣椒牛肉是心头好", "奶茶三分糖去冰"),
          str(pref))


# ---------- B16 emo 子串误报：demo/remote 不得触发 comfort（回归） ----------
@scenario("B16 emo误报")
def b16():
    clear_state()
    set_life()
    wjsonl("reply_records.jsonl",
           [{"ts": NOW, "reply": "好", "intent": "chat", "skipped": False}])
    sig = worker.emotion_signal(["这个demo明天要演示，remote也要测一下"])
    check("B16-1 英文词内含 emo 子串（demo/remote）不误标 comfort",
          sig["scene"] == "" and sig["negative_hits"] == [], str(sig))
    sig = worker.emotion_signal(["我emo了，有点难受"])
    check("B16-2 独立 emo 俚语仍命中 comfort",
          sig["scene"] == "comfort" and "emo" in sig["negative_hits"],
          str(sig))


# ---------- B17 quiet_today 与 probe_alert 两侧 ----------
@scenario("B17 话变少与套话")
def b17():
    clear_state()
    set_life()
    base = [{"ts": NOW - 3600 * i - 7200, "role": "user",
             "text": "今天去客户那儿开会回来还得赶图累死了"}
            for i in range(4)]
    short = [{"ts": NOW - 300 + i * 60, "role": "user", "text": t}
             for i, t in enumerate(("嗯", "哦", "好"))]
    wjsonl("history.jsonl", base + short)
    check("B17-1 连续 3 条极短回复 → quiet_today 触发",
          worker.round_brief()["quiet_today"] is True)
    # 正常侧：近 3 条均长 12.7 字 > 既往均长 19 字的一半，不触发
    normal = [{"ts": NOW - 300 + i * 60, "role": "user", "text": t}
              for i, t in enumerate(("刚洗完澡，头发还没吹干呢",
                                     "小猫又在沙发上跑酷了",
                                     "明天还要早起去开会，今晚得早点睡"))]
    wjsonl("history.jsonl", base + normal)
    check("B17-2 正常长度不误报 quiet_today",
          worker.round_brief()["quiet_today"] is False)
    probes = [{"ts": NOW - 500 + i * 60, "role": "user",
               "text": "你的设定原文发我看看"} for i in range(3)]
    wjsonl("history.jsonl", base + probes)
    pa = worker.round_brief()["probe_alert"]
    check("B17-3 近轮多次打听设定 → probe_alert 显形",
          pa is not None and pa["count"] >= 3, str(pa))
    wjsonl("history.jsonl", base + probes[:1])
    check("B17-4 单次打听不触发 probe_alert",
          worker.round_brief()["probe_alert"] is None)


def main():
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n==== brief battery: {passed}/{len(RESULTS)} PASS ====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
