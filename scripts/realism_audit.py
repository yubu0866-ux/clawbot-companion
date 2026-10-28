#!/usr/bin/env python3
# realism_audit.py — 聊天真人感 12 项指标审计（可复跑、只出数值）
#
# 样本 = --since（默认近 7 天）以来的真实回复轮（state/
# reply_records.jsonl 中 skipped=false、proactive=false 的条目）
# 与同窗 state/history.jsonl 里她的回复气泡（kind != proactive）。
# 只统计、不回显聊天正文：输出仅指标数值与计数。
#
# 指标定义（词表与阈值是按一种中文日常语域调的默认口径，部署者
# 应按自家人设调整 TARGETS 与各词表后再当回归线用）：
#  1 bracket_top1_share  方括号表情 Top1 占方括号表情总量比（目标 ≤0.25）
#  2 emoji_top1_share    真 emoji Top1 占比（目标 ≤0.40）
#  3 stance_rate         表态轮次率（目标 0.08–0.15）
#  4 people_per20        核心人际（facts.json 的 life.people）每 20
#                        轮提及次数（目标 ≥1）
#  5 recall_per10        显性回指每 10 轮次数（目标 ≥1）
#  6 unfinished_follow   未闭环 24h 跟进率（关键词重叠启发式，目标 ≥0.60）
#  7 twostage_per_day    两段式触发次数/活跃日（目标 0.5–2；由 history
#                        气泡间隔启发式识别：同轮两组气泡间隔 120–1800 秒
#                        且中间无他的消息、第二组非 proactive）
#  8 proactive_slot_share 主动消息中时段档/锚点类 intent 占比（目标
#                        ≥0.40；数据源 proactive.json 的 log 滚动窗）
#  9 question_rate       问句轮次率（目标 0.05–0.12）
# 10 opener_top3_share   气泡 opener Top3 占比（目标 ≤0.15）
# 11 length_cv           气泡字数变异系数（目标 ≥0.55；口径与
#                        worker.rhythm_recent 一致：标准差/均值）
# 12 template_hits       模板腔（共情模板/客服腔/说教）总命中（目标 0）
#
# 用法：python3 scripts/realism_audit.py [--since ISO] [--record] [--json]
# --record 把本次结果追加到 state/realism_audit.jsonl；对照基线取
# 该文件中的第一条记录（首次 --record 即基线）。典型用法是每周
# 定时跑一次 --record，正常静默、出现回归（未达标项变多）时报告。
# 退出码恒 0：这是报告工具，不是门禁。

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("COMPANION_HOME") or os.path.dirname(HERE)
STATE = os.path.join(ROOT, "state")

TARGETS = {
    "bracket_top1_share": ("<=", 0.25), "emoji_top1_share": ("<=", 0.40),
    "stance_rate": ("range", (0.08, 0.15)), "people_per20": (">=", 1.0),
    "recall_per10": (">=", 1.0), "unfinished_follow": (">=", 0.60),
    "twostage_per_day": ("range", (0.5, 2.0)),
    "proactive_slot_share": (">=", 0.40),
    "question_rate": ("range", (0.05, 0.12)),
    "opener_top3_share": ("<=", 0.15), "length_cv": (">=", 0.55),
    "template_hits": ("==", 0),
}

BRACKET_RE = re.compile(r"\[[^\[\]\s]{1,6}\]")
BRACKET_PLACEHOLDERS = {"[图片]", "[视频]", "[语音]", "[文件]"}
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF️]")
STANCE_MARKERS = ("想吃", "爱吃", "喜欢吃", "不吃", "我喜欢", "我不喜欢",
                  "我讨厌", "我想去", "我想看", "我觉得", "受不了",
                  "最烦", "超爱", "偏爱", "就爱", "不想吃", "不想去")
RECALL_MARKERS = ("你上次", "你之前说", "你说的", "还记得", "你提过",
                  "上次你", "你不是说", "你昨晚说", "你昨天")
TEMPLATE_WORDS = (
    "理解你", "辛苦了", "心疼你", "别难过", "会好的", "放轻松",
    "请问", "为您", "建议您", "这边建议", "竭诚",
    "你应该", "要注意", "多喝水", "少熬夜")
TOPIC_WORDS = ("妈妈", "电影", "火锅", "奶茶", "游乐园",
               "约会", "吃饭", "加班", "出差", "生日", "馄饨")
SLOT_INTENTS = {"morning", "lunch", "evening", "goodnight",
                "wakeup", "afterwork", "bedtime", "anchor"}


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def local_tz():
    """时区取 config.json 的 tz；缺失/非法时用系统本地时区。"""
    name = (read_json(os.path.join(ROOT, "config.json"), {}) or {}).get("tz")
    if name:
        try:
            return ZoneInfo(str(name))
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def people_names():
    """核心人际名单：facts.json 的 life.people（字符串直接用；
    dict 取 name/title 字段）。"""
    facts = read_json(os.path.join(ROOT, "facts.json"), {}) or {}
    raw = ((facts.get("life") or {}).get("people")) or []
    names = []
    for p in raw:
        if isinstance(p, str) and p.strip():
            names.append(p.strip())
        elif isinstance(p, dict):
            for k in ("name", "title", "who"):
                if p.get(k):
                    names.append(str(p[k]))
                    break
    return names


def opener_of(text):
    t = text.strip()
    m = re.split(r"[，。！？、…\n]", t, maxsplit=1)
    head = m[0] if m else t
    return head[:4] if head else t[:4]


def keywords_of(text, people):
    """未闭环话题的关键词：2–4 字中文片段 + 已知实体，粗粒度即可。"""
    kws = set()
    for name in list(people) + list(TOPIC_WORDS):
        if name in text:
            kws.add(name)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,4}", text):
        kws.add(m.group(0))
    return kws


def main():
    tz = local_tz()
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="ISO 时间；默认近 7 天")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()
    if args.since is None:
        args.since = (datetime.now(tz) - timedelta(days=7)).isoformat()
    since_dt = datetime.fromisoformat(args.since)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=tz)
    since = since_dt.timestamp()
    people = people_names()

    hist = [h for h in read_jsonl(os.path.join(STATE, "history.jsonl"))
            if float(h.get("ts", 0)) >= since]
    recs = [r for r in read_jsonl(os.path.join(STATE, "reply_records.jsonl"))
            if float(r.get("ts", 0)) >= since
            and not r.get("skipped") and not r.get("proactive")]
    bubbles = [str(h.get("text", "")) for h in hist
               if h.get("role") == "assistant"
               and h.get("kind") != "proactive"]
    n_rounds = len(recs)
    n_bubbles = len(bubbles)
    joined_rounds = [str(r.get("reply", "")) for r in recs]

    # 1–2 表情单点化
    brackets = Counter()
    for b in bubbles:
        for m in BRACKET_RE.findall(b):
            if m not in BRACKET_PLACEHOLDERS:
                brackets[m] += 1
    b_total = sum(brackets.values())
    bracket_top1 = (brackets.most_common(1)[0][1] / b_total) if b_total else None
    emojis = Counter()
    for b in bubbles:
        for ch in EMOJI_RE.findall(b):
            emojis[ch] += 1
    e_total = sum(emojis.values())
    emoji_top1 = (emojis.most_common(1)[0][1] / e_total) if e_total else None

    # 3 表态率
    stance = sum(1 for t in joined_rounds
                 if any(m in t for m in STANCE_MARKERS))
    stance_rate = (stance / n_rounds) if n_rounds else None

    # 4 人际提及
    people_hits = sum(1 for t in joined_rounds
                      if any(p in t for p in people))
    people_per20 = (people_hits / n_rounds * 20) if n_rounds else None

    # 5 显性回指
    recall_hits = sum(1 for t in joined_rounds
                      if any(m in t for m in RECALL_MARKERS))
    recall_per10 = (recall_hits / n_rounds * 10) if n_rounds else None

    # 6 未闭环 24h 跟进（启发式）
    sums = read_jsonl(os.path.join(STATE, "summaries.jsonl"))
    her_msgs = [(float(h.get("ts", 0)), str(h.get("text", "")))
                for h in read_jsonl(os.path.join(STATE, "history.jsonl"))
                if h.get("role") == "assistant"]
    un_total = un_followed = 0
    for s in sums:
        un = str(s.get("unfinished", "")).strip()
        if not un:
            continue
        try:
            d = datetime.strptime(str(s.get("date", "")),
                                  "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            continue
        if d.timestamp() < since - 7 * 86400:
            continue
        un_total += 1
        kws = keywords_of(un, people)
        win = [t for ts, t in her_msgs
               if d.timestamp() <= ts <= d.timestamp() + 86400 + 43200]
        if kws and any(any(k in t for k in kws) for t in win):
            un_followed += 1
    unfinished_follow = (un_followed / un_total) if un_total else None

    # 7 两段式（启发式）
    seq = [h for h in read_jsonl(os.path.join(STATE, "history.jsonl"))
           if float(h.get("ts", 0)) >= since]
    seq.sort(key=lambda h: float(h.get("ts", 0)))
    two_events = 0
    active_days = set()
    for r in recs:
        active_days.add(datetime.fromtimestamp(float(r["ts"]), tz)
                        .strftime("%Y-%m-%d"))
    prev = None
    for h in seq:
        if h.get("role") == "user":
            prev = None
            continue
        if h.get("role") == "assistant" and h.get("kind") != "proactive":
            if prev is not None:
                gap = float(h.get("ts", 0)) - float(prev.get("ts", 0))
                if 120 <= gap <= 1800:
                    two_events += 1
            prev = h
    twostage_per_day = (two_events / len(active_days)) if active_days else None

    # 8 主动时段档占比（proactive.json log 滚动窗）
    pj = read_json(os.path.join(STATE, "proactive.json"), {}) or {}
    plog = [e for e in pj.get("log", []) if float(e.get("ts", 0)) >= since]
    slot_n = sum(1 for e in plog if str(e.get("intent", "")) in SLOT_INTENTS)
    slot_share = (slot_n / len(plog)) if plog else None

    # 9 问句率
    q_rounds = sum(1 for t in joined_rounds if ("？" in t or "?" in t))
    question_rate = (q_rounds / n_rounds) if n_rounds else None

    # 10 opener Top3
    openers = Counter(opener_of(b) for b in bubbles if b.strip())
    o_total = sum(openers.values())
    opener_top3 = (sum(c for _, c in openers.most_common(3)) / o_total) \
        if o_total else None

    # 11 句长变异系数
    lens = [len(b) for b in bubbles if b.strip()]
    if len(lens) >= 2:
        mean = sum(lens) / len(lens)
        var = sum((x - mean) ** 2 for x in lens) / len(lens)
        length_cv = (math.sqrt(var) / mean) if mean else None
    else:
        length_cv = None

    # 12 模板腔
    template_hits = sum(b.count(w) for b in bubbles for w in TEMPLATE_WORDS)

    metrics = {
        "bracket_top1_share": bracket_top1,
        "emoji_top1_share": emoji_top1,
        "stance_rate": stance_rate,
        "people_per20": people_per20,
        "recall_per10": recall_per10,
        "unfinished_follow": unfinished_follow,
        "twostage_per_day": twostage_per_day,
        "proactive_slot_share": slot_share,
        "question_rate": question_rate,
        "opener_top3_share": opener_top3,
        "length_cv": length_cv,
        "template_hits": template_hits,
    }

    def verdict(key, val):
        if val is None:
            return "无数据"
        op, tgt = TARGETS[key]
        if op == "<=":
            return "达标" if val <= tgt else "未达标"
        if op == ">=":
            return "达标" if val >= tgt else "未达标"
        if op == "==":
            return "达标" if val == tgt else "未达标"
        lo, hi = tgt
        return "达标" if lo <= val <= hi else "未达标"

    audit_file = os.path.join(STATE, "realism_audit.jsonl")
    recorded = read_jsonl(audit_file)
    baseline = (recorded[0].get("metrics") if recorded else None)

    result = {
        "ts": datetime.now(tz).isoformat(),
        "since": args.since,
        "n_rounds": n_rounds,
        "n_bubbles": n_bubbles,
        "n_proactive_log": len(plog),
        "bracket_detail": {"total": b_total,
                           "top": brackets.most_common(3)},
        "emoji_detail": {"total": e_total,
                         "top": emojis.most_common(3)},
        "counts": {"stance_rounds": stance, "people_rounds": people_hits,
                   "recall_rounds": recall_hits,
                   "unfinished_total": un_total,
                   "unfinished_followed": un_followed,
                   "two_stage_events": two_events,
                   "active_days": len(active_days),
                   "question_rounds": q_rounds},
        "metrics": metrics,
        "baseline_first_record": baseline,
        "verdicts": {k: verdict(k, v) for k, v in metrics.items()},
    }
    if args.record:
        os.makedirs(STATE, exist_ok=True)
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"样本：回复轮 n={n_rounds}，气泡 {n_bubbles}，"
              f"主动 log {len(plog)} 条（since {args.since}）")
        for k, v in metrics.items():
            base = (baseline or {}).get(k)
            vs = "—" if v is None else (
                f"{v:.3f}" if isinstance(v, float) else str(v))
            bs = "—" if base is None else (
                f"{base:.3f}" if isinstance(base, float) else str(base))
            print(f"  {k:24s} 今 {vs:>7s}  基线 {bs:>7s}  "
                  f"{result['verdicts'][k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
