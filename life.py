#!/usr/bin/env python3
"""
life.py — 角色生活圈子 / 习惯 / 每日生活状态 + 主人适配画像（开源通用版）。

这个模块回答一个问题：「她今天在过什么样的日子？」

- 人设扩展（写在 persona.json 的 life 里，或 facts.json 的 life 里，
  facts 同名键优先）：circle（朋友/同事/家人/宠物，各带称呼、关系与
  常一起做的事）、habits（日常仪式与习惯）、routine（工作日/休息日
  作息骨架：起床、上班、午休、下班、入睡时刻）。
- 每日生活状态：每天按「日期 + 角色名」种子确定性采样一次，落
  state/life_today.json：今日日程块、主动锚点时刻（带随机抖动与当日
  跳过标记）、1-3 件可分享的生活事件（各带分享时刻与权重）、心情
  基调、长静默关怀阈值。同一天重复调用结果一致，方便复现与测试。
  proactive 的有机模式（proactive.mode="organic"）按这份计划行动，
  worker 的每日简报也读它，让她聊天时能自然带出自己的生活。
- 主人适配画像：从本地 history.jsonl 统计主人近 14 天的活跃时段、
  常回时延与互动密度，落 state/owner_profile.json；生活锚点（早安/
  晚安）向主人作息轻微偏移，事件分享时刻优先落在主人大概率在的
  时段。偏移有硬封顶（默认 60 分钟，config 可调更小），样本不足
  （<20 条或 <5 天）不做任何偏移。全部数据只存本地 state/，不上传；
  proactive.organic.owner_adapt.enabled=false 可整体关闭。

CLI: today / ensure / owner / summary（均输出 JSON，供调试与自检）
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("COMPANION_HOME") or HERE
STATE = os.path.join(HOME, "state")
CONFIG_FILE = os.path.join(HOME, "config.json")
PERSONA_FILE = os.path.join(HOME, "persona.json")
FACTS_FILE = os.path.join(HOME, "facts.json")
HISTORY_FILE = os.path.join(STATE, "history.jsonl")
LIFE_TODAY_FILE = os.path.join(STATE, "life_today.json")
OWNER_PROFILE_FILE = os.path.join(STATE, "owner_profile.json")

# 无人设 routine 时的中性兜底作息（与示例人设的「7 点半起、23 点睡」一致）
DEFAULT_ROUTINE = {
    "workday": {"wake": "07:30", "work_start": "09:00",
                "lunch": ["12:00", "13:00"], "work_end": "18:00",
                "sleep": "23:00"},
    "restday": {"wake": "08:30", "sleep": "23:30"},
}
DEFAULT_MOODS = {
    "high": ["有点累", "事情好多", "想歇一会儿"],
    "mid": ["还行", "平平淡淡", "有点困"],
    "low": ["心情不错", "懒洋洋的", "想出去走走"],
}
# 通用生活事件池（中性、无角色绑定）：与 circle / habits 事件混合采样
GENERIC_EVENTS = [
    "买到一件喜欢的新衣服",
    "去运动出了一身汗",
    "试着做了一道新菜",
    "追的剧更新了，一口气看了两集",
    "把房间彻底收拾了一遍",
    "收到一个期待很久的快递",
    "在楼下碰到一只很亲人的猫",
    "学了个小技能，还不太熟练",
    "翻到以前的照片，笑出声来",
    "出门忘带伞，淋了一小段雨",
]


def read_json(path, default=None):
    # 与 proactive.read_json 同口径：只认 dict 顶层，损坏回 default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def write_json(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def read_jsonl(path):
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


def organic_cfg():
    pcfg = cfg().get("proactive") or {}
    oc = pcfg.get("organic") or {}
    return oc if isinstance(oc, dict) else {}


def _f(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hhmm_to_min(s, default_min):
    try:
        h, m = str(s).split(":")[:2]
        return int(h) * 60 + int(m)
    except (TypeError, ValueError):
        return default_min


def _min_to_hhmm(minutes):
    minutes = int(max(0, min(1439, round(minutes))))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------- 人设生活设定（circle / habits / routine） ----------------

def persona_doc():
    return read_json(PERSONA_FILE, {}) or {}


def persona_life():
    """角色生活设定：persona.json 的 life 为主，facts.json 的 life 同名
    键覆盖（circle / habits / routine / moods / small_events 等逐键取）。"""
    life = {}
    pdoc = persona_doc()
    if isinstance(pdoc.get("life"), dict):
        life.update(pdoc["life"])
    facts = read_json(FACTS_FILE, {}) or {}
    if isinstance(facts.get("life"), dict):
        for k, v in facts["life"].items():
            if v not in (None, "", [], {}):
                life[k] = v
    return life


def persona_name():
    return str(persona_doc().get("name") or "persona")


def routine_for(day_kind, life=None):
    life = life if life is not None else persona_life()
    routine = life.get("routine") or {}
    base = dict(DEFAULT_ROUTINE.get(day_kind) or DEFAULT_ROUTINE["workday"])
    over = routine.get(day_kind) or {}
    if isinstance(over, dict):
        for k, v in over.items():
            if v not in (None, ""):
                base[k] = v
    return base


# ---------------- 主人适配画像 ----------------

def _median(vals):
    vals = sorted(vals)
    return vals[len(vals) // 2] if vals else 0.0


def compute_owner_profile(now=None):
    """从近 14 天 history 算主人作息画像并落盘。样本不足返回带
    insufficient 标记的空画像（不做偏移）。owner_adapt.enabled=false
    时直接返回空画像且不读 history。"""
    now = now or time.time()
    oc = organic_cfg()
    adapt = oc.get("owner_adapt") or {}
    if adapt.get("enabled", True) is False:
        prof = {"updated_at": now, "insufficient": True, "adapt_disabled": True,
                "active_hours": [], "adapt": {"wake_shift_min": 0,
                                               "sleep_shift_min": 0}}
        write_json(OWNER_PROFILE_FILE, prof)
        return prof
    cutoff = now - 14 * 86400
    rows = [h for h in read_jsonl(HISTORY_FILE)
            if float(h.get("ts", 0) or 0) >= cutoff]
    rows.sort(key=lambda h: float(h.get("ts", 0) or 0))
    user_rows = [h for h in rows if h.get("role") == "user"]
    prof = {"updated_at": now, "insufficient": True, "active_hours": [],
            "adapt": {"wake_shift_min": 0, "sleep_shift_min": 0}}
    if len(user_rows) >= 20:
        by_day = {}
        hours = {}
        for h in user_rows:
            dt = datetime.fromtimestamp(float(h["ts"]), tz())
            by_day.setdefault(dt.date(), []).append(dt.hour + dt.minute / 60.0)
            hours[dt.hour] = hours.get(dt.hour, 0) + 1
        if len(by_day) >= 5:
            total = sum(hours.values())
            active = sorted(h for h, n in hours.items()
                            if n >= max(2, total * 0.06))
            # 常回时延：她上一条消息之后、他 3 小时内回复的间隔中位数
            lat = []
            last_other = None
            for h in rows:
                ts = float(h.get("ts", 0) or 0)
                if h.get("role") == "user":
                    if last_other and 0 < ts - last_other <= 3 * 3600:
                        lat.append((ts - last_other) / 60.0)
                else:
                    last_other = ts
            first_med = _median([min(v) for v in by_day.values()])
            last_med = _median([max(v) for v in by_day.values()])
            # 锚点偏移（分钟，向主人作息靠拢，硬封顶在画像里就先封一次，
            # 生成计划时再按 config 的 shift_max_min 封第二次）：
            # 早安锚 = 她起床 +30 分；他通常更晚出现 → 锚点后移（≤+60），
            # 他更早 → 前移（≥-30）。晚安锚 = 她入睡 -20 分；同理 +45/-30。
            life = persona_life()
            wk = routine_for("workday", life)
            wake_anchor = _hhmm_to_min(wk.get("wake"), 450) + 30
            sleep_anchor = _hhmm_to_min(wk.get("sleep"), 1380) - 20
            first_min, last_min = first_med * 60, last_med * 60
            wake_shift = 0
            if first_min > wake_anchor + 30:
                wake_shift = min(first_min - wake_anchor, 60)
            elif first_min < wake_anchor - 30:
                wake_shift = max(first_min - wake_anchor, -30)
            sleep_shift = 0
            if last_min > sleep_anchor:
                sleep_shift = min(last_min - sleep_anchor, 45)
            elif last_min < sleep_anchor - 60:
                sleep_shift = max(last_min - sleep_anchor, -30)
            prof = {
                "updated_at": now, "insufficient": False,
                "days": len(by_day), "total": total,
                "hour_hist": {str(h): hours[h] for h in sorted(hours)},
                "active_hours": active,
                "first_med": round(first_med, 2), "last_med": round(last_med, 2),
                "reply_delay_med_min": round(_median(lat), 1) if lat else None,
                "msgs_per_active_day": round(total / len(by_day), 1),
                "adapt": {"wake_shift_min": int(round(wake_shift)),
                          "sleep_shift_min": int(round(sleep_shift))},
            }
    write_json(OWNER_PROFILE_FILE, prof)
    return prof


def owner_profile(now=None):
    """带缓存的画像读取：文件存在且未超过 owner_adapt.profile_refresh_hours
    （默认 6 小时）直接用，否则重算。"""
    now = now or time.time()
    oc = organic_cfg()
    adapt = oc.get("owner_adapt") or {}
    refresh_h = _f(adapt.get("profile_refresh_hours"), 6.0)
    prof = read_json(OWNER_PROFILE_FILE, {}) or {}
    if prof and now - float(prof.get("updated_at", 0)) < refresh_h * 3600:
        return prof
    return compute_owner_profile(now)


def owner_summary(now=None):
    prof = owner_profile(now)
    if not prof or prof.get("insufficient"):
        return {}
    ad = prof.get("adapt") or {}
    return {"active_hours": prof.get("active_hours", []),
            "first_med": prof.get("first_med"), "last_med": prof.get("last_med"),
            "reply_delay_med_min": prof.get("reply_delay_med_min"),
            "msgs_per_active_day": prof.get("msgs_per_active_day"),
            "wake_shift_min": ad.get("wake_shift_min", 0),
            "sleep_shift_min": ad.get("sleep_shift_min", 0)}


# ---------------- 每日生活状态 ----------------

def _event_pool(life):
    """事件池：circle 成员的日常（带人物与时段窗）+ habits 仪式 + 通用池
    + facts.life 的 small_events / occasional_events。每项
    {text, kind, who, window:[起,止]（分钟）}。"""
    pool = []
    for member in life.get("circle") or []:
        if not isinstance(member, dict):
            continue
        who = str(member.get("name") or member.get("call") or "").strip()
        join = str(member.get("join", "和"))
        for act in member.get("activities") or []:
            act = str(act).strip()
            if not act:
                continue
            text = f"{join}{who}{act}" if who else act
            if any(k in act for k in ("午饭", "午餐", "中午")):
                win = (690, 840)
            elif any(k in act for k in ("晚饭", "晚上", "夜里", "下班")):
                win = (1020, 1290)
            elif any(k in act for k in ("早上", "晨")):
                win = (420, 600)
            else:
                win = (600, 1260)
            pool.append({"text": text, "kind": "circle", "who": who,
                         "window": win})
    for habit in life.get("habits") or []:
        if not isinstance(habit, dict):
            continue
        text = str(habit.get("text") or "").strip()
        if not text:
            continue
        hint = str(habit.get("time_hint") or "any")
        win = {"morning": (390, 600), "noon": (660, 840),
               "evening": (1080, 1320)}.get(hint, (540, 1320))
        pool.append({"text": text, "kind": "habit",
                     "who": str(habit.get("label") or ""), "window": win})
    for text in GENERIC_EVENTS:
        pool.append({"text": text, "kind": "daily", "who": "",
                     "window": (570, 1290)})
    for key in ("small_events", "occasional_events"):
        for text in life.get(key) or []:
            text = str(text).strip()
            if text:
                pool.append({"text": text, "kind": "daily", "who": "",
                             "window": (540, 1290)})
    return pool


def _snap_to_active(minutes, active_hours):
    """事件分享时刻向主人活跃时段靠拢：当前整点不在活跃集时，找 ±2 小时内
    最近的活跃整点挪过去；找不到就保持原时刻（不为适配硬挪）。"""
    if not active_hours:
        return minutes
    hour = int(minutes // 60)
    if hour in active_hours:
        return minutes
    best = None
    for h in active_hours:
        cand = h * 60 + (minutes % 60)
        if abs(cand - minutes) <= 120 and (best is None
                                          or abs(cand - minutes) < abs(best - minutes)):
            best = cand
    return best if best is not None else minutes


def generate_today(now=None):
    """按日期种子采样生成今日生活状态并落盘（life_today.json）。"""
    now = now or time.time()
    local = datetime.fromtimestamp(now, tz())
    date_str = local.date().isoformat()
    life = persona_life()
    oc = organic_cfg()
    rng = random.Random(f"clawbot-life|{date_str}|{persona_name()}")
    prof = owner_profile(now)
    adapt_on = (oc.get("owner_adapt") or {}).get("enabled", True) is not False \
        and not prof.get("insufficient")
    shift_cap = _f((oc.get("owner_adapt") or {}).get("shift_max_min"), 60.0)
    ad = (prof.get("adapt") or {}) if adapt_on else {}
    wake_shift = max(-shift_cap, min(shift_cap,
                                     float(ad.get("wake_shift_min", 0) or 0)))
    sleep_shift = max(-shift_cap, min(shift_cap,
                                      float(ad.get("sleep_shift_min", 0) or 0)))

    rest_days = (life.get("routine") or {}).get("rest_days")
    if isinstance(rest_days, list) and rest_days:
        day_kind = "rest" if local.weekday() in rest_days else "work"
    else:
        day_kind = "rest" if local.weekday() >= 5 else "work"
    rt = routine_for("restday" if day_kind == "rest" else "workday", life)
    wake = _hhmm_to_min(rt.get("wake"), 450)
    sleep = _hhmm_to_min(rt.get("sleep"), 1380)
    day0 = datetime(local.year, local.month, local.day,
                    tzinfo=tz()).timestamp()

    def ts_of(minutes):
        return day0 + max(0.0, min(1439.0, minutes)) * 60

    # 日程块（给简报与生成层看「她现在大概在干嘛」）
    blocks = []
    if day_kind == "work":
        ws = _hhmm_to_min(rt.get("work_start"), 540)
        lunch = rt.get("lunch") or ["12:00", "13:00"]
        l0 = _hhmm_to_min(lunch[0], 720)
        l1 = _hhmm_to_min(lunch[1] if len(lunch) > 1 else lunch[0], 780)
        we = _hhmm_to_min(rt.get("work_end"), 1080)
        blocks = [
            {"label": "起床后的早晨", "start": _min_to_hhmm(wake),
             "end": _min_to_hhmm(ws)},
            {"label": "上午忙工作", "start": _min_to_hhmm(ws),
             "end": _min_to_hhmm(l0)},
            {"label": "午休", "start": _min_to_hhmm(l0), "end": _min_to_hhmm(l1)},
            {"label": "下午忙工作", "start": _min_to_hhmm(l1),
             "end": _min_to_hhmm(we)},
            {"label": "晚间自己的时间", "start": _min_to_hhmm(we),
             "end": _min_to_hhmm(sleep)},
        ]
    else:
        blocks = [
            {"label": "慢悠悠的上午", "start": _min_to_hhmm(wake),
             "end": "12:00"},
            {"label": "下午自由安排", "start": "12:00", "end": "18:00"},
            {"label": "晚间自己的时间", "start": "18:00",
             "end": _min_to_hhmm(sleep)},
        ]

    # 主动锚点时刻：routine 时刻 + 随机抖动（±anchor_jitter_min，默认 45）
    # + 主人作息偏移（仅早安/晚安，封顶已在上面做完）；每个锚点再掷当日
    # 跳过概率——像真人一样，不是每天每个点都会出声。
    jitter = _f(oc.get("anchor_jitter_min"), 45.0)

    def anchor(kind, intent, base_min, skip_key, skip_default, shift=0.0):
        m = base_min + rng.uniform(-jitter, jitter) + shift
        return {"id": f"{date_str}:{kind}", "kind": kind, "intent": intent,
                "time": _min_to_hhmm(m), "ts": ts_of(m),
                "skip": rng.random() < _f(oc.get(skip_key), skip_default)}

    moments = [anchor("wakeup", "morning", wake + 30,
                      "wakeup_skip_p", 0.15, wake_shift)]
    if day_kind == "work":
        lunch = rt.get("lunch") or ["12:00", "13:00"]
        moments.append(anchor("lunch_break", "lunch",
                              _hhmm_to_min(lunch[0], 720) + 15,
                              "lunch_skip_p", 0.35))
        moments.append(anchor("after_work", "evening",
                              _hhmm_to_min(rt.get("work_end"), 1080) + 20,
                              "afterwork_skip_p", 0.20))
    else:
        moments.append(anchor("afternoon", "evening", 930,
                              "afterwork_skip_p", 0.20))
    moments.append(anchor("bedtime", "goodnight", sleep - 25,
                          "bedtime_skip_p", 0.10, sleep_shift))
    moments.sort(key=lambda m: m["ts"])

    if day_kind == "work":
        busy = rng.choices(["low", "mid", "high"],
                           weights=[0.2, 0.55, 0.25])[0]
    else:
        busy = rng.choices(["low", "mid"], weights=[0.7, 0.3])[0]
    moods = life.get("moods") or {}
    mood_pool = moods.get(busy) or DEFAULT_MOODS.get(busy) or ["平静"]
    mood = rng.choice([str(m) for m in mood_pool]) if mood_pool else "平静"

    # 生活事件：池中不重复抽 1-3 件，各配分享时刻（事件时段窗内采样，再
    # 向主人活跃时段轻微吸附）与权重
    pool = _event_pool(life)
    rng.shuffle(pool)
    n_events = rng.choices([1, 2, 3], weights=[0.3, 0.45, 0.25])[0]
    active_hours = prof.get("active_hours") or []
    events = []
    for i, ev in enumerate(pool[:n_events]):
        lo, hi = ev["window"]
        share_min = rng.uniform(lo, max(lo + 30, hi - 30))
        share_min = _snap_to_active(share_min, active_hours)
        events.append({"id": f"{date_str}:evt{i}", "text": ev["text"],
                       "kind": ev["kind"], "who": ev["who"],
                       "share_at": ts_of(share_min),
                       "time": _min_to_hhmm(share_min),
                       "weight": round(rng.uniform(0.5, 1.0), 2)})
    events.sort(key=lambda e: e["share_at"])

    thr = oc.get("silence_threshold_h") or [2.5, 6.0]
    try:
        thr_lo, thr_hi = float(thr[0]), float(thr[1])
    except (TypeError, ValueError, IndexError):
        thr_lo, thr_hi = 2.5, 6.0
    plan = {"date": date_str, "day_kind": day_kind, "generated_at": now,
            "schedule": blocks, "moments": moments, "events": events,
            "mood": mood, "busy_level": busy,
            "silence_threshold_h": round(rng.uniform(thr_lo, thr_hi), 2),
            "owner_adapt": {"applied": bool(adapt_on and (wake_shift or sleep_shift)),
                            "wake_shift_min": int(round(wake_shift)),
                            "sleep_shift_min": int(round(sleep_shift))}}
    write_json(LIFE_TODAY_FILE, plan)
    return plan


def ensure_today(now=None):
    """今日生活状态：文件日期对得上直接用，否则重新采样生成。"""
    now = now or time.time()
    today = datetime.fromtimestamp(now, tz()).date().isoformat()
    plan = read_json(LIFE_TODAY_FILE, {}) or {}
    if plan.get("date") == today and plan.get("moments") is not None:
        return plan
    return generate_today(now)


def current_block(plan, now=None):
    now = now or time.time()
    local = datetime.fromtimestamp(now, tz())
    cur = local.hour * 60 + local.minute
    for b in plan.get("schedule") or []:
        if _hhmm_to_min(b.get("start"), 0) <= cur < _hhmm_to_min(b.get("end"), 1439):
            return b.get("label", "")
    return ""


def brief_summary(now=None):
    """给 worker 简报用的压缩摘要（控制 token 量级）：心情、当前日程块、
    下一个锚点、今日事件（已到点/未到点分开，只给文本）。"""
    now = now or time.time()
    plan = ensure_today(now)
    nxt = None
    for m in plan.get("moments") or []:
        if not m.get("skip") and float(m.get("ts", 0)) > now:
            nxt = {"kind": m.get("kind"), "time": m.get("time")}
            break
    evs = plan.get("events") or []
    return {"date": plan.get("date"), "day_kind": plan.get("day_kind"),
            "mood": plan.get("mood"), "busy_level": plan.get("busy_level"),
            "now_block": current_block(plan, now),
            "next_moment": nxt,
            "events_shared_or_due": [e.get("text") for e in evs
                                     if float(e.get("share_at", 0)) <= now],
            "events_later": [e.get("text") for e in evs
                             if float(e.get("share_at", 0)) > now],
            "owner_adapt": plan.get("owner_adapt") or {}}


def main(argv=None):
    p = argparse.ArgumentParser(prog="life.py")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("today")
    sub.add_parser("ensure")
    sub.add_parser("owner")
    sub.add_parser("summary")
    args = p.parse_args(argv)
    if args.cmd in ("today", "ensure"):
        plan = ensure_today() if args.cmd == "ensure" else generate_today()
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if args.cmd == "owner":
        print(json.dumps(owner_profile(), ensure_ascii=False))
        return 0
    if args.cmd == "summary":
        print(json.dumps({"life_today": brief_summary(),
                          "owner_profile": owner_summary()},
                         ensure_ascii=False))
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
