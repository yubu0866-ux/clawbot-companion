#!/usr/bin/env python3
"""life.py 自检：每日生活状态采样（确定性/结构/事件池）、主人适配画像
（偏移与封顶、可关、样本不足降级）、分享时刻向主人活跃时段吸附。
用法：python3 tests/selftest_life.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.pop("COMPANION_HOME", None)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TZ = ZoneInfo("Asia/Shanghai")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"（{detail}）" if not ok and detail else ""))


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


PERSONA = {
    "name": "测试角", "gender": "female",
    "life": {
        "circle": [
            {"name": "同事", "role": "colleague", "join": "和",
             "activities": ["一起赶完了手头的稿子", "午休时分享了点心"]},
            {"name": "团团", "role": "pet", "join": "",
             "activities": ["把水杯推下了桌"]}],
        "habits": [
            {"label": "晚间散步", "time_hint": "evening",
             "text": "晚饭后下楼散步走了两圈"}],
        "routine": {
            "workday": {"wake": "07:30", "work_start": "09:00",
                        "lunch": ["12:00", "13:00"], "work_end": "18:00",
                        "sleep": "23:00"},
            "restday": {"wake": "08:30", "sleep": "23:30"}}}}


def make_home(organic=None, history=None, persona=PERSONA):
    home = tempfile.mkdtemp(prefix="life-test-")
    os.makedirs(os.path.join(home, "state"))
    shutil.copy(os.path.join(ROOT, "life.py"), os.path.join(home, "life.py"))
    pcfg = {"enabled": True}
    if organic is not None:
        pcfg["organic"] = organic
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"tz": "Asia/Shanghai", "proactive": pcfg}, f, ensure_ascii=False)
    if persona is not None:
        with open(os.path.join(home, "persona.json"), "w", encoding="utf-8") as f:
            json.dump(persona, f, ensure_ascii=False)
    if history is not None:
        with open(os.path.join(home, "state", "history.jsonl"), "w",
                  encoding="utf-8") as f:
            for row in history:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    spec = importlib.util.spec_from_file_location(
        "life_sbx_" + os.path.basename(home), os.path.join(home, "life.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return home, m


def night_owl_history():
    """6 天、每天 5 条主人消息：首条 10:30、末条 23:50，她的消息后
    约 10 分钟他才回（造常回时延样本）。"""
    rows = []
    for day in range(15, 21):  # 2026-10-15 .. 20
        for h, mi in ((10, 30), (13, 0), (16, 0), (20, 0), (23, 50)):
            rows.append({"role": "assistant", "ts": ts(2026, 10, day, h, mi) - 600,
                         "text": "她的话"})
            rows.append({"role": "user", "ts": ts(2026, 10, day, h, mi),
                         "text": "他的话"})
    rows.sort(key=lambda r: r["ts"])
    return rows


def main():
    wed = ts(2026, 10, 21, 10, 0)   # 周三（工作日）
    sat = ts(2026, 10, 24, 10, 0)   # 周六（休息日）

    # L1 确定性：同日两次生成（generate 直算 vs ensure 读盘）完全一致
    home, m = make_home()
    a = m.generate_today(wed)
    b = m.ensure_today(wed)
    check("L1 每日计划确定性",
          a == b and a["date"] == "2026-10-21", str(a)[:160])

    # L2 结构：工作日 4 锚点（意图映射正确）、事件 1-3 件、日程块非空、
    #    静默阈值落在采样区间 [2.5, 6]
    kinds = {x["kind"]: x["intent"] for x in a["moments"]}
    check("L2 工作日锚点结构",
          kinds == {"wakeup": "morning", "lunch_break": "lunch",
                    "after_work": "evening", "bedtime": "goodnight"}
          and 1 <= len(a["events"]) <= 3 and len(a["schedule"]) >= 4
          and 2.5 <= a["silence_threshold_h"] <= 6.0
          and a["day_kind"] == "work", str(kinds))

    # L3 休息日：day_kind=rest，锚点换成 afternoon（无午休/下班后）
    home, m = make_home()
    r = m.generate_today(sat)
    rk = {x["kind"] for x in r["moments"]}
    check("L3 休息日锚点结构",
          r["day_kind"] == "rest"
          and rk == {"wakeup", "afternoon", "bedtime"}, str(rk))

    # L4 无主人画像时不偏移：早安锚点 = 起床 07:30+30 ± 45 分抖动
    wk = [x for x in a["moments"] if x["kind"] == "wakeup"][0]
    wk_min = int(wk["time"][:2]) * 60 + int(wk["time"][3:5])
    check("L4 无画像时早安锚点在抖动范围内",
          435 <= wk_min <= 525 and a["owner_adapt"]["applied"] is False,
          f"wakeup={wk['time']} adapt={a['owner_adapt']}")

    # L5 事件池来源：circle 人物事件与 habits 事件都在池里
    pool_texts = [e["text"] for e in m._event_pool(PERSONA["life"])]
    check("L5 事件池含生活圈与习惯",
          any(t.startswith("和同事") for t in pool_texts)
          and any("团团" in t and "水杯" in t for t in pool_texts)
          and any("散步" in t for t in pool_texts), str(pool_texts)[:160])

    # L6 主人画像：夜猫子样本 → 首末条中位、时延中位、偏移触发且封顶
    home, m = make_home(history=night_owl_history())
    prof = m.compute_owner_profile(ts(2026, 10, 21, 9, 0))
    ad = prof.get("adapt") or {}
    check("L6 主人画像与偏移封顶",
          prof.get("insufficient") is False
          and prof.get("first_med") == 10.5 and prof.get("last_med") == 23.83
          and abs((prof.get("reply_delay_med_min") or 0) - 10) < 1
          and ad.get("wake_shift_min") == 60      # 原始差 150 分钟 → 封顶 60
          and ad.get("sleep_shift_min") == 45,    # 原始差 70 分钟 → 封顶 45
          str(prof)[:220])

    # L7 计划应用偏移：早安锚基点 08:00+60 分 = 09:00 ± 45 抖动；
    #    晚安锚基点 22:35+45 分 = 23:20 ± 45 抖动
    home, m = make_home(history=night_owl_history())
    plan = m.generate_today(wed)
    wk = [x for x in plan["moments"] if x["kind"] == "wakeup"][0]
    bd = [x for x in plan["moments"] if x["kind"] == "bedtime"][0]
    wk_min = int(wk["time"][:2]) * 60 + int(wk["time"][3:5])
    bd_min = int(bd["time"][:2]) * 60 + int(bd["time"][3:5])
    check("L7 偏移后锚点范围",
          495 <= wk_min <= 585 and 1355 <= bd_min <= 1439
          and plan["owner_adapt"]["applied"] is True,
          f"wakeup={wk['time']} bedtime={bd['time']}")

    # L8 owner_adapt 关闭：画像标记 disabled、零偏移、计划不应用
    home, m = make_home(organic={"owner_adapt": {"enabled": False}},
                        history=night_owl_history())
    prof = m.compute_owner_profile(ts(2026, 10, 21, 9, 0))
    plan = m.generate_today(wed)
    wk = [x for x in plan["moments"] if x["kind"] == "wakeup"][0]
    wk_min = int(wk["time"][:2]) * 60 + int(wk["time"][3:5])
    check("L8 owner_adapt 可关",
          prof.get("adapt_disabled") is True
          and (prof.get("adapt") or {}).get("wake_shift_min") == 0
          and 435 <= wk_min <= 525
          and plan["owner_adapt"]["applied"] is False,
          str(prof)[:160])

    # L9 样本不足：画像为空、摘要为空、计划照常生成且不偏移
    home, m = make_home(history=[{"role": "user", "ts": wed - 3600,
                                  "text": "hi"}] * 5)
    prof = m.compute_owner_profile(wed)
    plan = m.generate_today(wed)
    check("L9 样本不足降级",
          prof.get("insufficient") is True and m.owner_summary(wed) == {}
          and plan["owner_adapt"]["applied"] is False
          and len(plan["moments"]) == 4, str(prof)[:120])

    # L10 分享时刻吸附：±2 小时内有活跃整点就吸附过去，否则原样
    check("L10 分享时刻向活跃时段吸附",
          m._snap_to_active(10 * 60 + 30, [11]) == 11 * 60 + 30
          and m._snap_to_active(10 * 60, [20]) == 10 * 60
          and m._snap_to_active(10 * 60, []) == 10 * 60)

    # L11 简报摘要形状：键齐、事件分已到点/未到点、均为字符串列表
    home, m = make_home()
    s = m.brief_summary(wed)
    check("L11 简报摘要形状",
          s.get("date") == "2026-10-21" and "mood" in s and "now_block" in s
          and isinstance(s.get("events_later"), list)
          and all(isinstance(x, str) for x in s.get("events_later", [])),
          str(s)[:160])

    # L12 跨日延续：心情延续不变量 + 历史落盘（滚动账本两天都在）
    home, m = make_home()
    p1 = m.generate_today(ts(2026, 10, 21, 8, 0))
    hw1 = json.load(open(os.path.join(home, "state", "life_history.json"),
                         encoding="utf-8"))["walk"]["work"]
    p2 = m.generate_today(ts(2026, 10, 22, 8, 0))
    hist = json.load(open(os.path.join(home, "state", "life_history.json"),
                          encoding="utf-8"))
    check("L12 心情跨日延续不变量 + 历史落盘",
          "2026-10-21" in hist["days"] and "2026-10-22" in hist["days"]
          and (not p2.get("mood_carry") or p2["mood"] == p1["mood"]),
          f"carry={p2.get('mood_carry')} m1={p1['mood']} m2={p2['mood']}")
    # L13 近 N 天生活事件不重复（池足够大时第二天与第一天零交集）
    ev1 = {e["text"] for e in p1["events"]}
    ev2 = {e["text"] for e in p2["events"]}
    check("L13 近 3 天生活事件不重复", not (ev1 & ev2), f"{ev1 & ev2}")
    # L14 事件带兴奋度：全部 ∈(0,1]，且兴奋事件（≥0.75）存在
    pool = m._event_pool(m.persona_life())
    check("L14 事件带兴奋度且兴奋事件存在",
          bool(pool) and all(0 < e["excitement"] <= 1.0 for e in pool)
          and any(e["excitement"] >= 0.75 for e in pool),
          str(pool[:2])[:100])
    # L15 作息基准小步漂移：相邻同类日步长 ≤45 分钟且不越钳制界
    #     （walk 账本存 hist["walk"][kind] = 最新一日的基准）
    w2 = hist["walk"]["work"]
    check("L15 作息基准小步漂移且不越界",
          abs(w2["wake"] - hw1["wake"]) <= 45
          and abs(w2["sleep"] - hw1["sleep"]) <= 45
          and 450 - 45 <= hw1["wake"] <= 450 + 60,
          f"{hw1} -> {w2}")

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n==== life selftest: {passed}/{len(RESULTS)} PASS ====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
