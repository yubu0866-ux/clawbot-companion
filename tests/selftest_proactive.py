#!/usr/bin/env python3
"""proactive.py 自检：临时目录 + 注入时间，逐项验证门控与触发优先级。
用法：python3 tests/selftest_proactive.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
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


def _race_child62(modpath, target, tag, n):
    # 模块级函数：Windows spawn 子进程按引用导入，嵌套函数无法 pickle；
    # fork 与 spawn 两路共用，参数均为可 pickle 的纯数据。
    spec = importlib.util.spec_from_file_location("race_mod62", modpath)
    mm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mm)
    payload = {"tag": tag, "pad": "x" * 20000, "n": 0}
    for i in range(n):
        payload["n"] = i
        mm.write_json(target, payload)


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


ORGAN_PERSONA = {
    "name": "测试角", "gender": "female",
    "life": {
        "circle": [
            {"name": "同事", "role": "colleague", "join": "和",
             "activities": ["一起赶完了手头的稿子"]},
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


def make_home(now, slots=None, ctx_age_h=2.0, extra_state=None, followups=None,
              history=None, rhythm=None, mode=None, organic=None, persona=None):
    home = tempfile.mkdtemp(prefix="pro-test-")
    os.makedirs(os.path.join(home, "state"))
    os.makedirs(os.path.join(home, "inbox"))
    shutil.copy(os.path.join(ROOT, "proactive.py"), os.path.join(home, "proactive.py"))
    shutil.copy(os.path.join(ROOT, "life.py"), os.path.join(home, "life.py"))
    pcfg = {"enabled": True, "daily_max": 3, "min_gap_hours": 3,
            "quiet_hours": [0, 7], "silence_hours": 5,
            "active_skip_min": 30, "max_context_age_hours": 24,
            "send_jitter_min": [0, 0],
            "slots": slots if slots is not None else
                     {"morning": {"p": 1.0, "window": ["07:30", "09:30"]}}}
    if mode is not None:
        pcfg["mode"] = mode
    if organic is not None:
        pcfg["organic"] = organic
    if persona is not None:
        with open(os.path.join(home, "persona.json"), "w", encoding="utf-8") as f:
            json.dump(persona, f, ensure_ascii=False)
    if rhythm is not None:
        pcfg["rhythm"] = {"impulse_p": 0.0, "photo_p": 0.0, "video_p": 0.0, **rhythm}
    else:
        pcfg["rhythm"] = {"impulse_p": 0.0, "photo_p": 0.0, "video_p": 0.0}
    cfg = {"tz": "Asia/Shanghai", "proactive": pcfg}
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    st = {"date": None, "sent_count": 0, "last_sent_at": None, "last_intent": None,
          "awaiting_reply": False, "slot_rolls": {}, "followups": followups or [],
          "enabled": True, "started_on": "2026-10-01"}
    if extra_state:
        st.update(extra_state)
    with open(os.path.join(home, "state", "proactive.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    with open(os.path.join(home, "state", "context.json"), "w", encoding="utf-8") as f:
        json.dump({"context_token": "tok-test", "received_at": now - ctx_age_h * 3600,
                   "stale": False}, f)
    with open(os.path.join(home, "state", "counters.json"), "w", encoding="utf-8") as f:
        json.dump({"unreplied": 0}, f)
    with open(os.path.join(home, "state", "life.json"), "w", encoding="utf-8") as f:
        json.dump({"schedule": "下午赶稿", "small_events": ["小猫推倒了水杯"],
                   "mood_now": "平静", "busy_level": "mid"}, f)
    with open(os.path.join(home, "inbox", "m1.json"), "w", encoding="utf-8") as f:
        json.dump({"message_id": "m1", "from_user_id": "user1@im.wechat",
                   "text": "嗯", "create_time_ms": 1}, f)
    if history is not None:
        with open(os.path.join(home, "state", "history.jsonl"), "w",
                  encoding="utf-8") as f:
            for row in history:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    spec = importlib.util.spec_from_file_location("pro_sbx_" + os.path.basename(home),
                                                  os.path.join(home, "proactive.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return home, m


def main():
    t0830 = ts(2026, 10, 21, 8, 30)
    # 1. followup 到期优先于时段
    home, m = make_home(t0830, followups=[{"about": "他的面试", "after_ts": t0830 - 60,
                                           "created": t0830 - 86400}])
    d = m.decide(now=t0830)
    check("1 followup 到期 → due 且意图为 followup",
          d.get("due") and d.get("intent") == "followup" and d.get("about") == "他的面试"
          and d.get("to_user_id") == "user1@im.wechat", str(d)[:120])
    # 2. 时段命中（p=1.0）
    home, m = make_home(t0830)
    d = m.decide(now=t0830)
    check("2 时段窗口内 p=1 → due morning",
          d.get("due") and d.get("intent") == "morning", str(d)[:120])
    d2 = m.decide(now=t0830)
    check("2b 同一时段命中后不重复触发", not d2.get("due"), str(d2)[:80])
    # 3. 静默时段
    home, m = make_home(ts(2026, 10, 21, 3, 0))
    check("3 凌晨 3 点静默时段不发", not m.decide(now=ts(2026, 10, 21, 3, 0)).get("due"))
    # 4. 未回复并存不再是闸（2026-10-27 情绪模型定版，旧并存上限已删）：
    #     已有 3 条没被回复 → 照常按时段触发，未回复只进 mood 记账
    home, m = make_home(t0830, extra_state={
        "log": [{"ts": t0830 - 1800, "intent": "morning", "replied": None},
                {"ts": t0830 - 3600, "intent": "lunch", "replied": None},
                {"ts": t0830 - 5400, "intent": "impulse", "replied": None}]})
    _cfgp = os.path.join(home, "config.json")
    _cfg = json.load(open(_cfgp))
    _cfg["proactive"]["daily_max"] = 10
    json.dump(_cfg, open(_cfgp, "w"), ensure_ascii=False)
    d = m.decide(now=t0830)
    check("4 三条未回复并存 → 不再拦截，照常 due",
          d.get("due") and d.get("intent") == "morning", str(d)[:80])
    # 4b. 两条全是作废（没送达）→ 不算未回复、不进 streak，照常触发
    #     （2026-10-22 真实事故：晚安+早安两条作废堵死午饭档；新口径下
    #     作废条目连情绪记账都不参与）
    home, m = make_home(t0830, extra_state={
        "log": [{"ts": t0830 - 3600, "intent": "goodnight", "replied": None, "voided": True},
                {"ts": t0830 - 7200, "intent": "morning", "replied": None, "voided": True}]})
    d = m.decide(now=t0830)
    st4b = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    check("4b 两条作废不算未回复 → 照常 due 且 streak=0",
          d.get("due") and (st4b.get("mood") or {}).get("streak", 0) == 0,
          str(d)[:80])
    # 5. 观察期上限（started_on=今天 → 基线 4 条，滚动 24 小时已发 4 条）
    today = "2026-10-21"
    home, m = make_home(t0830, extra_state={
        "started_on": today, "date": today,
        "log": [{"ts": t0830 - 900, "intent": "morning", "replied": None},
                {"ts": t0830 - 1800, "intent": "morning", "replied": None},
                {"ts": t0830 - 3600, "intent": "morning", "replied": None},
                {"ts": t0830 - 7200, "intent": "lunch", "replied": None}]})
    d = m.decide(now=t0830)
    check("5 观察期滚动 24 小时上限 4 条", not d.get("due") and "上限" in d.get("reason", ""),
          str(d)[:80])
    # 6. context 太旧
    home, m = make_home(t0830, ctx_age_h=30)
    d = m.decide(now=t0830)
    check("6 context 超过 24 小时 → 不发", not d.get("due") and "太旧" in d.get("reason", ""),
          str(d)[:80])
    # 7. 刚聊过（10 分钟前有消息）
    home, m = make_home(t0830, ctx_age_h=10 / 60)
    check("7 30 分钟内刚聊过 → 不打扰", not m.decide(now=t0830).get("due"))
    # 8. 长静默触发（无 slots，下午，他 6 小时没说话）
    t1500 = ts(2026, 10, 21, 15, 0)
    home, m = make_home(t1500, slots={}, ctx_age_h=6)
    m.SILENCE_P = 1.0
    d = m.decide(now=t1500)
    check("8 长静默 6 小时 → due silence",
          d.get("due") and d.get("intent") == "silence", str(d)[:120])
    # 8b. 长静默不在深夜触发
    t2330 = ts(2026, 10, 21, 23, 30)
    home, m = make_home(t2330, slots={}, ctx_age_h=6)
    m.SILENCE_P = 1.0
    check("8b 23:30 长静默不触发（窗口外）", not m.decide(now=t2330).get("due"))
    # 9. sent 记账
    home, m = make_home(t0830, followups=[{"about": "他的面试", "after_ts": 1, "created": 1}])
    st = m.mark_sent("followup", "他的面试")
    check("9 sent 记账（计数+awaiting+移除 followup）",
          st["sent_count"] == 1 and st["awaiting_reply"] is True and st["followups"] == [])
    # 10. 关闭开关
    home, m = make_home(t0830, extra_state={"enabled": False})
    check("10 #主动关 后不发", not m.decide(now=t0830).get("due"))

    # ---- 节奏优化 ----
    def profile_history(days, first_h=9, last_h=23):
        rows = []
        for d in days:
            for hh, mm in ((first_h, 0), (12, 30), (21, 0), (last_h, 30)):
                rows.append({"ts": ts(2026, 10, d, hh, mm), "role": "user",
                             "text": "x"})
        return rows

    hist6 = profile_history([15, 16, 17, 18, 19, 20])
    # 11. 作息档案：他 9 点才起，早安窗口移到 8:30-10:30，8:00 不发、9:00 发
    home, m = make_home(ts(2026, 10, 21, 8, 0), history=hist6)
    d_early = m.decide(now=ts(2026, 10, 21, 8, 0))
    home, m = make_home(ts(2026, 10, 21, 9, 0), history=hist6)
    d_ok = m.decide(now=ts(2026, 10, 21, 9, 0))
    check("11 早安跟着他的作息走（9 点起 → 8 点不发、9 点发）",
          not d_early.get("due") and d_ok.get("due") and d_ok.get("intent") == "morning",
          f"early={d_early.get('reason')} ok={d_ok.get('intent')}")
    # 12. 周末早安再推迟 1 小时（周六 9:00 不发、10:00 发）
    home, m = make_home(ts(2026, 10, 24, 9, 0),
                        history=profile_history([18, 19, 20, 21, 22, 23]))
    d_sat9 = m.decide(now=ts(2026, 10, 24, 9, 0))
    home, m = make_home(ts(2026, 10, 24, 10, 0),
                        history=profile_history([18, 19, 20, 21, 22, 23]))
    d_sat10 = m.decide(now=ts(2026, 10, 24, 10, 0))
    check("12 周末早安推迟 1 小时",
          not d_sat9.get("due") and d_sat10.get("due"),
          f"sat9={d_sat9.get('reason')} sat10={d_sat10.get('intent')}")
    # 13. 傍晚静默 3 小时就触发，下午仍要 5 小时
    home, m = make_home(ts(2026, 10, 21, 19, 0), slots={}, ctx_age_h=3.5)
    m.SILENCE_P = 1.0
    d_eve = m.decide(now=ts(2026, 10, 21, 19, 0))
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=3.5)
    m.SILENCE_P = 1.0
    d_aft = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("13 傍晚静默阈值 3 小时（下午仍 5 小时）",
          d_eve.get("due") and d_eve.get("intent") == "silence" and not d_aft.get("due"),
          f"eve={d_eve.get('intent')} aft={d_aft.get('reason')}")
    # 14. 随机间隔：sent 后 1 小时内被 next_allowed_at 拦住
    home, m = make_home(t0830)
    st = m.mark_sent("morning")
    in_gap = st.get("next_allowed_at", 0) - time.time()
    d = m.decide(now=time.time() + 3600)
    check("14 主动间隔随机化（2.5-4.5 小时）且生效",
          2.4 * 3600 < in_gap < 4.6 * 3600 and not d.get("due"),
          f"gap_h={in_gap / 3600:.2f} reason={d.get('reason')}")
    # 15. 二连发标记（double_p=1 时 silence 必带 double）
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"double_p": 1.0, "gap_hours": [2.5, 4.5],
                                "silence_evening_hours": 3})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("15 二连发 roll（double_p=1 → double=true）",
          d.get("due") and d.get("double") is True, str(d)[:100])

    # ---- 节奏优化二轮：回应率浮动 + 滚动 24h + 冷却 ----
    # 16. 滚动 24 小时：昨天发的也算，3 条满了今天 0 条也不发
    log3 = [{"ts": t0830 - 9.5 * 3600, "intent": "goodnight", "replied": True},
            {"ts": t0830 - 5 * 3600, "intent": "morning", "replied": True},
            {"ts": t0830 - 3 * 3600, "intent": "lunch", "replied": True}]
    home, m = make_home(t0830, extra_state={"date": today, "sent_count": 0, "log": log3})
    d = m.decide(now=t0830)
    check("16 滚动 24 小时上限（跨自然日也算）",
          not d.get("due") and "滚动" in d.get("reason", ""), str(d)[:80])
    # 17. 回应率 100%（8 条样本）→ 上限仍封顶基线 3（不越用户定版），
    #     24 小时内已发 2 条时第 3 条可发
    log8 = [{"ts": t0830 - (50 + i) * 3600, "intent": "morning", "replied": True}
            for i in range(6)] + \
           [{"ts": t0830 - 5 * 3600, "intent": "morning", "replied": True},
            {"ts": t0830 - 3 * 3600, "intent": "lunch", "replied": True}]
    home, m = make_home(t0830, extra_state={"log": log8})
    cap = m.effective_cap(m.load_state(), m.cfg()["proactive"], today)
    d = m.decide(now=t0830)
    check("17 回应率再高上限也只到 3，第 3 条可发",
          cap == 3 and d.get("due") and d.get("intent") == "morning",
          f"cap={cap} d={str(d)[:80]}")
    # 18. 连续被晾 → 不再冷却拦截（2026-10-27 撤除冷却）：只记 streak=2
    #     进情绪模型，decide 的任何结果里都不许再出现「冷却」拦截
    log_miss = [{"ts": t0830 - 40 * 3600, "intent": "morning", "replied": True},
                {"ts": t0830 - 30 * 3600, "intent": "lunch", "replied": False},
                {"ts": t0830 - 13 * 3600, "intent": "evening", "replied": False}]
    home, m = make_home(t0830, extra_state={"log": log_miss})
    d = m.decide(now=t0830)
    st18 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    check("18 连续两条被晾 → 无冷却拦截、streak 记为 2",
          "冷却" not in d.get("reason", "")
          and (st18.get("mood") or {}).get("streak") == 2,
          str(d)[:80])
    # 19. 回应结算：主动发出后他回了消息 → 台账标 replied=True
    home, m = make_home(t0830, ctx_age_h=2,
                        extra_state={"log": [{"ts": t0830 - 3 * 3600,
                                              "intent": "morning", "replied": None}]})
    m.decide(now=t0830)
    st_after = json.load(open(os.path.join(home, "state", "proactive.json"),
                              encoding="utf-8"))
    check("19 他回过消息 → 台账结算为已回复",
          st_after["log"][0]["replied"] is True, str(st_after["log"])[:80])

    # ---- 节奏优化三轮 ----
    import types as _types
    # 20. awaiting 解锁：上一条被晾已结算 → 不再永久沉默
    home, m = make_home(t0830, extra_state={
        "awaiting_reply": True, "last_sent_at": t0830 - 13 * 3600,
        "log": [{"ts": t0830 - 13 * 3600, "intent": "morning", "replied": False}]})
    d = m.decide(now=t0830)
    st_after = json.load(open(os.path.join(home, "state", "proactive.json"),
                              encoding="utf-8"))
    check("20 被晾一条后 awaiting 自动解锁（不永久沉默）",
          d.get("due") and st_after["awaiting_reply"] is False, str(d)[:80])
    # 21a. 时段连败保底：p=0 但连败 2 天 → 今天必中
    home, m = make_home(t0830, slots={"morning": {"p": 0.0, "window": ["07:30", "09:30"]}},
                        extra_state={"slot_streaks": {"morning": {"hit": 0, "miss": 2}}})
    d = m.decide(now=t0830)
    check("21a 连败 2 天保底命中", d.get("due") and d.get("intent") == "morning",
          str(d)[:80])
    # 21b. 连中降频：p=1 但连中 2 天 → 概率减半，roll 0.6 未中
    home, m = make_home(t0830, extra_state={"slot_streaks": {"morning": {"hit": 2, "miss": 0}}})
    m.random = _types.SimpleNamespace(random=lambda: 0.6, randint=lambda a, b: a,
                                      uniform=lambda a, b: a)
    d = m.decide(now=t0830)
    check("21b 连中 2 天概率减半", not d.get("due"), str(d)[:80])
    # 22. followup 过期：约定时间过去 72 小时 → 自动作废
    home, m = make_home(t0830, slots={"morning": {"p": 0.0, "window": ["07:30", "09:30"]}},
                        followups=[{"about": "他的面试", "after_ts": t0830 - 72 * 3600,
                                    "created": t0830 - 96 * 3600}])
    d = m.decide(now=t0830)
    st_after = json.load(open(os.path.join(home, "state", "proactive.json"),
                              encoding="utf-8"))
    check("22 followup 超 48 小时过期作废",
          not d.get("due") and st_after["followups"] == [], str(d)[:80])
    # 23. 长静默防重复：昨天那条被晾 → 今天不用同一招；昨天被接住 → 今天可用
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        extra_state={"log": [{"ts": ts(2026, 10, 20, 15, 0),
                                              "intent": "silence", "replied": False}]})
    m.SILENCE_P = 1.0
    d_miss = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        extra_state={"log": [{"ts": ts(2026, 10, 20, 15, 0),
                                              "intent": "silence", "replied": True}]})
    m.SILENCE_P = 1.0
    d_ok = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("23 长静默防重复（被晾才隔日）",
          not d_miss.get("due") and d_ok.get("due")
          and d_ok.get("intent") == "silence",
          f"miss={d_miss.get('reason')} ok={d_ok.get('intent')}")
    # 24. 秒回型用户：间隔仍严格落在 config 定版 gap_hours 内
    # （2026-10-21 的 0.75-1.5 浮动覆盖是基线 2.5-4.5 时代的产物，
    # 2026-10-22 定版 [0.4, 1.0] 后反转为拉长间隔，2026-10-23 复盘移除）
    fast_log = [{"ts": t0830 - (30 + i) * 3600, "intent": "morning",
                 "replied": True, "latency_sec": 600} for i in range(5)]
    home, m = make_home(t0830, extra_state={"log": fast_log},
                        rhythm={"gap_hours": [0.4, 1.0]})
    t0 = time.time()
    st = m.mark_sent("morning")
    delta_h = (st["next_allowed_at"] - t0) / 3600
    check("24 他一贯秒回 → 间隔仍在定版 0.4-1.0 小时内", 0.39 < delta_h < 1.01,
          f"delta={delta_h:.2f}h")

    # ---- 节奏优化四轮（25 于 2026-10-27 改新口径） ----
    # 25. followup 与上限的豁免耦合已撤：24h 已发满 3 条（daily_max 填
    #     数字时），到期话题同样被上限拦下；followup 只保留「惦记未完
    #     话题」的触发语义本身（默认无上限时照常优先触发，见用例 1）
    home, m = make_home(t0830, extra_state={"date": today, "sent_count": 0, "log": log3},
                        followups=[{"about": "他的面试", "after_ts": t0830 - 60,
                                    "created": t0830 - 86400}])
    d = m.decide(now=t0830)
    check("25 到期 followup 不再豁免条数上限（已满照拦）",
          not d.get("due") and "滚动" in d.get("reason", ""), str(d)[:80])
    # 26. 形式裁定：photo 权重独占（photo_p=1、video_p=0）时闲聊类主动
    # 变照片形式；两权重同为 0 时回落纯文字
    # （photo_self_p 钉 0，本条专验旧主体奇偶轮换；自拍主体见 66）
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"photo_p": 1.0, "photo_self_p": 0.0,
                                "double_p": 0.0})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m2 = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                         rhythm={"photo_p": 0.0, "double_p": 0.0})
    m2.SILENCE_P = 1.0
    d2 = m2.decide(now=ts(2026, 10, 21, 15, 0))
    check("26 主动形式轮换（照片/文字）",
          d.get("form") == "photo" and d.get("photo_subject") == "生活场景"
          and d.get("double") is False and d2.get("form") == "text",
          f"photo={d.get('form')}/{d.get('photo_subject')} text={d2.get('form')}")
    # 27. 周末白天静默阈值 3 小时（周六下午 3.5 小时没聊即可触发）
    home, m = make_home(ts(2026, 10, 24, 15, 0), slots={}, ctx_age_h=3.5)
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 24, 15, 0))
    check("27 周末静默阈值放宽到 3 小时",
          d.get("due") and d.get("intent") == "silence", str(d)[:80])

    # ---- 节奏优化五轮 ----
    # 28. 上限回归：回应率低降额仍在（6 条样本只回 1 条，<20% → 上限 2，
    #     2026-10-22 放开前旧档为 1）
    low_log = [{"ts": t0830 - (30 + i) * 3600, "intent": "morning",
                "replied": (i == 0)} for i in range(6)]
    home, m = make_home(t0830, extra_state={"log": low_log})
    cap = m.effective_cap(m.load_state(), m.cfg()["proactive"], today)
    check("28 回应率 <20% → 上限降到 2", cap == 2, f"cap={cap}")
    # 29. 时段问候避开刚聊完：40 分钟前刚聊过，早安不发；到期 followup 照发
    home, m = make_home(t0830, ctx_age_h=40 / 60)
    d_slot = m.decide(now=t0830)
    home, m = make_home(t0830, ctx_age_h=40 / 60,
                        followups=[{"about": "他的面试", "after_ts": t0830 - 60,
                                    "created": t0830 - 3600}])
    d_fu = m.decide(now=t0830)
    check("29 刚聊完 1 小时内不发时段问候（followup 不受影响）",
          not d_slot.get("due") and d_fu.get("due")
          and d_fu.get("intent") == "followup",
          f"slot={d_slot.get('reason')} fu={d_fu.get('intent')}")
    # 30. 随机小心情：impulse_p=1 时活跃时段可触发；当天已发满 2 次后不再触发
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=2,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 2})
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=2,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 2},
                        extra_state={"log": [
                            {"ts": ts(2026, 10, 21, 12, 0), "intent": "impulse",
                             "replied": None},
                            {"ts": ts(2026, 10, 21, 13, 0), "intent": "impulse",
                             "replied": None}]})
    d2 = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("30 随机小心情触发且每天至多 2 次",
          d.get("due") and d.get("intent") == "impulse" and not d2.get("due"),
          f"d={d.get('intent')} d2={d2.get('reason')}")

    # 31. 内容口味轮换：空历史 → 第一种；最近 3 次用过的跳过；sent 后记账
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6)
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        extra_state={"flavor_history": ["日常分享", "近况问候", "轻松玩笑"]})
    m.SILENCE_P = 1.0
    d2 = m.decide(now=ts(2026, 10, 21, 15, 0))
    st = m.mark_sent("silence")
    check("31 口味轮换（跳过最近用过的 + sent 记账）",
          d.get("flavor") in m.DEFAULT_FLAVORS
          and d2.get("flavor") in set(m.DEFAULT_FLAVORS) - {"日常分享", "近况问候", "轻松玩笑"}
          and st["flavor_history"][-1] == d2.get("flavor"),
          f"f1={d.get('flavor')} f2={d2.get('flavor')} hist={st.get('flavor_history')}")
    # 32. 未回复并存：只有 1 条没被回复时，允许再发 1 条
    home, m = make_home(t0830, extra_state={
        "awaiting_reply": True, "last_sent_at": t0830 - 3600,
        "next_allowed_at": t0830 - 60,
        "log": [{"ts": t0830 - 3600, "intent": "morning", "replied": None}]})
    d = m.decide(now=t0830)
    check("32 一条未回复时允许并存第二条",
          d.get("due") and d.get("intent") == "morning", str(d)[:80])
    # 33. 视频形式：video 权重独占时闲聊类主动变视频（Muse 内置生成）；
    # 当日计数再高也不拦截（2026-10-24 定档「基本不限」取消条数上限，
    # 计数只作统计）；sent 记账落 last_video_date 并给 video_sends_today +1
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"photo_p": 0.0, "video_p": 1.0,
                                "video_self_p": 0.0, "double_p": 0.0})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    st3 = m.mark_sent("silence")
    home2, m2 = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                          extra_state={"date": "2026-10-21",
                                       "video_sends_today": 5},
                          rhythm={"photo_p": 0.0, "video_p": 1.0,
                                  "video_self_p": 0.0, "double_p": 0.0})
    m2.SILENCE_P = 1.0
    d2 = m2.decide(now=ts(2026, 10, 21, 15, 0))
    # 注：mark_sent 用真实墙钟日期落 last_video_date，期望值不能写死模拟日期，
    # 否则过了零点这条必挂（2026-10-22 实测踩中），改按运行时真实日期比。
    real_today = datetime.now(TZ).date().isoformat()
    check("33 主动视频形式（命中/记账/计数高不拦截）",
          d.get("form") == "video" and d.get("video_subject") == "生活场景"
          and d.get("double") is False and d2.get("form") == "video"
          and st3.get("last_video_date") == real_today
          and st3.get("video_sends_today") == 1,
          f"video={d.get('form')}/{d.get('video_subject')} again={d2.get('form')} "
          f"lvd={st3.get('last_video_date')} cnt={st3.get('video_sends_today')}")
    # 34. 提前毕业不被回应率分支挡死：观察期 + 5 条结算全被回 → cap=daily_max
    log5 = [{"ts": t0830 - (i + 1) * 3600, "intent": "morning", "replied": True}
            for i in range(5)]
    home, m = make_home(t0830, extra_state={"date": today, "started_on": today,
                                            "log": log5})
    cap = m.effective_cap(m.load_state(), {"daily_max": 10}, today)
    check("34 观察期全回 5 条仍毕业到 daily_max",
          cap == 10, f"cap={cap}")
    # 35. 作废对账：发送端以「已有新对话」作废的主动不算已送达，
    # 台账标 voided 后不进回应率与 24h 条数；普通发送失败不误标
    home, m = make_home(t0830, extra_state={
        "date": today, "sent_count": 1,
        "log": [{"ts": t0830 - 3600 + 4, "intent": "impulse", "replied": True,
                 "latency_sec": 90},
                {"ts": t0830 - 1800 + 4, "intent": "impulse", "replied": None}]})
    fdir = os.path.join(home, "outbox", "failed")
    os.makedirs(fdir)
    with open(os.path.join(fdir, f"pro-{int(t0830 - 3600)}-1.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{int(t0830 - 3600)}",
                   "error": "已有新对话"}, f)
    with open(os.path.join(fdir, f"pro-{int(t0830 - 1800)}-1.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{int(t0830 - 1800)}",
                   "error": "Timeout: 网络超时"}, f)
    st = m.load_state()
    changed = m.reconcile_voided(st)
    e1, e2 = st["log"][0], st["log"][1]
    check("35 作废主动对账（标 voided/普通失败不误标/统计剔除）",
          changed and e1.get("voided") and e1.get("replied") is None
          and not e2.get("voided")
          and m.reply_stats(st)["n"] == 0
          and m.sent_last_24h(st, t0830) == 1
          and st["sent_count"] == 0,
          f"changed={changed} e1={e1} e2voided={e2.get('voided')} "
          f"sent_count={st.get('sent_count')}")
    # 53. 作废对账匹配口径：message_id 取自含 jitter 的 send_after 时可比
    # 记账时刻晚（delta 为负，旧 0<=delta 窗口系统性漏对账，
    # 2026-10-23 13:18 视频条实测 delta=-53）；带 message_id 的台账走
    # 精确匹配、不受时间窗口限制
    mid53 = int(t0830 - 3600) + 53
    home, m = make_home(t0830, extra_state={
        "date": today, "sent_count": 1,
        "log": [{"ts": t0830 - 3600, "intent": "impulse", "replied": True,
                 "latency_sec": 90}]})
    fdir = os.path.join(home, "outbox", "failed")
    os.makedirs(fdir)
    with open(os.path.join(fdir, f"pro-{mid53}-2.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{mid53}", "error": "已有新对话"}, f)
    st = m.load_state()
    ch53 = m.reconcile_voided(st)
    ok53a = ch53 and st["log"][0].get("voided") and st["sent_count"] == 0
    mid53b = int(t0830 - 2000)
    home, m = make_home(t0830, extra_state={
        "date": today, "sent_count": 1,
        "log": [{"ts": t0830 - 7200, "intent": "impulse", "replied": True,
                 "message_id": f"pro-{mid53b}"}]})
    fdir = os.path.join(home, "outbox", "failed")
    os.makedirs(fdir)
    with open(os.path.join(fdir, f"pro-{mid53b}-2.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{mid53b}", "error": "已有新对话"}, f)
    st = m.load_state()
    ch53b = m.reconcile_voided(st)
    ok53b = ch53b and st["log"][0].get("voided") and st["sent_count"] == 0
    check("53 作废对账（负 delta 兜底/message_id 精确匹配）",
          bool(ok53a and ok53b), f"neg={ok53a} exact={ok53b}")

    # 54. 部分送达建模（partial）：同组文字已进 sent 时，媒体条作废/
    # 最终失败不整组作废、媒体改绑挽回后补达也标 partial——消息主体
    # 已送达，不清 replied、不回减 sent_count，与整组 voided 区分。
    def _mk54(mid, failed_err=None, rescued=False):
        home, m = make_home(t0830, extra_state={
            "date": today, "sent_count": 1,
            "log": [{"ts": mid + 4, "intent": "impulse", "replied": None,
                     "message_id": f"pro-{mid}"}]})
        sdir = os.path.join(home, "outbox", "sent")
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, f"pro-{mid}-1.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"message_id": f"pro-{mid}"}, f)
        if rescued:
            with open(os.path.join(sdir, f"pro-{mid}-2.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"message_id": f"pro-{mid}", "rescued": True}, f)
        if failed_err is not None:
            fdir = os.path.join(home, "outbox", "failed")
            os.makedirs(fdir, exist_ok=True)
            with open(os.path.join(fdir, f"pro-{mid}-2.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"message_id": f"pro-{mid}",
                           "error": failed_err}, f)
        st = m.load_state()
        ch = m.reconcile_voided(st)
        return ch, st["log"][0], st["sent_count"]

    ch54a, e54a, n54a = _mk54(int(t0830 - 3600), failed_err="已有新对话")
    ok54a = ch54a and e54a.get("partial") and not e54a.get("voided") \
        and n54a == 1
    ch54b, e54b, n54b = _mk54(int(t0830 - 1800),
                              failed_err="RuntimeError: CDN 上传失败 HTTP 500")
    ok54b = ch54b and e54b.get("partial") and not e54b.get("voided") \
        and n54b == 1
    ch54c, e54c, n54c = _mk54(int(t0830 - 900), rescued=True)
    ok54c = ch54c and e54c.get("partial") and not e54c.get("voided") \
        and n54c == 1
    check("54 部分送达对账（媒体作废/媒体终败/挽回补达均标 partial）",
          bool(ok54a and ok54b and ok54c),
          f"void={ok54a} hardfail={ok54b} rescued={ok54c}")

    # 55. 冻结误标转正：条目在 partial 建模上线前已被旧口径标 voided、
    # 同组文字其实已进 sent → 转正 partial、按 history 补结算、计数
    # 回补；真作废条目保持 voided；二次运行幂等。
    mid55 = int(t0830 - 3600)
    mid55v = int(t0830 - 7200)
    home, m = make_home(t0830, extra_state={
        "date": today, "sent_count": 0,
        "log": [{"ts": mid55 + 3, "intent": "lunch", "replied": None,
                 "voided": True},
                {"ts": mid55v, "intent": "impulse", "replied": None,
                 "voided": True}]},
        history=[{"role": "user", "ts": mid55 + 250, "text": "在吃呢"}])
    sdir = os.path.join(home, "outbox", "sent")
    fdir = os.path.join(home, "outbox", "failed")
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(fdir, exist_ok=True)
    with open(os.path.join(sdir, f"pro-{mid55}-1.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{mid55}"}, f)
    with open(os.path.join(fdir, f"pro-{mid55}-2.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{mid55}", "error": "已有新对话"}, f)
    with open(os.path.join(fdir, f"pro-{mid55v}-1.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{mid55v}", "error": "已有新对话"}, f)
    st = m.load_state()
    ch55 = m.reconcile_voided(st)
    e55p, e55v = st["log"][0], st["log"][1]
    ok55a = ch55 and e55p.get("partial") and not e55p.get("voided") \
        and e55p.get("replied") is True \
        and abs((e55p.get("latency_sec") or 0) - 250) < 1 \
        and st["sent_count"] == 1
    ok55b = e55v.get("voided") and not e55v.get("partial")
    ch55b = m.reconcile_voided(st)
    ok55c = (not ch55b) and st["sent_count"] == 1
    check("55 冻结误标转正 partial（补结算+回补，真作废不动，幂等）",
          bool(ok55a and ok55b and ok55c),
          f"conv={ok55a} keepvoid={ok55b} idem={ok55c} e={e55p}")

    # 56. 口味轮换：近 3 次避重恒成立，且全味长跑全可达（旧首-fit
    # 实现下排末位的「回忆细节」结构性不可达）
    import random as _rnd
    _rnd.seed(20261023)
    st56 = {"flavor_history": []}
    picks = []
    ok56_avoid = True
    for _ in range(200):
        f56 = m.pick_flavor(st56, "impulse")
        if f56 in picks[-3:]:
            ok56_avoid = False
        picks.append(f56)
        st56["flavor_history"] = (st56["flavor_history"] + [f56])[-5:]
    check("56 口味全味可达且近 3 次避重",
          ok56_avoid and set(picks) == set(m.DEFAULT_FLAVORS),
          f"avoid={ok56_avoid} seen={sorted(set(picks))}")

    # 56b. 去重窗扩到近 8（L7）：窗内出现过的先排除；全味（现 7 种）都在
    # 近 8 次内时挑最久没用过的（上次出现最靠前），轮转间隔拉满
    st56b = {"flavor_history": ["话题续接", "日常分享", "近况问候",
                                "轻松玩笑", "回忆细节", "小见闻分享",
                                "关心提醒", "日常分享"]}
    check("56b-1 近 8 次全覆盖时挑最久未用（话题续接）",
          m.pick_flavor(st56b, "impulse") == "话题续接",
          m.pick_flavor(st56b, "impulse"))
    st56c = {"flavor_history": ["日常分享", "近况问候"]}
    _rnd.seed(7)
    picks56c = {m.pick_flavor(st56c, "impulse") for _ in range(40)}
    check("56b-2 窗内口味不被选中、窗外全可达",
          picks56c == set(m.DEFAULT_FLAVORS) - {"日常分享", "近况问候"}, str(picks56c))
    st56d = {"flavor_history": ["日常分享"] * 10}
    _rnd.seed(11)
    picks56d = {m.pick_flavor(st56d, "impulse") for _ in range(40)}
    check("56b-3 历史超 8 条只看近 8（第 9 条前的口味可再选）",
          picks56d == set(m.DEFAULT_FLAVORS) - {"日常分享"}, str(picks56d))

    # 56c. 口味池扩为 7 种（契约同步：新增「小见闻分享」「关心提醒」）：
    # 旧五味在窗内时新两味可被选中且互不独占；七味全在窗内时兜底取最久未用
    st56e = {"flavor_history": ["日常分享", "近况问候", "轻松玩笑",
                                "话题续接", "回忆细节"]}
    _rnd.seed(23)
    picks56e = {m.pick_flavor(st56e, "impulse") for _ in range(60)}
    check("56c-1 新两味在旧五味被窗排除时可达",
          picks56e == {"小见闻分享", "关心提醒"}, str(picks56e))
    st56f = {"flavor_history": ["关心提醒", "日常分享", "近况问候",
                                "轻松玩笑", "话题续接", "回忆细节",
                                "小见闻分享"]}
    check("56c-2 七味全在窗内兜底挑最久未用（关心提醒）",
          m.pick_flavor(st56f, "impulse") == "关心提醒",
          m.pick_flavor(st56f, "impulse"))

    # 42. sent_count 展示口径：按台账重算时剔除已作废条目
    #     （mark_sent 先计数、作废没回减曾让 status 显示 5 条而真实只有 3 条）
    st42 = {"date": today, "sent_count": 5,
            "log": [{"ts": t0830 - 3600, "intent": "impulse", "replied": True},
                    {"ts": t0830 - 1800, "intent": "impulse", "replied": None,
                     "voided": True},
                    {"ts": t0830 - 900, "intent": "followup", "replied": None}]}
    check("42 sent_count 按台账重算剔除作废",
          m.logged_sent_count(st42) == 2,
          f"count={m.logged_sent_count(st42)}")

    # 36. roll 自愈：命中 roll 已 30 分钟、无今日发送记录 → 删 roll 重 roll 命中
    home, m = make_home(t0830, extra_state={
        "date": "2026-10-21",
        "slot_rolls": {"morning": {"date": "2026-10-21", "hit": True,
                                   "rolled_at": t0830 - 1800}}})
    d = m.decide(now=t0830)
    check("36 命中 roll 被空耗 → 自愈重 roll", d.get("due") and d.get("intent") == "morning", str(d))

    # 37. roll 自愈反例：命中 roll 已有今日发送记录 → 不重发
    home, m = make_home(t0830, extra_state={
        "date": "2026-10-21",
        "slot_rolls": {"morning": {"date": "2026-10-21", "hit": True,
                                   "rolled_at": t0830 - 1800}},
        "log": [{"ts": t0830 - 900, "intent": "morning", "replied": True}]})
    d = m.decide(now=t0830)
    check("37 命中 roll 已有发送记录 → 不重 roll", not d.get("due"), str(d))

    # 38. roll 自愈反例：命中 roll 才 10 分钟（发送可能还在路上）→ 不重 roll
    home, m = make_home(t0830, extra_state={
        "date": "2026-10-21",
        "slot_rolls": {"morning": {"date": "2026-10-21", "hit": True,
                                   "rolled_at": t0830 - 600}}})
    d = m.decide(now=t0830)
    check("38 命中 roll 未超时 → 不重 roll", not d.get("due"), str(d))

    # 39. impulse 抖动收窄：随手分享不套 0-25 分钟大抖动（窗口太长会被
    #     他先发来的新对话作废，2026-10-22 实测当天 impulse 因此作废），
    #     send_after 距决策时刻至多 3 分钟
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=2,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 2})
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("39 impulse 抖动 ≤3 分钟",
          d.get("due") and d.get("intent") == "impulse"
          and 0 <= float(d.get("send_after", 0)) - ts(2026, 10, 21, 15, 0) <= 180,
          f"d={d.get('intent')} delay={float(d.get('send_after', 0)) - ts(2026, 10, 21, 15, 0):.0f}s")

    # 40. 时段档抖动收窄：即使配置残留旧的 0-25 分钟，代码也封顶 10 分钟
    home, m = make_home(t0830, rhythm={"jitter_min": [0, 25]})
    d = m.decide(now=t0830)
    check("40 时段 morning 抖动 ≤10 分钟",
          d.get("due") and d.get("intent") == "morning"
          and 0 <= float(d.get("send_after", 0)) - t0830 <= 600,
          f"d={d.get('intent')} delay={float(d.get('send_after', 0)) - t0830:.0f}s")

    # 41. 静默档抖动同样封顶 10 分钟
    home, m = make_home(t1500, slots={}, ctx_age_h=6,
                        rhythm={"jitter_min": [0, 25]})
    m.SILENCE_P = 1.0
    d = m.decide(now=t1500)
    check("41 静默 silence 抖动 ≤10 分钟",
          d.get("due") and d.get("intent") == "silence"
          and 0 <= float(d.get("send_after", 0)) - t1500 <= 600,
          f"d={d.get('intent')} delay={float(d.get('send_after', 0)) - t1500:.0f}s")

    # 43. 事件签名记账：sent --event 落 log；followup 的 --about 与 --event 并存；
    #     不带 --event 的旧调用照常工作、新条目不带 event 键
    home, m = make_home(t0830)
    rc1 = m.main(["sent", "--intent", "impulse", "--event", "小猫开柜偷零食"])
    rc2 = m.main(["sent", "--intent", "followup", "--about", "他说要下班回家",
                  "--event", "下班到家关心"])
    rc3 = m.main(["sent", "--intent", "morning"])
    st43 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    lg43 = st43["log"]
    check("43 sent --event 落盘（followup about/event 并存、旧调用兼容）",
          rc1 == 0 and rc2 == 0 and rc3 == 0 and len(lg43) == 3
          and lg43[0].get("event") == "小猫开柜偷零食"
          and lg43[1].get("about") == "他说要下班回家"
          and lg43[1].get("event") == "下班到家关心"
          and "event" not in lg43[2],
          f"rc={rc1}/{rc2}/{rc3} log={lg43}")
    # 44. tick brief 回喂今日已用事件（含作废条目的 event、followup 的 about）
    home, m = make_home(t0830, extra_state={
        "date": "2026-10-21",
        "log": [{"ts": t0830 - 3600, "intent": "impulse", "replied": True,
                 "event": "小猫开柜偷零食"},
                {"ts": t0830 - 1800, "intent": "impulse", "replied": None,
                 "voided": True, "event": "改稿赶工"},
                {"ts": t0830 - 900, "intent": "followup", "replied": True,
                 "about": "他的面试"}]})
    d = m.decide(now=t0830)
    used44 = (d.get("brief") or {}).get("used_events_today")
    check("44 brief 带 used_events_today（含作废）",
          d.get("due") and used44 == ["小猫开柜偷零食", "改稿赶工", "他的面试"],
          f"used={used44} reason={d.get('reason')}")
    # 44b. used_events_today 本身：同签名去重、非今日的不算
    st44 = {"log": [{"ts": t0830 - 100, "intent": "impulse", "replied": True,
                     "event": "同梗"},
                    {"ts": t0830 - 200, "intent": "impulse", "replied": True,
                     "event": "同梗"},
                    {"ts": ts(2026, 10, 19, 20, 0), "intent": "impulse",
                     "replied": True, "event": "前天的梗"}]}
    check("44b used_events_today 去重且只取今日",
          m.used_events_today(st44, "2026-10-21") == ["同梗"],
          f"used={m.used_events_today(st44, '2026-10-21')}")

    # 45. 时段尾段豁免：唯一挡路的是随机间隔，且 next_allowed_at 落在窗口
    #     结束（09:30）之后——等它走完这句早安注定错过 → 豁免、due
    #     （复刻 2026-10-22 晚 evening 被 followup 记账推过窗口的事故）
    home, m = make_home(t0830, extra_state={
        "last_sent_at": t0830 - 2 * 3600,
        "next_allowed_at": ts(2026, 10, 21, 9, 45)})
    d = m.decide(now=t0830)
    check("45 时段尾段豁免随机间隔 → due morning",
          d.get("due") and d.get("intent") == "morning", str(d)[:100])
    # 46. 反例：间隔在窗口内就能走完（09:00 < 窗口结束 09:30）→ 不豁免、
    #     不 due，且不消耗今天的 roll（间隔走完后下个 tick 还能正常 roll）
    home, m = make_home(t0830, extra_state={
        "last_sent_at": t0830 - 2 * 3600,
        "next_allowed_at": ts(2026, 10, 21, 9, 0)})
    d = m.decide(now=t0830)
    st46 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    check("46 间隔窗口内可走完 → 不豁免且不消耗 roll",
          not d.get("due") and "还没到" in d.get("reason", "")
          and "morning" not in (st46.get("slot_rolls") or {}),
          f"d={str(d)[:80]} rolls={st46.get('slot_rolls')}")
    # 47. impulse 不享受豁免：同样的间隔挡路、无时段窗口 → 照旧不发
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=2,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 2},
                        extra_state={"last_sent_at": ts(2026, 10, 21, 13, 0),
                                     "next_allowed_at": ts(2026, 10, 21, 18, 0)})
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("47 impulse 被随机间隔挡住不豁免",
          not d.get("due") and "还没到" in d.get("reason", ""), str(d)[:80])

    # ---- P1/P2（2026-10-23 复盘乙类提案，用户批准落地） ----
    # 57. P1 用例 (a)：他 50 分钟前刚回过上一条主动（旧「主动话题刚
    # 聊完」1 小时全局封锁的窗口正中：上一条主动在 4000 秒前发出、
    # 他 3000 秒前回复），impulse 条件齐备 → 不再被该门控拦（改前
    # 这里直接返回「主动话题刚聊完」）。随机间隔已走完
    # （next_allowed_at 在过去），不构成第二个变量。
    home, m = make_home(t1500, slots={}, ctx_age_h=50 / 60,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 8},
                        extra_state={"last_sent_at": t1500 - 4000,
                                     "next_allowed_at": t1500 - 60})
    d = m.decide(now=t1500)
    check("57 P1 impulse 不再被「刚聊完」1 小时门控拦",
          d.get("due") and d.get("intent") == "impulse", str(d)[:100])
    # 58. P1 用例 (b)：同样的刚聊完窗口内，时段问候仍被挡（行为
    # 不变）且不消耗今日 roll。他最新消息在 3000 秒前，已越过 45
    # 分钟避让的 2700 秒——挡住的只能是 1 小时门控本身。对照组去掉
    # last_sent_at（他没回过任何主动）→ 同一时刻早安照常 due，
    # 隔离出这道门控的作用。
    home, m = make_home(t0830, ctx_age_h=50 / 60,
                        extra_state={"last_sent_at": t0830 - 4000,
                                     "next_allowed_at": t0830 - 60})
    d = m.decide(now=t0830)
    st58 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    home, m = make_home(t0830, ctx_age_h=50 / 60)
    d_ctl = m.decide(now=t0830)
    check("58 P1 时段问候在刚聊完 1 小时内仍被挡且不消耗 roll",
          not d.get("due") and "morning" not in (st58.get("slot_rolls") or {})
          and d_ctl.get("due") and d_ctl.get("intent") == "morning",
          f"d={str(d)[:80]} rolls={st58.get('slot_rolls')} ctl={d_ctl.get('intent')}")
    # 59. 用例 (c) 边界：「15 分钟内正在聊」是防打断正在进行对话的
    # 另一条门控（看 last_activity，含她的回复），与 P1 收窄的 1 小时
    # 话题门控不是一回事——他 10 分钟前刚发过消息，impulse 照样被
    # 它挡住（active_skip_min 按生产值 15 验）。
    home, m = make_home(t1500, slots={}, ctx_age_h=10 / 60,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 8})
    _cfgp = os.path.join(home, "config.json")
    _cfg = json.load(open(_cfgp))
    _cfg["proactive"]["active_skip_min"] = 15
    json.dump(_cfg, open(_cfgp, "w"), ensure_ascii=False)
    d = m.decide(now=t1500)
    check("59 15 分钟内正在聊 → impulse 仍被抑制（与 P1 门控是两条）",
          not d.get("due") and "刚刚还在聊" in d.get("reason", ""), str(d)[:80])
    # 60. P2 用例 (d)：silence 首次 roll 未中 → +30 分钟不给第二次、
    # 静默再持续满 1 小时给第二次（同概率）且命中即 due。
    # random 用队列桩定序（silence 与 impulse 的判定都会消耗随机数，
    # 逐项算好）：0.9=silence 首 roll 未中（≥0.85）、0.99=impulse 判
    # 定消耗（p=0 恒不中）、0.99=第二次 tick 的 impulse 消耗、
    # 0.1=silence 第二次 roll 命中。
    def _seq_random(vals):
        it = iter(vals)
        return _types.SimpleNamespace(
            random=lambda: next(it, 0.99), randint=lambda a, b: a,
            uniform=lambda a, b: a, choice=lambda s: s[0])

    home, m = make_home(t1500, slots={}, ctx_age_h=6)
    m.random = _seq_random([0.9, 0.99, 0.99, 0.1])
    d1 = m.decide(now=t1500)
    st60 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    r1 = (st60.get("slot_rolls") or {}).get("silence") or {}
    d2 = m.decide(now=t1500 + 1800)
    st60 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    r2 = (st60.get("slot_rolls") or {}).get("silence") or {}
    d3 = m.decide(now=t1500 + 3700)
    st60 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    r3 = (st60.get("slot_rolls") or {}).get("silence") or {}
    check("60 P2 silence 第二次机会（+30min 不给 / +1h 给且命中）",
          not d1.get("due") and r1.get("hit") is False
          and r1.get("attempts") == 1
          and not d2.get("due") and r2.get("attempts") == 1
          and d3.get("due") and d3.get("intent") == "silence"
          and r3.get("hit") is True and r3.get("attempts") == 2,
          f"d1={d1.get('reason')} r1={r1} r2={r2} "
          f"d3={d3.get('intent')} r3={r3}")
    # 60b. 两次都未中 → 当日绝不第三次（随机数就算会命中也不 roll）
    home, m = make_home(t1500, slots={}, ctx_age_h=6)
    m.random = _seq_random([0.9, 0.99, 0.95, 0.99, 0.1])
    e1 = m.decide(now=t1500)
    e2 = m.decide(now=t1500 + 3700)
    e3 = m.decide(now=t1500 + 7400)
    st60 = json.load(open(os.path.join(home, "state", "proactive.json"),
                          encoding="utf-8"))
    r60 = (st60.get("slot_rolls") or {}).get("silence") or {}
    check("60b P2 silence 每日至多 roll 两次（无第三次）",
          not e1.get("due") and not e2.get("due") and not e3.get("due")
          and r60.get("attempts") == 2 and r60.get("hit") is False,
          f"r={r60} e3={e3.get('reason')}")
    # 60c. 自愈对第二次同样适用：第二次 roll 命中但发送链路空耗
    # （超 20 分钟无 silence 发送记录）→ 删 roll 重 roll、可再次命中
    home, m = make_home(t1500, slots={}, ctx_age_h=6,
                        extra_state={"date": "2026-10-21",
                                     "slot_rolls": {"silence": {
                                         "date": "2026-10-21", "hit": True,
                                         "rolled_at": t1500 - 1800,
                                         "attempts": 2}}})
    m.SILENCE_P = 1.0
    d = m.decide(now=t1500)
    check("60c P2 第二次命中 roll 被空耗 → 自愈重 roll",
          d.get("due") and d.get("intent") == "silence", str(d)[:100])
    # 61. 用例 (e) 新口径（2026-10-27）：未回复并存与未回复计数都不再
    # 是全域门控——impulse 在两类场景下照常触发；静默时段照旧生效。
    # 61a 未回复并存 3 条（三条都发在他最新消息之后，不会被结算掉）
    home, m = make_home(t1500, slots={}, ctx_age_h=50 / 60,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 8},
                        extra_state={
                            "last_sent_at": t1500 - 4000,
                            "next_allowed_at": t1500 - 60,
                            "log": [{"ts": t1500 - 2000, "intent": "impulse",
                                     "replied": None},
                                    {"ts": t1500 - 1500, "intent": "impulse",
                                     "replied": None},
                                    {"ts": t1500 - 1000, "intent": "impulse",
                                     "replied": None}]})
    _cfgp = os.path.join(home, "config.json")
    _cfg = json.load(open(_cfgp))
    _cfg["proactive"]["daily_max"] = 10
    json.dump(_cfg, open(_cfgp, "w"), ensure_ascii=False)
    d = m.decide(now=t1500)
    ok61a = d.get("due") and d.get("intent") == "impulse"
    # 61b 未回复计数给 8：counters 自此不被决策层读取，照常触发
    home, m = make_home(t1500, slots={}, ctx_age_h=50 / 60,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 8},
                        extra_state={"last_sent_at": t1500 - 4000,
                                     "next_allowed_at": t1500 - 60})
    with open(os.path.join(home, "state", "counters.json"), "w",
              encoding="utf-8") as f:
        json.dump({"unreplied": 8}, f)
    d = m.decide(now=t1500)
    ok61b = d.get("due") and d.get("intent") == "impulse"
    # 61c 静默时段对 silence 照旧生效
    home, m = make_home(ts(2026, 10, 21, 3, 0), slots={}, ctx_age_h=6)
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 3, 0))
    ok61c = not d.get("due") and "静默时段" in d.get("reason", "")
    check("61 未回复不再是全域门控（并存/计数不停发、静默照旧）",
          bool(ok61a and ok61b and ok61c),
          f"unresolved={ok61a} stop={ok61b} quiet={ok61c}")

    # 62. write_json 并发：两进程同写一文件不许再共用固定 ".tmp" 互相
    # 抢走临时文件（修复前实证 FileNotFoundError 崩溃，bughunt 面 1）
    from multiprocessing import get_context

    home62, _m62 = make_home(t1500, slots={})
    target62 = os.path.join(home62, "state", "race.json")
    # fork 仅 POSIX 可用；Windows 用 spawn（_race_child62 已提为模块级函数）
    ctx62 = get_context("fork" if os.name != "nt" else "spawn")
    ps62 = [ctx62.Process(target=_race_child62,
                          args=(os.path.join(home62, "proactive.py"), target62, t, 150))
            for t in ("A", "B")]
    [p.start() for p in ps62]
    [p.join() for p in ps62]
    try:
        d62 = json.load(open(target62, encoding="utf-8"))
        ok62 = len(d62.get("pad", "")) == 20000
    except Exception:
        ok62 = False
    check("62 write_json 并发同写不互抢 tmp、产物完整",
          all(p.exitcode == 0 for p in ps62) and ok62,
          f"exits={[p.exitcode for p in ps62]} ok={ok62}")

    # 63. 视频无条数上限（2026-10-24 用户定档「基本不限」，取代当日
    # 稍早的日限 2 条）：同日计数 1 条、5 条都照常可 roll 成视频，
    # video_sends_today 只统计不拦截
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        extra_state={"date": "2026-10-21",
                                     "video_sends_today": 1},
                        rhythm={"photo_p": 0.0, "video_p": 1.0, "double_p": 0.0})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        extra_state={"date": "2026-10-21",
                                     "video_sends_today": 5},
                        rhythm={"photo_p": 0.0, "video_p": 1.0, "double_p": 0.0})
    m.SILENCE_P = 1.0
    d2 = m.decide(now=ts(2026, 10, 21, 15, 0))
    check("63 视频无条数上限（计数 1/5 均可连续 roll）",
          d.get("form") == "video" and d2.get("form") == "video",
          f"cnt1={d.get('form')} cnt5={d2.get('form')}")
    # 64. 视频计数跨日归零 + mark_sent 落盘 +1：昨日计数今日归零后照常
    # 可 roll；pending_form=video 的 sent 把当日计数从 1 推到 2 并落
    # last_video_date（计数只作统计、无上限，见 63）
    home, m = make_home(ts(2026, 10, 22, 15, 0), slots={}, ctx_age_h=6,
                        extra_state={"date": "2026-10-21",
                                     "video_sends_today": 2},
                        rhythm={"photo_p": 0.0, "video_p": 1.0, "double_p": 0.0})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 22, 15, 0))
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6)
    real_today = datetime.now(TZ).date().isoformat()
    stx = m.load_state()
    stx["date"] = real_today
    stx["video_sends_today"] = 1
    stx["pending_form"] = "video"
    m.save_state(stx)
    sty = m.mark_sent("evening")
    check("64 视频计数跨日归零 + sent 计数 +1",
          d.get("form") == "video"
          and sty.get("video_sends_today") == 2
          and sty.get("last_video_date") == real_today,
          f"nextday={d.get('form')} cnt={sty.get('video_sends_today')} "
          f"lvd={sty.get('last_video_date')}")

    # 65. S2 datetime 守卫：daily_cap 的 started_on 混入 naive 时间串 /
    # 带偏移时间串 / 垃圾值都不崩，且统一按 Asia/Shanghai 日期口径判；
    # 日期串与 naive 串与旧行为一致，垃圾值走 days=99 兜底
    home, m = make_home(t0830)
    pcfg65 = {"daily_max": 18}
    cap_date = m.daily_cap({"started_on": "2026-10-20"}, pcfg65, "2026-10-21")
    cap_naive = m.daily_cap({"started_on": "2026-10-20T23:30:00"},
                            pcfg65, "2026-10-21")
    cap_aware = m.daily_cap({"started_on": "2026-10-19T16:00:00+00:00"},
                            pcfg65, "2026-10-21")
    cap_junk = m.daily_cap({"started_on": "not-a-date"}, pcfg65, "2026-10-21")
    cap_none = m.daily_cap({}, pcfg65, "2026-10-21")
    check("65 daily_cap datetime 守卫（naive/aware/垃圾不崩、口径统一）",
          cap_date == 4 and cap_naive == 4 and cap_aware == 4
          and cap_junk == 18 and cap_none == 4
          and m._day_of("2026-10-21").isoformat() == "2026-10-21",
          f"date={cap_date} naive={cap_naive} aware={cap_aware} "
          f"junk={cap_junk} none={cap_none}")

    # 66. 照片主体加权混合（2026-10-24 用户指示：主要拍她自己）：
    # photo_self_p=1 → 本人出镜；=0 → 回落日期奇偶轮换（偶数日生活场景）
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"photo_p": 1.0, "photo_self_p": 1.0,
                                "double_p": 0.0})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(ts(2026, 10, 22, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"photo_p": 1.0, "photo_self_p": 0.0,
                                "double_p": 0.0})
    m.SILENCE_P = 1.0
    d2 = m.decide(now=ts(2026, 10, 22, 15, 0))
    check("66 照片主体加权（本人出镜命中/未命中回落生活场景）",
          d.get("form") == "photo" and d.get("photo_subject") == "本人出镜"
          and d2.get("form") == "photo" and d2.get("photo_subject") == "生活场景",
          f"self={d.get('photo_subject')} legacy={d2.get('photo_subject')}")
    # 67. 视频主体加权混合（2026-10-24 用户指示：主要以她自己录的为主）：
    # video_self_p=1 → 本人出镜；=0 → 回落日期奇偶轮换（偶数日生活场景）
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"photo_p": 0.0, "video_p": 1.0,
                                "video_self_p": 1.0, "double_p": 0.0})
    m.SILENCE_P = 1.0
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(ts(2026, 10, 22, 15, 0), slots={}, ctx_age_h=6,
                        rhythm={"photo_p": 0.0, "video_p": 1.0,
                                "video_self_p": 0.0, "double_p": 0.0})
    m.SILENCE_P = 1.0
    d2 = m.decide(now=ts(2026, 10, 22, 15, 0))
    check("67 视频主体加权（本人出镜命中/未命中回落生活场景）",
          d.get("form") == "video" and d.get("video_subject") == "本人出镜"
          and d2.get("form") == "video" and d2.get("video_subject") == "生活场景",
          f"self={d.get('video_subject')} legacy={d2.get('video_subject')}")

    # 68. 形式裁定·双路可达（2026-10-24 定档「基本不限」）：evening
    # 同时在照片/视频可达集，photo_p=video_p=1.0 时按 50/50 裁定——
    # 连跑 20 次两路都出现且永不回落文字
    forms68 = set()
    for _ in range(20):
        home, m = make_home(ts(2026, 10, 21, 19, 0),
                            slots={"evening": {"p": 1.0,
                                               "window": ["18:00", "20:00"]}},
                            ctx_age_h=2,
                            rhythm={"photo_p": 1.0, "video_p": 1.0,
                                    "photo_self_p": 0.0, "video_self_p": 0.0,
                                    "double_p": 0.0})
        d = m.decide(now=ts(2026, 10, 21, 19, 0))
        if d.get("intent") == "evening":
            forms68.add(d.get("form"))
    check("68 形式裁定双路可达（evening 50/50、必出媒体）",
          forms68 == {"photo", "video"}, f"forms={sorted(forms68)}")
    # 69. 形式裁定·单路可达：impulse 只在视频集 → 两权重拉满也必视频；
    # morning 只在照片集 → 必照片
    home, m = make_home(ts(2026, 10, 21, 15, 0), slots={}, ctx_age_h=2,
                        rhythm={"impulse_p": 1.0, "impulse_daily_max": 8,
                                "photo_p": 1.0, "video_p": 1.0,
                                "double_p": 0.0})
    d = m.decide(now=ts(2026, 10, 21, 15, 0))
    home, m = make_home(t0830,
                        rhythm={"photo_p": 1.0, "video_p": 1.0,
                                "photo_self_p": 0.0, "double_p": 0.0})
    d2 = m.decide(now=t0830)
    check("69 形式裁定单路可达（impulse 必视频/morning 必照片）",
          d.get("intent") == "impulse" and d.get("form") == "video"
          and d2.get("intent") == "morning" and d2.get("form") == "photo",
          f"impulse={d.get('intent')}/{d.get('form')} "
          f"morning={d2.get('intent')}/{d2.get('form')}")
    # 70. 形式裁定·不在任一可达集：lunch 两权重拉满仍保持文字
    home, m = make_home(ts(2026, 10, 21, 12, 0),
                        slots={"lunch": {"p": 1.0,
                                         "window": ["11:45", "12:40"]}},
                        ctx_age_h=2,
                        rhythm={"photo_p": 1.0, "video_p": 1.0,
                                "double_p": 0.0})
    d = m.decide(now=ts(2026, 10, 21, 12, 0))
    check("70 形式裁定不可达集（lunch 必文字）",
          d.get("intent") == "lunch" and d.get("form") == "text",
          f"lunch={d.get('intent')}/{d.get('form')}")
    # 71. roll 结果 durable 落盘（二轮深审 §5）：命中与未命中各一行，
    # 字段含 slot/hit/p；文件不存在于未 roll 的家目录（不误写）。
    home, m = make_home(t0830)
    d = m.decide(now=t0830)
    _rp = os.path.join(home, "state", "proactive_rolls.jsonl")
    _rolls = [json.loads(x) for x in open(_rp, encoding="utf-8")] \
        if os.path.exists(_rp) else []
    check("71a slot roll 命中落盘一行（morning/hit/p）",
          d.get("due") and len(_rolls) == 1
          and _rolls[0].get("slot") == "morning"
          and _rolls[0].get("hit") is True
          and _rolls[0].get("p") == 1.0,
          f"rolls={_rolls}")
    home, m = make_home(t0830, slots={"morning": {"p": 0.0,
                                                  "window": ["07:30", "09:30"]}})
    d = m.decide(now=t0830)
    _rp = os.path.join(home, "state", "proactive_rolls.jsonl")
    _rolls = [json.loads(x) for x in open(_rp, encoding="utf-8")] \
        if os.path.exists(_rp) else []
    check("71b slot roll 未命中同样落盘（分母完整）",
          not d.get("due") and len(_rolls) == 1
          and _rolls[0].get("slot") == "morning"
          and _rolls[0].get("hit") is False,
          f"rolls={_rolls} d={str(d)[:60]}")

    # 72. E2 日期对齐：昨夜发送的条目次日被作废——回滚只认条目 ts 的
    # 归属日，今日 sent_count 缓存分毫不动；作废落 voided_at 双时间戳；
    # derive_day_counts 按归属日派生、剔除 voided 后归属日归零。
    _now = time.time()
    _today_r = datetime.now(TZ).date().isoformat()
    _yday_r = datetime.fromtimestamp(_now - 90000, TZ).date().isoformat()
    _mid72 = int(_now - 90000)
    home, m = make_home(_now, extra_state={
        "date": _today_r, "sent_count": 2,
        "log": [{"ts": _now - 90000, "intent": "impulse", "replied": True,
                 "message_id": f"pro-{_mid72}"}]})
    fdir = os.path.join(home, "outbox", "failed")
    os.makedirs(fdir)
    with open(os.path.join(fdir, f"pro-{_mid72}-1.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": f"pro-{_mid72}", "error": "已有新对话"}, f)
    st = m.load_state()
    ch72 = m.reconcile_voided(st)
    e72 = st["log"][0]
    dc72 = m.derive_day_counts(st)
    check("72 E2 跨午夜作废（归属日回滚/今日数不动/voided_at 落盘）",
          ch72 and e72.get("voided") and float(e72.get("voided_at", 0)) > 0
          and st["sent_count"] == 2
          and dc72.get(_yday_r) == 0 and dc72.get(_today_r) == 0,
          f"ch={ch72} e={e72} sent_count={st.get('sent_count')} dc={dc72}")

    # 73. E3 form 埋点：intended 取 pending_form 裁定值，delivered 按
    # outbox 实际条目推；视频裁定只达文字 → media_fail；如裁定送达与
    # 无组可核（记账先行）均不记 degrade_reason。
    def _mk73(pending, sent_items):
        mid = int(time.time()) - 5000
        home, m = make_home(time.time(), extra_state={
            "pending_form": pending})
        sdir = os.path.join(home, "outbox", "sent")
        os.makedirs(sdir, exist_ok=True)
        for i, it in enumerate(sent_items, 1):
            with open(os.path.join(sdir, f"pro-{mid}-{i}.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"message_id": f"pro-{mid}", "kind": "proactive",
                           **it}, f)
        st = m.mark_sent("impulse", message_id=f"pro-{mid}")
        return st["log"][-1]

    e73a = _mk73("video", [{"text": "想你了"}])
    e73b = _mk73("video", [{"text": "录好啦"},
                           {"text": "[视频]", "media_type": "video",
                            "media_file": "/tmp/x.mp4"}])
    home, m = make_home(time.time())
    st73c = m.mark_sent("morning")
    e73c = st73c["log"][-1]
    check("73 E3 形态埋点（降级分因/如裁定/无组不虚构）",
          e73a.get("form_intended") == "video"
          and e73a.get("form_delivered") == "text"
          and e73a.get("degrade_reason") == "media_fail"
          and e73b.get("form_intended") == "video"
          and e73b.get("form_delivered") == "video"
          and "degrade_reason" not in e73b
          and e73c.get("form_intended") == "text"
          and e73c.get("form_delivered") == "text"
          and "degrade_reason" not in e73c,
          f"a={e73a} b={e73b} c={e73c}")

    # ---- 90 系：有机模式（organic） ----
    t0700 = ts(2026, 10, 21, 7, 0)  # 周三
    # 90. 有机计划确定性：同日两次生成的锚点/事件/心情完全一致
    home, m = make_home(t0700, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0})
    plan_a = m._life.ensure_today(t0700)
    plan_b = m._life.generate_today(t0700)
    check("90 有机计划同日确定性",
          plan_a == plan_b and plan_a.get("date") == "2026-10-21"
          and len(plan_a.get("moments") or []) == 4
          and 1 <= len(plan_a.get("events") or []) <= 3,
          str(plan_a)[:160])
    # 91. 锚点到点 → due，intent 与锚点一致，brief 带 trigger/life_state
    home, m = make_home(t0700, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0})
    plan = m._life.ensure_today(t0700)
    live = [x for x in plan["moments"] if not x["skip"]]
    if live:
        mm = live[0]
        now91 = float(mm["ts"]) + 120
        with open(os.path.join(home, "state", "context.json"), "w", encoding="utf-8") as f:
            json.dump({"context_token": "tok-test",
                       "received_at": now91 - 2 * 3600, "stale": False}, f)
        d = m.decide(now=now91)
        check("91 有机锚点到点触发",
              d.get("due") and d.get("intent") == mm["intent"]
              and (d.get("brief") or {}).get("trigger", {}).get("type") == "plan"
              and (d.get("brief") or {}).get("life_state", {}).get("mood")
              == plan["mood"],
              str(d)[:200])
        d2 = m.decide(now=now91 + 60)
        check("91b 同一锚点不重复触发", not d2.get("due")
              or d2.get("intent") != mm["intent"], str(d2)[:120])
    else:
        check("91 有机锚点到点触发", False, "当日锚点全跳过，样本异常")
    # 92. 全部锚点跳过 + 无事件到期 → 不发（跳过是当日采样的一部分）
    home, m = make_home(t0700, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0, "wakeup_skip_p": 1.0,
                                 "lunch_skip_p": 1.0, "afterwork_skip_p": 1.0,
                                 "bedtime_skip_p": 1.0})
    plan92 = m._life.ensure_today(t0700)
    wake92 = [x for x in plan92["moments"] if x["kind"] == "wakeup"][0]
    now92 = float(wake92["ts"]) + 120
    with open(os.path.join(home, "state", "context.json"), "w", encoding="utf-8") as f:
        json.dump({"context_token": "tok-test",
                   "received_at": now92 - 2 * 3600, "stale": False}, f)
    d = m.decide(now=now92)
    check("92 被跳过的锚点不触发", not d.get("due"), str(d)[:120])
    # 93. 生活事件到分享时刻 → impulse，about 为事件文本，trigger 标缘由
    home, m = make_home(t0700, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0})
    plan93 = m._life.ensure_today(t0700)
    ev = plan93["events"][0]
    now93 = float(ev["share_at"]) + 60
    st93 = json.load(open(os.path.join(home, "state", "proactive.json")))
    st93["organic"] = {"date": "2026-10-21",
                       "fired": [x["id"] for x in plan93["moments"]],
                       "silence_date": None}
    json.dump(st93, open(os.path.join(home, "state", "proactive.json"), "w"),
              ensure_ascii=False)
    with open(os.path.join(home, "state", "context.json"), "w", encoding="utf-8") as f:
        json.dump({"context_token": "tok-test",
                   "received_at": now93 - 2 * 3600, "stale": False}, f)
    d = m.decide(now=now93)
    check("93 有机生活事件分享",
          d.get("due") and d.get("intent") == "impulse"
          and d.get("about") == ev["text"]
          and (d.get("brief") or {}).get("trigger", {}).get("type") == "life_event",
          str(d)[:200])
    # 94. 有机模式安全闸：静默时段 / context 太旧仍在；未回不再熔断
    home, m = make_home(ts(2026, 10, 21, 3, 0), mode="organic",
                        persona=ORGAN_PERSONA, organic={"spontaneous_p": 0.0})
    check("94a 有机模式凌晨静默硬闸（rare 默认关）",
          not m.decide(now=ts(2026, 10, 21, 3, 0)).get("due"))
    home, m = make_home(t0830, mode="organic", persona=ORGAN_PERSONA,
                        ctx_age_h=30, organic={"spontaneous_p": 0.0})
    check("94b 有机模式 context 太旧不发",
          not m.decide(now=t0830).get("due"))
    home, m = make_home(t0830, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0})
    with open(os.path.join(home, "state", "counters.json"), "w", encoding="utf-8") as f:
        json.dump({"unreplied": 5}, f)
    d = m.decide(now=t0830)
    check("94c 有机模式未回复计数不再熔断（counters 不被决策层读取）",
          "没回" not in d.get("reason", "") and "熔断" not in d.get("reason", ""),
          str(d)[:80])
    # 95. 有机间隔采样：mark_sent 后 next_allowed_at 落在采样区间内，
    #     且间隔未到时 decide 不发
    home, m = make_home(time.time(), mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0})
    st95 = m.mark_sent("impulse")
    gap95 = (float(st95["next_allowed_at"]) - float(st95["last_sent_at"])) / 3600
    check("95 有机间隔在采样区间 [0.3, 5] 小时",
          0.29 <= gap95 <= 5.01, f"gap={gap95:.2f}h")
    # 96. 静默稀有例外：显式开启 + p=1 时凌晨可发一条（仅文字），
    #     同周第二次被周上限挡住
    t0300 = ts(2026, 10, 21, 3, 0)
    home, m = make_home(t0300, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0,
                                 "quiet_rare": {"enabled": True, "p": 1.0,
                                                "max_per_week": 1}})
    d = m.decide(now=t0300)
    check("96a quiet_rare 开启后凌晨可发",
          d.get("due") and d.get("intent") == "impulse"
          and d.get("form") == "text"
          and (d.get("brief") or {}).get("trigger", {}).get("type") == "quiet_rare",
          str(d)[:160])
    d = m.decide(now=t0300 + 1800)
    check("96b quiet_rare 周上限 1 次后不再放行", not d.get("due"), str(d)[:120])
    # 97. 缺省 mode 仍是 fixed（兼容）：mode 函数判定 + 时段行为不变
    home, m = make_home(t0830)
    pcfg97 = json.load(open(os.path.join(home, "config.json")))["proactive"]
    check("97 缺省 fixed 模式判定", m.proactive_mode(pcfg97) == "fixed")

    # ---- 2026-10-27 撤上限 + 未回复情绪模型 ----
    # 98. daily_max=null → 无上限：24h 内已发 20 条（全已回复）也不拦
    home, m = make_home(t0830, extra_state={
        "log": [{"ts": t0830 - (i + 1) * 900, "intent": "morning",
                 "replied": True} for i in range(20)]})
    _cfgp = os.path.join(home, "config.json")
    _cfg = json.load(open(_cfgp))
    _cfg["proactive"]["daily_max"] = None
    json.dump(_cfg, open(_cfgp, "w"), ensure_ascii=False)
    cap98 = m.effective_cap(m.load_state(), m.cfg()["proactive"], "2026-10-21")
    d = m.decide(now=t0830)
    check("98 daily_max=null 时无上限拦截（24h 20 条照发）",
          cap98 is None and d.get("due") and d.get("intent") == "morning",
          f"cap={cap98} d={str(d)[:80]}")
    # 99. mood streak 累积与「他一回复清零」（mood_state 直测）
    home, m = make_home(t0830)
    st99 = m.load_state()
    st99["log"] = [{"ts": t0830 - 30 * 3600, "intent": "morning", "replied": False},
                   {"ts": t0830 - 26 * 3600, "intent": "lunch", "replied": False},
                   {"ts": t0830 - 13 * 3600, "intent": "evening", "replied": False}]
    mo99 = m.mood_state(st99, "2026-10-21")
    ok99a = (mo99["streak"] == 3 and 0.60 < mo99["factor"] < 0.63
             and bool(mo99["label"]))
    st99["log"].append({"ts": t0830 - 3600, "intent": "impulse", "replied": True})
    mo99b = m.mood_state(st99, "2026-10-21")
    check("99 streak 累积 3 条、他一回复清零",
          ok99a and mo99b["streak"] == 0 and mo99b["factor"] == 1.0,
          f"a={mo99} b={mo99b}")
    # 100. 跨日 streak 先减半（昨日 5 条 → 今日 2 条，无新结算）
    st100 = m.load_state()
    st100["log"] = [{"ts": t0830 - 50 * 3600, "intent": "morning", "replied": False}]
    st100["mood"] = {"streak": 5, "date": "2026-10-20",
                     "seen_ts": t0830 - 40 * 3600}
    mo100 = m.mood_state(st100, "2026-10-21")
    check("100 跨日 streak 减半（5 → 2）", mo100["streak"] == 2, str(mo100))
    # 101. 有机间隔按待回复条数拉长：2 条待回复 → 采样间隔 ×2
    tnow = time.time()
    home, m = make_home(tnow, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0},
                        extra_state={"log": [
                            {"ts": tnow - 3600, "intent": "impulse", "replied": False},
                            {"ts": tnow - 1800, "intent": "impulse", "replied": False}]})
    st101 = m.mark_sent("impulse")
    gap101 = (float(st101["next_allowed_at"]) - float(st101["last_sent_at"])) / 3600
    check("101 两条待回复 → 有机间隔下限翻倍（≥0.6h）",
          0.599 <= gap101 <= 10.01, f"gap={gap101:.2f}h")
    # 102. 轻触：streak ≥ 4 + 静默远超静默阈值（无常回样本兜底）+
    #      light_touch_p=1 → 触发轻触（内容/时机断言 trigger 类型）
    t1500b = ts(2026, 10, 21, 15, 0)
    home, m = make_home(t1500b, mode="organic", persona=ORGAN_PERSONA,
                        ctx_age_h=20,
                        organic={"spontaneous_p": 0.0, "light_touch_p": 1.0},
                        extra_state={"log": [
                            {"ts": t1500b - k * 3600, "intent": "impulse",
                             "replied": False} for k in (50, 44, 38, 32, 26)]})
    plan102 = m._life.ensure_today(t1500b)
    st102 = json.load(open(os.path.join(home, "state", "proactive.json")))
    st102["organic"] = {"date": "2026-10-21",
                        "fired": [x["id"] for x in plan102["moments"]]
                                 + [e["id"] for e in plan102["events"]],
                        "silence_date": "2026-10-21"}
    json.dump(st102, open(os.path.join(home, "state", "proactive.json"), "w"),
              ensure_ascii=False)
    d = m.decide(now=t1500b)
    check("102 被晾 5 条且远超回复间隔 → 轻触触发",
          d.get("due") and d.get("intent") == "impulse"
          and (d.get("brief") or {}).get("trigger", {}).get("type") == "light_touch",
          str(d)[:160])
    # 103. 锚点犹豫：streak=2 时锚点首次到点先推迟半小时，之后照常发
    home, m = make_home(t0830, mode="organic", persona=ORGAN_PERSONA,
                        organic={"spontaneous_p": 0.0, "wakeup_skip_p": 0.0},
                        extra_state={"log": [
                            {"ts": t0830 - 26 * 3600, "intent": "morning", "replied": False},
                            {"ts": t0830 - 25 * 3600, "intent": "lunch", "replied": False}]})
    plan103 = m._life.ensure_today(t0830)
    wake103 = next(x for x in plan103["moments"] if x["kind"] == "wakeup")
    st103 = json.load(open(os.path.join(home, "state", "proactive.json")))
    st103["organic"] = {"date": "2026-10-21",
                        "fired": [e["id"] for e in plan103["events"]],
                        "silence_date": "2026-10-21"}
    json.dump(st103, open(os.path.join(home, "state", "proactive.json"), "w"),
              ensure_ascii=False)
    with open(os.path.join(home, "state", "context.json"), "w", encoding="utf-8") as f:
        json.dump({"context_token": "tok-test",
                   "received_at": float(wake103["ts"]) - 2 * 3600,
                   "stale": False}, f)
    t_hit = max(float(wake103["ts"]) + 300, ts(2026, 10, 21, 7, 5))
    d1 = m.decide(now=t_hit)
    st103b = json.load(open(os.path.join(home, "state", "proactive.json")))
    held103 = ((st103b.get("organic") or {}).get("held") or {})
    d2 = m.decide(now=t_hit + 31 * 60)
    check("103 streak=2 锚点犹豫：首次推迟、半小时后照常发",
          (not d1.get("due")) and wake103["id"] in held103
          and d2.get("due") and d2.get("intent") == "morning",
          f"d1={str(d1)[:80]} held={held103} d2={str(d2)[:80]}")
    # 104. daily_max 填数字时对 followup 不再豁免：已满 3 条 → followup 也拦
    home, m = make_home(t0830, followups=[{"about": "他的面试", "after_ts": t0830 - 60,
                                           "created": t0830 - 86400}],
                        extra_state={"log": [
                            {"ts": t0830 - 3 * 3600, "intent": "morning", "replied": True},
                            {"ts": t0830 - 2 * 3600, "intent": "lunch", "replied": True},
                            {"ts": t0830 - 3600, "intent": "impulse", "replied": True}]})
    d = m.decide(now=t0830)
    check("104 上限填数字已满时 followup 同样被拦（无豁免）",
          not d.get("due") and "滚动" in d.get("reason", ""), str(d)[:80])

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n==== proactive selftest: {passed}/{len(RESULTS)} PASS ====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
