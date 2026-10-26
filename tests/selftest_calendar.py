#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日历边界电池（第五轮线5）：合成时间逐项验各模块的日期逻辑。

覆盖：
  C1 午夜边界：proactive _roll_day / decide 日计数切换、derive_day_counts
     (E2) 归属、followup 跨日豁免与 48h 过期、worker two_stage 跨午夜清零。
  C2 周/月/年界：tune daily 对账跨月不漏日（10-31→11-02）与跨年 promote；
     user_profile 14 天窗滚动；worker summaries 按日键跨年不串。
  C3 旋钮日历：apply_decay 跨锁定期连续性（锁定期不衰减、到期瞬间恢复、
     到期后 locks 条目清除且 delta 可调）；生产 locks 到期点
     （2027-10-22/23）前后各 1h 合成验。
  C4 proactive 时段窗：slot_window 起止等号边界（decide 层窗首/窗尾各一例、
     窗外 1 分钟反例）；silence 档跨午夜 roll/attempts 口径与昨日被晾拦截。
  （源套件 C5 备份/保留边界测 scripts/backup_state.sh 与 prune_*.sh，
  通用版无这些脚本，该段已剔除，见 tests/README.md。）
     prune_outbox 30/90 天恰边界（严格 <：恰满天数保留、多 1 秒删）、
     中间产物 14 天档、media_jobs 24h TTL——全部合成 mtime + PRUNE_NOW 注入，
     不真跑生产数据。

沙箱办法照各套件既有口径：模块复制进临时 home + 环境变量指数据目录 +
monkeypatch 模块内 datetime/time/random 注入合成时间。绝不碰生产 state/。
用法：TZ=Asia/Shanghai python3 tests/selftest_calendar.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = ZoneInfo("Asia/Shanghai")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name
          + (f"（{detail}）" if detail and not ok else ""))


def note(msg):
    print("NOTE " + msg)


def ts(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp()


def fake_dt_class(fixed_ts):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            dt = datetime.fromtimestamp(fixed_ts, TZ)
            return dt if tz is not None else dt.replace(tzinfo=None)
    return FakeDT


def fake_time(fixed_ts):
    return SimpleNamespace(time=lambda: fixed_ts)


def fake_random():
    return SimpleNamespace(random=lambda: 0.0, randint=lambda a, b: a,
                           uniform=lambda a, b: a, choice=lambda seq: seq[0])


def load_sandbox_mod(modname, path):
    """加载复制进沙箱的模块：模块以 COMPANION_HOME 环境变量优先定
    HOME，worker 沙箱（c1e）设过该 env 会把后来加载的 proactive/tune
    沙箱模块的数据目录劫去 worker 沙箱——加载期临时摘除 env，让模块
    按脚本所在目录（沙箱 home）绑定，加载毕恢复。"""
    saved = os.environ.pop("COMPANION_HOME", None)
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        if saved is not None:
            os.environ["COMPANION_HOME"] = saved


# ---------------- proactive 沙箱 ----------------

def make_pro_home(now, slots=None, quiet=(1, 7), ctx_age_h=2.0,
                  extra_state=None, followups=None, history=None,
                  rhythm_extra=None, daily_max=18, silence_hours=2.5):
    home = tempfile.mkdtemp(prefix="cal-pro-")
    os.makedirs(os.path.join(home, "state"))
    os.makedirs(os.path.join(home, "inbox"))
    shutil.copy(os.path.join(ROOT, "proactive.py"),
                os.path.join(home, "proactive.py"))
    rhythm = {"impulse_p": 0.0, "photo_p": 0.0, "video_p": 0.0,
              "silence_evening_hours": 2, "followup_expire_hours": 48,
              "gap_hours": [0.4, 1.0], "impulse_daily_max": 8}
    if rhythm_extra:
        rhythm.update(rhythm_extra)
    pcfg = {"enabled": True, "daily_max": daily_max, "min_gap_hours": 0.75,
            "quiet_hours": list(quiet), "silence_hours": silence_hours,
            "active_skip_min": 15, "max_context_age_hours": 24,
            "unresolved_max": 3, "unreplied_stop": 8,
            "send_jitter_min": [0, 0],
            "slots": slots if slots is not None else
                     {"morning": {"p": 1.0, "window": ["07:30", "09:30"]}},
            "rhythm": rhythm}
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"tz": "Asia/Shanghai", "proactive": pcfg}, f,
                  ensure_ascii=False)
    st = {"date": None, "sent_count": 0, "last_sent_at": None,
          "last_intent": None, "awaiting_reply": False, "slot_rolls": {},
          "followups": followups or [], "enabled": True,
          "started_on": "2026-10-01", "log": []}
    if extra_state:
        st.update(extra_state)
    with open(os.path.join(home, "state", "proactive.json"), "w",
              encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    with open(os.path.join(home, "state", "context.json"), "w",
              encoding="utf-8") as f:
        json.dump({"context_token": "tok-test",
                   "received_at": now - ctx_age_h * 3600, "stale": False}, f)
    with open(os.path.join(home, "state", "counters.json"), "w",
              encoding="utf-8") as f:
        json.dump({"unreplied": 0}, f)
    with open(os.path.join(home, "state", "life.json"), "w",
              encoding="utf-8") as f:
        json.dump({"schedule": "下午赶稿", "small_events": ["小猫推倒了水杯"],
                   "mood_now": "平静", "busy_level": "mid"}, f)
    with open(os.path.join(home, "inbox", "m1.json"), "w",
              encoding="utf-8") as f:
        json.dump({"message_id": "m1", "from_user_id": "user1@im.wechat",
                   "text": "嗯", "create_time_ms": 1}, f)
    if history is not None:
        with open(os.path.join(home, "state", "history.jsonl"), "w",
                  encoding="utf-8") as f:
            for row in history:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    m = load_sandbox_mod("pro_cal_" + os.path.basename(home),
                         os.path.join(home, "proactive.py"))
    return home, m


def pro_saved(home):
    with open(os.path.join(home, "state", "proactive.json"),
              encoding="utf-8") as f:
        return json.load(f)


def set_ctx(home, received_at):
    with open(os.path.join(home, "state", "context.json"), "w",
              encoding="utf-8") as f:
        json.dump({"context_token": "tok-test", "received_at": received_at,
                   "stale": False}, f)


# ---------------- tune 沙箱 ----------------

def make_tune_home():
    home = tempfile.mkdtemp(prefix="cal-tune-")
    os.makedirs(os.path.join(home, "state"))
    shutil.copy(os.path.join(ROOT, "tune.py"), os.path.join(home, "tune.py"))
    _spec = importlib.util.spec_from_file_location(
        "tune_defaults_cal", os.path.join(ROOT, "tune.py"))
    _tm = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tm)
    knobs = json.loads(json.dumps(_tm.DEFAULT_KNOBS))
    cfg = {"tz": "Asia/Shanghai",
           "tune": {"enabled": True, "cmd_prefix": "#", "ack": "brief",
                    "decay_hours": 12, "decay_factor": 0.7,
                    "promote_after_days": 3, "learn_rate": 0.1,
                    "knobs": knobs}}
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    shutil.copy(os.path.join(ROOT, "persona.example.json"),
                os.path.join(home, "persona.json"))
    m = load_sandbox_mod("tune_cal_" + os.path.basename(home),
                         os.path.join(home, "tune.py"))
    return home, m


def write_tune_state(home, obj):
    with open(os.path.join(home, "state", "tune.json"), "w",
              encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


# ================= C1 午夜边界 =================

def c1():
    # C1a _roll_day 纯函数口径 + mark_sent 的日切（mark 内即 _roll_day）
    _, m = make_pro_home(ts(2026, 10, 25, 12, 0))
    st = {"date": "2026-10-25", "sent_count": 7,
          "slot_rolls": {"morning": {"date": "2026-10-25", "hit": True}},
          "video_sends_today": 3, "started_on": "2026-10-01"}
    m._roll_day(st, "2026-10-25")
    check("C1a-1 同日 _roll_day 不清零",
          st["sent_count"] == 7 and st["video_sends_today"] == 3
          and "morning" in st["slot_rolls"])
    m._roll_day(st, "2026-10-26")
    check("C1a-2 跨日 _roll_day 清零计数/rolls/视频数、started_on 保留",
          st["date"] == "2026-10-26" and st["sent_count"] == 0
          and st["slot_rolls"] == {} and st["video_sends_today"] == 0
          and st["started_on"] == "2026-10-01", f"st={st}")
    # mark_sent 用真实时钟：昨天的 state 今天记账应先清零再 +1
    today_real = datetime.now(TZ).date()
    yday_real = (today_real - timedelta(days=1)).isoformat()
    home2, m2 = make_pro_home(time.time(), extra_state={
        "date": yday_real, "sent_count": 5, "log": []})
    st2 = m2.mark_sent("morning")
    check("C1a-3 mark_sent 跨日先清零再记账（5→1 而非 6）",
          st2["date"] == today_real.isoformat() and st2["sent_count"] == 1,
          f"date={st2['date']} count={st2['sent_count']}")
    home3, m3 = make_pro_home(time.time(), extra_state={
        "date": today_real.isoformat(), "sent_count": 5, "log": []})
    st3 = m3.mark_sent("morning")
    check("C1a-4 mark_sent 同日累加（5→6）", st3["sent_count"] == 6)

    # C1b decide 23:59 → 00:01 序列：日期归属与计数切换
    n1 = ts(2026, 10, 25, 23, 59)
    home, m = make_pro_home(n1, ctx_age_h=1.0,
                            extra_state={"date": "2026-10-25",
                                         "sent_count": 5, "log": []})
    m.decide(now=n1)
    sv = pro_saved(home)
    check("C1b-1 decide@23:59 state 日期仍归当日、计数不清零",
          sv["date"] == "2026-10-25" and sv["sent_count"] == 5,
          f"sv={sv.get('date')},{sv.get('sent_count')}")
    n2 = ts(2026, 10, 26, 0, 1)
    set_ctx(home, n2 - 3600)
    m.decide(now=n2)
    sv = pro_saved(home)
    check("C1b-2 decide@00:01 切到新日、sent_count/rolls 清零",
          sv["date"] == "2026-10-26" and sv["sent_count"] == 0
          and sv["slot_rolls"] == {}, f"sv={sv.get('date')},{sv.get('sent_count')}")
    # 午夜前命中时段 roll、午夜后新日可再 roll（rolls 不跨日残留）
    e1 = ts(2026, 10, 25, 23, 59)
    home, m = make_pro_home(
        e1, slots={"evening": {"p": 1.0, "window": ["23:00", "23:59"]},
                   "morning": {"p": 1.0, "window": ["07:30", "09:30"]}},
        ctx_age_h=2.0)
    m.random = fake_random()
    r = m.decide(now=e1)
    check("C1b-3 23:59 evening 窗尾命中", r.get("due") is True
          and r.get("intent") == "evening", f"r={r}")
    e2 = ts(2026, 10, 26, 8, 0)
    set_ctx(home, e2 - 7200)
    r = m.decide(now=e2)
    sv = pro_saved(home)
    check("C1b-4 次日 08:00 morning 可重新 roll 命中（rolls 已换日）",
          r.get("due") is True and r.get("intent") == "morning"
          and sv["slot_rolls"].get("morning", {}).get("date") == "2026-10-26"
          and "evening" not in sv["slot_rolls"], f"r={r} rolls={sv.get('slot_rolls')}")

    # C1c derive_day_counts 跨月归属（fake now = 2026-11-01）
    _, m = make_pro_home(ts(2026, 11, 1, 12, 0))
    m.datetime = fake_dt_class(ts(2026, 11, 1, 12, 0))
    st = {"log": [
        {"ts": ts(2026, 10, 31, 23, 59), "intent": "evening"},
        {"ts": ts(2026, 11, 1, 0, 1), "intent": "morning"},
        {"ts": ts(2026, 11, 1, 0, 2), "intent": "morning"},
        {"ts": ts(2026, 10, 30, 12, 0), "intent": "lunch", "voided": True},
        {"ts": ts(2026, 10, 29, 23, 59), "intent": "lunch"}]}
    got = m.derive_day_counts(st, days=3)
    check("C1c-1 derive_day_counts 跨月归属（23:59 归 10-31、00:01 归 11-01、voided 剔除）",
          got == {"2026-11-01": 2, "2026-10-31": 1, "2026-10-30": 0},
          f"got={got}")

    # C1d followup 跨日：昨日约定的 followup 次日仍 due 且豁免滚动上限；
    # 超 48h 的被清；无 followup 时同台账被上限挡住（对照）
    now = ts(2026, 10, 26, 10, 0)
    log20 = [{"ts": now - 3600 - i * 60, "intent": "morning",
              "replied": True} for i in range(20)]
    home, m = make_pro_home(
        now, ctx_age_h=1.0, extra_state={"log": log20},
        followups=[{"about": "他提过的面试", "after_ts": ts(2026, 10, 25, 9, 0)}])
    r = m.decide(now=now)
    check("C1d-1 昨日 followup 次日 due 且豁免滚动 24h 上限",
          r.get("due") is True and r.get("intent") == "followup",
          f"r={r}")
    home, m = make_pro_home(
        now, ctx_age_h=1.0, extra_state={"log": log20},
        followups=[{"about": "太久以前的事", "after_ts": now - 49 * 3600}])
    r = m.decide(now=now)
    sv = pro_saved(home)
    check("C1d-2 followup 超 48h 过期被清、不再 due",
          r.get("due") is not True and sv.get("followups") == [],
          f"r={r} followups={sv.get('followups')}")
    home, m = make_pro_home(
        ts(2026, 10, 26, 8, 0), ctx_age_h=1.0,
        extra_state={"log": log20})
    r = m.decide(now=ts(2026, 10, 26, 8, 0))
    check("C1d-3 对照：无 followup 时同台账被滚动上限挡住",
          r.get("due") is not True and "上限" in str(r.get("reason", "")),
          f"r={r}")


def c1e_worker():
    """worker two_stage 跨午夜清零（进程内加载 worker，数据目录指沙箱）。"""
    home = tempfile.mkdtemp(prefix="cal-worker-")
    for d in ("state", "inbox", "outbox/pending", "outbox/sent",
              "outbox/failed"):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.environ["COMPANION_HOME"] = home

    def wjson(rel, obj):
        with open(os.path.join(home, rel), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    fixed = ts(2026, 10, 26, 0, 1)
    wjson("state/life.json", {"date": "2026-10-26", "busy_level": "high",
                              "schedule": "全天忙工作，晚上还要开会",
                              "mood_now": "平静", "small_events": [],
                              "carry_over": ""})
    wjson("state/counters.json", {"unreplied": 0})
    wjson("state/two_stage.json", {"date": "2026-10-25", "count": 2})
    for rel in ("state/history.jsonl", "state/reply_records.jsonl"):
        open(os.path.join(home, rel), "w").close()
    spec = importlib.util.spec_from_file_location(
        "worker_cal", os.path.join(ROOT, "worker.py"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    w.datetime = fake_dt_class(fixed)
    check("C1e-1 two_stage_today 昨日计数按 0（跨午夜清零口径）",
          w.two_stage_today() == 0)
    dec = w.two_stage_decision("chat", now=fixed, roll=0.0)
    saved = json.load(open(os.path.join(home, "state", "two_stage.json"),
                           encoding="utf-8"))
    check("C1e-2 跨日后可重新触发且计数落新日（count=1@2026-10-26）",
          dec.get("trigger") is True and saved == {"date": "2026-10-26",
                                                   "count": 1},
          f"dec={dec} saved={saved}")
    check("C1e-3 two_stage_today 新日读到 1", w.two_stage_today() == 1)
    wjson("state/two_stage.json", {"date": "2026-10-26", "count": 2})
    dec = w.two_stage_decision("chat", now=fixed, roll=0.0)
    check("C1e-4 同日上限 2 仍生效（清零不等于无限）",
          dec.get("trigger") is False and "2 次" in str(dec.get("reason", "")),
          f"dec={dec}")
    return home, w


# ================= C2 周/月/年界 =================

def c2(home_w, w):
    # C2a tune daily 跨月对账：快照 10-31/11-01 已存，11-02 跑 daily
    home, m = make_tune_home()
    fixed = ts(2026, 11, 2, 23, 50)
    m.datetime = fake_dt_class(fixed)
    m.time = fake_time(fixed)
    write_tune_state(home, {
        "deltas": {"playfulness": 1.0}, "locks": {}, "updated_at": fixed,
        "snapshots": {"2026-10-31": {"playfulness": 1.0},
                      "2026-11-01": {"playfulness": 1.0}}})
    with open(os.path.join(home, "state", "feedback.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts(2026, 10, 31, 12, 0), "score": 1.0}) + "\n")
        f.write(json.dumps({"ts": ts(2026, 11, 2, 10, 0), "score": 0.5}) + "\n")
    with open(os.path.join(home, "state", "proactive.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": "2026-11-02", "sent_count": 1,
                   "log": [{"ts": ts(2026, 11, 2, 9, 0), "intent": "morning"},
                           {"ts": ts(2026, 10, 31, 23, 59),
                            "intent": "evening"}]}, f)
    out = m.daily()
    summ = out["summary"]
    check("C2a-1 daily 跨月：摘要日=11-02、三日快照齐全",
          summ["date"] == "2026-11-02"
          and set(m.load_tune()["snapshots"].keys())
          == {"2026-10-31", "2026-11-01", "2026-11-02"},
          f"summ={summ}")
    check("C2a-2 daily 反馈只算当日（10-31 的不串入 11-02）",
          summ["n_feedback"] == 1 and summ["avg_score"] == 0.5,
          f"summ={summ}")
    check("C2a-3 daily 近 3 日对账跨月不漏日（10-31 计 1）",
          summ["proactive_reconcile"]["derived"]
          == {"2026-11-02": 1, "2026-11-01": 0, "2026-10-31": 1},
          f"rec={summ.get('proactive_reconcile')}")
    props = json.load(open(os.path.join(home, "state", "proposals.json"),
                           encoding="utf-8")) if os.path.exists(
        os.path.join(home, "state", "proposals.json")) else {"proposals": []}
    check("C2a-4 跨月连续 3 日同向 → promote 建议生成",
          out["proposals_created"] and any(
              p.get("knob") == "playfulness" and p.get("status") == "pending"
              for p in props["proposals"]), f"out={out}")
    # 跨年 promote：2026-12-31 / 2027-01-01 / 2027-01-02
    home, m = make_tune_home()
    fixed = ts(2027, 1, 2, 23, 50)
    m.datetime = fake_dt_class(fixed)
    m.time = fake_time(fixed)
    write_tune_state(home, {
        "deltas": {"playfulness": -1.0}, "locks": {}, "updated_at": fixed,
        "snapshots": {"2026-12-31": {"playfulness": -1.0},
                      "2027-01-01": {"playfulness": -1.0}}})
    out = m.daily()
    check("C2a-5 跨年连续 3 日同向 → promote 建议生成（不串年）",
          bool(out["proposals_created"])
          and out["summary"]["date"] == "2027-01-02", f"out={out}")

    # C2b user_profile 14 天窗滚动（fake now = 2026-10-25 12:00）
    fixed = ts(2026, 10, 25, 12, 0)
    rows = []
    for day in (21, 22, 23, 24, 25):
        for h in (9, 12, 18, 21, 22):
            rows.append({"role": "user", "ts": ts(2026, 10, day, h),
                         "text": "x"})
    for i in range(10):  # 第 15 天（10-10）的数据：整批应滑出窗外
        rows.append({"role": "user", "ts": ts(2026, 10, 10, 3, i),
                     "text": "old"})
    _, m = make_pro_home(fixed, history=rows)
    m.time = fake_time(fixed)
    prof = m.user_profile()
    check("C2b-1 第 15 天数据滑出：total/days/first_med 不受旧数据影响",
          prof is not None and prof["total"] == 25 and prof["days"] == 5
          and prof["first_med"] == 9.0, f"prof={prof}")
    rows2 = rows + [{"role": "user", "ts": fixed - 14 * 86400, "text": "edge"},
                    {"role": "user", "ts": fixed - 14 * 86400 - 3600,
                     "text": "out"}]
    _, m = make_pro_home(fixed, history=rows2)
    m.time = fake_time(fixed)
    prof = m.user_profile()
    check("C2b-2 窗界等号：恰 14 天整计入、早 1 小时滑出（total 25→26）",
          prof is not None and prof["total"] == 26 and prof["days"] == 6,
          f"prof={prof}")

    # C2c worker summaries 跨年：2027-01-01 的「昨天」是 2026-12-31
    fixed = ts(2027, 1, 1, 9, 0)
    w.datetime = fake_dt_class(fixed)
    prompts = []

    def fake_llm(prompt, kind=None):
        prompts.append(prompt)
        return json.dumps({"summary": "跨年摘要", "unfinished": "接上次的话题"})

    w.llm = SimpleNamespace(call=fake_llm)
    hist = [{"role": "user", "text": "跨年夜聊了很久", "ts": ts(2026, 12, 31, 23, 59)},
            {"role": "user", "text": "新年第一句", "ts": ts(2027, 1, 1, 0, 1)},
            {"role": "user", "text": "前天的话", "ts": ts(2026, 12, 30, 23, 0)}]
    text, _unf = w.summarize_yesterday(hist)
    check("C2c-1 summarize_yesterday 跨年只取 2026-12-31 全天",
          text == "跨年摘要" and prompts
          and "跨年夜聊了很久" in prompts[0]
          and "新年第一句" not in prompts[0]
          and "前天的话" not in prompts[0],
          f"text={text}")
    with open(os.path.join(home_w, "state", "summaries.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"date": "2026-12-30", "text": "旧摘要",
                            "unfinished": ""}, ensure_ascii=False) + "\n")
    with open(os.path.join(home_w, "state", "daily_prep.json"), "w",
              encoding="utf-8") as f:
        json.dump({}, f)
    with open(os.path.join(home_w, "state", "history.jsonl"), "w",
              encoding="utf-8") as f:
        for r in hist:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    w.daily_prep()
    sums = [json.loads(ln) for ln in open(
        os.path.join(home_w, "state", "summaries.jsonl"), encoding="utf-8")
        if ln.strip()]
    prep = json.load(open(os.path.join(home_w, "state", "daily_prep.json"),
                          encoding="utf-8"))
    life = json.load(open(os.path.join(home_w, "state", "life.json"),
                          encoding="utf-8"))
    check("C2c-2 daily_prep 跨年：摘要按 2026-12-31 键落盘、prep/life 归 2027-01-01",
          [s["date"] for s in sums] == ["2026-12-30", "2026-12-31"]
          and prep.get("date") == "2027-01-01"
          and life.get("date") == "2027-01-01",
          f"sums={[s['date'] for s in sums]} prep={prep} life={life.get('date')}")
    w.daily_prep()
    sums2 = [json.loads(ln) for ln in open(
        os.path.join(home_w, "state", "summaries.jsonl"), encoding="utf-8")
        if ln.strip()]
    check("C2c-3 daily_prep 同日重跑不重复落摘要",
          len(sums2) == 2, f"n={len(sums2)}")


# ================= C3 旋钮日历 =================

def c3():
    _, m = make_tune_home()
    t0 = ts(2026, 10, 25, 12, 0)
    # 未锁定：12h 一档连续衰减、跨界指数连续
    t = {"deltas": {"playfulness": 2.0}, "locks": {}, "updated_at": t0}
    m.apply_decay(t, now=t0 + 12 * 3600)
    v1 = t["deltas"]["playfulness"]
    t = {"deltas": {"playfulness": 2.0}, "locks": {}, "updated_at": t0}
    m.apply_decay(t, now=t0 + 24 * 3600)
    check("C3-1 未锁定衰减指数连续（12h→×0.7、24h→×0.49）",
          v1 == 1.4 and t["deltas"]["playfulness"] == 0.98,
          f"v1={v1} v2={t['deltas']['playfulness']}")
    # 锁定期内不衰减
    E = ts(2027, 10, 22, 13, 57, 13)  # 合成到期点（口径同生产 playfulness 锁）
    t = {"deltas": {"playfulness": 2.0}, "locks": {"playfulness": float(E)},
         "updated_at": E - 24 * 3600}
    m.apply_decay(t, now=E - 3600)
    check("C3-2 到期前 1h：locked delta 纹丝不动、锁保留",
          t["deltas"]["playfulness"] == 2.0 and "playfulness" in t["locks"],
          f"t={t}")
    # 到期瞬间：锁清除、delta 仍不动（可衰减时长为 0）
    t = {"deltas": {"playfulness": 2.0}, "locks": {"playfulness": float(E)},
         "updated_at": E - 3600}
    m.apply_decay(t, now=float(E))
    check("C3-3 到期瞬间：locks 条目清除、delta 不突跳",
          "playfulness" not in t["locks"] and t["deltas"]["playfulness"] == 2.0,
          f"t={t}")
    # 到期后跨界：只衰减到期后的 1h，不补扣锁定期（本轮修复的回归锁）
    t = {"deltas": {"playfulness": 2.0}, "locks": {"playfulness": float(E)},
         "updated_at": E - 3600}
    m.apply_decay(t, now=E + 3600)
    expect = round(2.0 * 0.7 ** (1 / 12), 3)
    check("C3-4 到期后 1h：只按到期后时长衰减（不补扣锁定期的 1h）",
          "playfulness" not in t["locks"]
          and abs(t["deltas"]["playfulness"] - expect) < 0.001,
          f"got={t['deltas']['playfulness']} expect={expect}")
    # 生产 locks 到期点（值逐字取自 2026-10-25 生产 state/tune.json）
    LF, LB = 1824184632.9989607, 1824282929.8419557  # playfulness 2027-10-22 / bubbles 2027-10-23
    t = {"deltas": {"playfulness": 2.0, "bubbles": 1.0},
         "locks": {"playfulness": LF, "bubbles": LB}, "updated_at": LF - 3600}
    m.apply_decay(t, now=LF - 1800)
    check("C3-5 生产到期点前 30min：两锁都在、delta 都不动",
          t["deltas"] == {"playfulness": 2.0, "bubbles": 1.0}
          and set(t["locks"]) == {"playfulness", "bubbles"}, f"t={t}")
    t = {"deltas": {"playfulness": 2.0, "bubbles": 1.0},
         "locks": {"playfulness": LF, "bubbles": LB}, "updated_at": LF - 3600}
    m.apply_decay(t, now=LF + 3600)
    check("C3-6 playfulness 到期后 1h：playfulness 锁清除且已恢复衰减、bubbles 仍锁死不动",
          "playfulness" not in t["locks"] and "bubbles" in t["locks"]
          and abs(t["deltas"]["playfulness"] - expect) < 0.001
          and t["deltas"]["bubbles"] == 1.0, f"t={t}")
    # 到期后 delta 可调：adjust 经 apply_decay 清锁后正常落盘
    home, m = make_tune_home()
    m.time = fake_time(LF + 3600)
    write_tune_state(home, {"deltas": {"playfulness": 2.0}, "locks": {"playfulness": LF},
                            "updated_at": LF, "snapshots": {}})
    new = m.adjust("playfulness", -0.5)
    saved = m.load_tune()
    _spec2 = importlib.util.spec_from_file_location(
        "tune_defaults_cal2", os.path.join(ROOT, "tune.py"))
    _tm2 = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(_tm2)
    base = float(_tm2.DEFAULT_KNOBS["playfulness"]["base"])
    check("C3-7 到期后 adjust 可调：锁被清、delta 落盘且与返回值一致",
          "playfulness" not in saved.get("locks", {})
          and new is not None
          and abs((base + float(saved["deltas"]["playfulness"])) - new) < 0.001
          and abs(float(saved["deltas"]["playfulness"])
                  - (round(2.0 * 0.7 ** (1 / 12), 3) - 0.5)) < 0.002,
          f"new={new} saved={saved.get('deltas')} locks={saved.get('locks')}")
    # 观察项（设计口径，不判失败）：锁定期内 adjust/set_value 本身并不拒绝
    home, m = make_tune_home()
    m.time = fake_time(LF - 3600)
    write_tune_state(home, {"deltas": {"playfulness": 2.0}, "locks": {"playfulness": LF},
                            "updated_at": LF - 3600, "snapshots": {}})
    m.adjust("playfulness", -0.5)
    saved = m.load_tune()
    note(f"C3-观察：锁定期内 adjust 仍改 delta（{saved['deltas'].get('playfulness')}，"
         "locks 只约束衰减与 promote——设计口径，见回传报告)")


# ================= C4 时段窗与 silence =================

def c4():
    _, m = make_pro_home(ts(2026, 10, 26, 12, 0))
    slot = {"window": ["07:30", "09:30"]}
    mon = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)   # 周一
    sun = datetime(2026, 10, 25, 8, 0, tzinfo=TZ)   # 周日
    check("C4-1 slot_window 无档案用 config 窗（07:30–09:30 → 450–570）",
          m.slot_window("morning", slot, mon, None) == (450, 570))
    prof = {"first_med": 8.0, "last_med": 23.0, "active_hours": set(range(9, 23)),
            "days": 6, "total": 30}
    check("C4-2 slot_window 有档案：工作日早安贴 first_med-0.5h、周末 +1h",
          m.slot_window("morning", slot, mon, prof) == (450, 570)
          and m.slot_window("morning", slot, sun, prof) == (510, 630),
          f"mon={m.slot_window('morning', slot, mon, prof)} "
          f"sun={m.slot_window('morning', slot, sun, prof)}")
    check("C4-3 slot_window 晚安贴 last_med-1h（22:00–23:10 → 1320–1390）",
          m.slot_window("goodnight", {"window": ["22:30", "23:40"]}, mon,
                        prof) == (1320, 1390))

    # decide 层等号边界：窗首/窗尾各一例 + 窗外 1 分钟反例
    def slot_case(hh, mi, slots=None):
        now = ts(2026, 10, 26, hh, mi)
        home, mm = make_pro_home(now, slots=slots, ctx_age_h=2.0)
        mm.random = fake_random()
        return mm.decide(now=now), home, mm

    r, _, _ = slot_case(7, 30)
    check("C4-4 窗首等号 07:30 命中 morning", r.get("due") is True
          and r.get("intent") == "morning", f"r={r}")
    r, _, _ = slot_case(9, 30)
    check("C4-5 窗尾等号 09:30 命中 morning（闭区间）", r.get("due") is True
          and r.get("intent") == "morning", f"r={r}")
    r, _, _ = slot_case(9, 31)
    check("C4-6 窗尾 +1min 09:31 不命中", r.get("due") is not True, f"r={r}")
    r, _, _ = slot_case(7, 29)
    check("C4-7 窗首 -1min 07:29 不命中", r.get("due") is not True, f"r={r}")
    gn = {"goodnight": {"p": 1.0, "window": ["22:30", "23:40"]}}
    r, _, _ = slot_case(23, 40, slots=gn)
    check("C4-8 goodnight 窗尾等号 23:40 命中", r.get("due") is True
          and r.get("intent") == "goodnight", f"r={r}")
    r, _, _ = slot_case(23, 41, slots=gn)
    check("C4-9 goodnight 窗尾 +1min 23:41 不命中",
          r.get("due") is not True, f"r={r}")

    # silence：窗口 9–22（左闭右开）；21:59 可 roll、23:30 不可
    now = ts(2026, 10, 25, 21, 59)
    home, m = make_pro_home(now, slots={}, ctx_age_h=6.0)
    m.random = fake_random()
    r = m.decide(now=now)
    sv = pro_saved(home)
    check("C4-10 silence 21:59 命中、roll 落当日 attempts=1",
          r.get("due") is True and r.get("intent") == "silence"
          and sv["slot_rolls"]["silence"]["date"] == "2026-10-25"
          and sv["slot_rolls"]["silence"]["attempts"] == 1,
          f"r={r} rolls={sv.get('slot_rolls')}")
    now = ts(2026, 10, 25, 23, 30)
    home, m = make_pro_home(now, slots={}, ctx_age_h=10.0)
    m.random = fake_random()
    r = m.decide(now=now)
    check("C4-11 silence 23:30 在窗口外（≥22）不触发（跨午夜不续）",
          r.get("due") is not True, f"r={r}")
    # 当日第二次机会：首次 miss 满 1h 后可再 roll、attempts 累到 2
    now = ts(2026, 10, 25, 21, 59)
    home, m = make_pro_home(now, slots={}, ctx_age_h=6.0, extra_state={
        "date": "2026-10-25",
        "slot_rolls": {"silence": {"date": "2026-10-25", "hit": False,
                                   "rolled_at": ts(2026, 10, 25, 20, 30),
                                   "attempts": 1}}})
    m.random = fake_random()
    r = m.decide(now=now)
    sv = pro_saved(home)
    check("C4-12 silence 当日第二次 roll：attempts 1→2 且可命中",
          r.get("due") is True and r.get("intent") == "silence"
          and sv["slot_rolls"]["silence"]["attempts"] == 2,
          f"r={r} rolls={sv.get('slot_rolls')}")
    # 跨午夜 attempts 归零：昨日 attempts 已满 2，今日经 _roll_day 换日重开
    now = ts(2026, 10, 26, 10, 0)
    home, m = make_pro_home(now, slots={}, ctx_age_h=6.0, extra_state={
        "date": "2026-10-25",
        "slot_rolls": {"silence": {"date": "2026-10-25", "hit": False,
                                   "rolled_at": ts(2026, 10, 25, 23, 0),
                                   "attempts": 2}}})
    m.random = fake_random()
    r = m.decide(now=now)
    sv = pro_saved(home)
    check("C4-13 跨午夜 silence attempts 重新计（昨日满 2 → 今日 attempts=1）",
          r.get("due") is True and r.get("intent") == "silence"
          and sv["slot_rolls"]["silence"]["date"] == "2026-10-26"
          and sv["slot_rolls"]["silence"]["attempts"] == 1,
          f"r={r} rolls={sv.get('slot_rolls')}")
    # 昨日 silence 被晾 → 今日同招拦截（sil_recent 跨日判定）
    now = ts(2026, 10, 26, 10, 0)
    home, m = make_pro_home(now, slots={}, ctx_age_h=6.0, extra_state={
        "log": [{"ts": ts(2026, 10, 25, 20, 0), "intent": "silence",
                 "replied": False}]})
    m.random = fake_random()
    r = m.decide(now=now)
    check("C4-14 昨日 silence 被晾 → 今日 silence 被 sil_recent 拦截",
          r.get("due") is not True, f"r={r}")


def main():
    c1()
    home_w, w = c1e_worker()
    c2(home_w, w)
    c3()
    c4()
    n_pass = sum(1 for _, ok in RESULTS if ok)
    print(f"\n== calendar: {n_pass}/{len(RESULTS)} PASS ==")
    return 0 if n_pass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
