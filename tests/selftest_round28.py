#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round28 selftest（通用版）：2026-10-28 同步批的回归锁——情景
记忆 episodes、真人感简报供给、学习环（领取锚点打分/判重/
learn-report）、两段式概率与审计、intent 格式豁免、processed
熔断、损坏隔离唯一命名、fsync 对齐、入站媒体完整性、脚本工具。

沙箱纪律与既有套件一致：模块副本在临时 COMPANION_HOME 内加载、
合成 state 落临时目录、自清理；不触碰真实数据目录。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"（{detail}）" if detail and not cond else ""))


TMP = tempfile.mkdtemp(prefix="ccr28-test-")
for d in ("state", "inbox", "outbox/pending", "outbox/sent",
          "outbox/failed", "state/media_in"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
    CFG = json.load(f)
CFG["llm"]["mode"] = "api"
with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)
shutil.copy(os.path.join(ROOT, "persona.example.json"),
            os.path.join(TMP, "persona.json"))
shutil.copy(os.path.join(ROOT, "facts.example.json"),
            os.path.join(TMP, "facts.json"))
shutil.copytree(os.path.join(ROOT, "content"), os.path.join(TMP, "content"))
os.environ["COMPANION_HOME"] = TMP


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


B = _load("bridge_r28", "bridge.py")
W = _load("worker_r28", "worker.py")
WB = W.bridge  # worker 实际加载的 bridge 实例（配置缓存与 B 独立，改配置走它）
T = W.tune
STATE = os.path.join(TMP, "state")


def wj(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def rj(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


# ---------------- E：情景记忆 episodes ----------------
r = W.add_episode("shared_experience", "一起去江边看了日落，还吃了烧烤")
check("E1 episode 落账", r.get("added") is True
      and len(W.load_episodes()) == 1
      and W.load_episodes()[0]["kind"] == "shared_experience")
r2 = W.add_episode("shared_experience", "一起去江边看了日落，还吃了烧烤")
check("E2 规范化互含判重", r2.get("added") is False
      and "重复" in r2.get("reason", ""))
r3 = W.add_episode("his_detail", "他最近迷上了做手冲咖啡每天早上都要自己磨豆子冲一杯")
W.add_episode("his_detail", "他最近迷上了做手冲咖啡每天早上都要自己磨豆子泡一杯")
check("E3 近似条目相似度判重", len(W.load_episodes()) == 2,
      f"n={len(W.load_episodes())}")
# 与 facts 判重：facts.example 的 user_facts/facts 中找一条够长的
_facts = rj(os.path.join(TMP, "facts.json"), {}) or {}
_cands = [e for e in (_facts.get("facts") or []) + (_facts.get("user_facts") or [])]
_long = ""
for _e in _cands:
    _t = _e.get("fact", "") if isinstance(_e, dict) else str(_e)
    if len(_t) >= 8:
        _long = _t
        break
if _long:
    _before = len(W.load_episodes())
    # episode 侧 facts 读的是 state/facts.json（FACTS_FILE）：把同一
    # 条目放进 state/facts.json 后应判重
    wj(os.path.join(STATE, "facts.json"), {"facts": [_long], "user_facts": []})
    r4 = W.add_episode("his_detail", _long)
    check("E4 与 facts 档案判重", r4.get("added") is False
          and len(W.load_episodes()) == _before)
else:
    check("E4 与 facts 档案判重", False, "示例 facts 无长条目可用")
r5 = W.add_episode("his_detail", "他的银行卡密码是 123456")
check("E5 敏感内容拒落账", r5.get("added") is False
      and "敏感" in r5.get("reason", ""))
r6 = W.add_episode("bogus_kind", "他最近在学吉他")
check("E6 kind 白名单兜底", r6.get("added") is True
      and W.load_episodes()[-1]["kind"] == "his_detail")
# 上限归档：cap=3 时第 4 条普通条目归档最旧普通条目、约定不归档
WB.CFG["worker"]["episodes_cap"] = 3
W.add_episode("promise", "答应周末一起去看展览")
W.add_episode("shared_experience", "一起拼完了一幅拼图")
_act = [e for e in W.load_episodes() if e.get("status") == "active"]
_arc = [e for e in W.load_episodes() if e.get("status") == "archived"]
check("E7 上限归档最旧普通条目、约定不归档",
      len(_act) == 3 and len(_arc) >= 1
      and all(e["kind"] != "promise" for e in _arc)
      and any(e["kind"] == "promise" for e in _act),
      f"active={len(_act)} archived={len(_arc)}")
WB.CFG["worker"]["episodes_cap"] = 150
_today = W.today_local().date().isoformat()
W.add_episode("anniversary", "下周的纪念日要一起吃饭", date=_today)
_br = W.episodes_brief()
check("E8 简报回提：recent 封顶 + upcoming 窗口内带出",
      len(_br["recent"]) <= 3 and len(_br["upcoming"]) <= 2
      and any(u["kind"] == "anniversary" for u in _br["upcoming"]))

# ---------------- B：真人感简报供给 ----------------
_records = os.path.join(STATE, "reply_records.jsonl")
_now = time.time()
for i in range(6):
    append_jsonl(_records, {"ts": _now - 60 + i, "batch_ids": [f"B{i}"],
                            "reply": "哈哈，好呀 我想吃火锅",
                            "intent": "chat", "skipped": False,
                            "proactive": False})
_op = W.opener_recent()
check("B1 opener 计数与冷却标记",
      _op and _op[0]["count"] == 6 and _op[0]["cooling"] is True)
_rh = W.rhythm_recent()
check("B2 句长整齐时 rhythm 给长短交错 hint",
      _rh["cv"] is not None and _rh["cv"] < 0.4 and "长短交错" in _rh["hint"],
      f"cv={_rh['cv']}")
for i in range(6):
    append_jsonl(_records, {"ts": _now + i, "batch_ids": [f"C{i}"],
                            "reply": "好 我先洗个手马上就来啦你等我一下下哦",
                            "intent": "chat", "skipped": False,
                            "proactive": False})
_rh2 = W.rhythm_recent()
check("B2b 句长交错后 CV 抬升", _rh2["cv"] is not None
      and _rh2["cv"] > _rh["cv"], f"cv2={_rh2['cv']}")
_st = W.stance_today()
check("B3 stance_today 计数（今日轮次与表态轮次）",
      _st["rounds_today"] >= 12 and _st["stance_rounds_today"] >= 6,
      f"{_st}")
_rb = W.round_brief()
check("B4 round_brief 新键齐全",
      all(k in _rb for k in ("opener_recent", "rhythm_recent",
                             "stance_today", "episodes", "topic_pref")))

# ---------------- S：学习环 ----------------
append_jsonl(os.path.join(STATE, "history.jsonl"),
             {"ts": _now, "role": "user", "text": "在干嘛呢今天过得怎么样",
              "kind": "reply", "message_id": "HU1"})
_batch = [{"message_id": "S100", "text": "哈哈哈太好笑了",
           "create_time_ms": int((_now + 30) * 1000), "type": "text"}]
_fb1 = W.score_batch_feedback(_batch)
_fbfile = os.path.join(STATE, "feedback.jsonl")
_sigfile = os.path.join(STATE, "signals.jsonl")
check("S1 统一入口打分落盘（feedback 带 batch_key + signals 一行）",
      _fb1 is not None and len(read_jsonl(_fbfile)) == 1
      and read_jsonl(_fbfile)[0].get("batch_key") == "S100"
      and len(read_jsonl(_sigfile)) == 1
      and read_jsonl(_sigfile)[0].get("laughed") is True)
_fb2 = W.score_batch_feedback(_batch)
check("S2 同批重复打分判重（feedback/signals 不双计）",
      len(read_jsonl(_fbfile)) == 1 and len(read_jsonl(_sigfile)) == 1
      and (_fb2 or {}).get("dedup") is True)
append_jsonl(_records, {"ts": _now + 60, "batch_ids": ["S100"],
                        "reply": "好呀，慢慢说", "intent": "chat",
                        "skipped": False, "proactive": False})
_batch2 = [{"message_id": "S101", "text": "嗯嗯好",
            "create_time_ms": int((_now + 90) * 1000), "type": "text"}]
W.score_batch_feedback(_batch2)
check("S3 异批照常落盘", len(read_jsonl(_fbfile)) == 2
      and len(read_jsonl(_sigfile)) == 2)
_tp = W.topic_pref_summary()
check("S4 topic_pref 聚合", _tp["n_signals"] == 2
      and _tp["by_intent"].get("chat", {}).get("n") == 2)
_rep = W.learn_report()
check("S5 learn-report 结构（逐旋钮 promote 评估 + 反馈统计）",
      _rep["feedback_total"] == 2 and len(_rep["promote_watch"])
      == len(T.DEFAULT_KNOBS) and "topic_pref" in _rep)
# 领取锚点：pending 首领即打分、重复领取不双计、exclude 复查不打分
wj(os.path.join(TMP, "inbox", "S200.json"),
   {"message_id": "S200", "from_user_id": "u@im.wechat",
    "context_token": "CTX", "create_time_ms": int((_now + 120) * 1000),
    "type": "text", "text": "哈哈好的", "ref_text": "", "recalled": False})
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    W.cmd_pending()
_n_fb = len(read_jsonl(_fbfile))
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    W.cmd_pending()
check("S6 领取锚点打分一次、重复领取不双计",
      _n_fb == 3 and len(read_jsonl(_fbfile)) == 3,
      f"fb={len(read_jsonl(_fbfile))}")
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    W.cmd_pending(exclude=["S200"])
check("S6b exclude 复查不打分", len(read_jsonl(_fbfile)) == 3)
# 两段式：概率 0.25 + 审计日志 + 每日上限
wj(os.path.join(STATE, "life.json"),
   {"date": _today, "schedule": "", "busy_level": "high"})
_d1 = W.two_stage_decision("chat", roll=0.20)
_d2 = W.two_stage_decision("chat", roll=0.30)
check("S7 两段式新概率（0.20 触发 / 0.30 不触发且 reason 标 0.25）",
      _d1.get("trigger") is True and _d2.get("trigger") is False
      and "0.25" in _d2.get("reason", ""), f"{_d1} / {_d2}")
_d3 = W.two_stage_decision("chat", roll=0.10)
_d4 = W.two_stage_decision("chat", roll=0.10)
_tlog = read_jsonl(os.path.join(STATE, "two_stage_log.jsonl"))
check("S7b 两段式每日 ≤2 + 每次判定落审计",
      _d3.get("trigger") is True and _d4.get("trigger") is False
      and len(_tlog) == 4, f"log={len(_tlog)}")

# ---------------- F：intent 格式豁免 ----------------
WB.CFG["limits"]["intent_overrides"] = {
    "playful": {"max_bubbles": 5, "max_chars_per_bubble": 150,
                "max_chars_per_reply": 600, "mirror_exempt": True,
                "flat_len_score": True}}
_ctx = {"cfg": WB.cfg(), "intent": "playful", "batch_text": "嗯",
        "batch_question": False, "answered": True, "persona": "",
        "recent_replies": [], "facts": [], "target_len": 10,
        "crisis": False, "cooling": [], "recent_praise": 0, "knobs": {}}
_long_bubbles = ["长" * 100, "二", "三", "四"]
_v_ov = W.check_candidate(_long_bubbles, _ctx)
check("F1 豁免 intent：条数/单条/整轮/镜像四项放行",
      not any(s in " ".join(_v_ov) for s in
              ("条数超限", "单条超过", "总字数超限", "镜像")), f"{_v_ov}")
_ctx2 = dict(_ctx, intent="chat")
_v_no = W.check_candidate(_long_bubbles, _ctx2)
check("F1b 无豁免 intent：同候选被常态上限拦",
      any("条数超限" in v for v in _v_no)
      and any("单条超过" in v for v in _v_no), f"{_v_no}")
_s_ov = W.score_candidate(["长" * 100], dict(_ctx, target_len=30))
_s_no = W.score_candidate(["长" * 100], dict(_ctx2, target_len=30))
check("F2 豁免 intent 长度分平坦（不按 target 罚分）",
      _s_ov > _s_no, f"ov={_s_ov} no={_s_no}")
_batch_w = [{"message_id": "W900", "from_user_id": "u@im.wechat",
             "context_token": "CTX", "create_time_ms": int(_now * 1000),
             "type": "text", "text": "hi"}]
_ok_w = W.write_outbox(_batch_w, ["一", "二", "三", "四", "五"], 0, 0,
                       intent="playful")
_pend = sorted(os.listdir(os.path.join(TMP, "outbox", "pending")))
_items = [rj(os.path.join(TMP, "outbox", "pending", fn), {})
          for fn in _pend if fn.startswith("W900-")]
check("F3 write_outbox：豁免上限截断 + intent 落盘",
      _ok_w and len(_items) == 5
      and all(i.get("intent") == "playful" for i in _items),
      f"n={len(_items)}")
WB.CFG["limits"]["intent_overrides"] = {}
_pack = W.build_pack()
check("F4 build_pack 带 episodes 与 scene_look",
      "episodes" in _pack and "scene_look" in _pack)
wj(os.path.join(TMP, "content", "scene_looks.json"),
   {"居家": ["素颜", "丸子头"]})
_pack2 = W.build_pack()
check("F4b scene_look 接线：内容包场景进 pack",
      _pack2["scene_look"].get("居家", {}).get("makeup_level") == "素颜",
      f"{list(_pack2['scene_look'])}")

# ---------------- R：可靠性硬化 ----------------
# processed 熔断 + 旁证自愈：35 条未处理、其中 5 条已有答复旁证
for i in range(35):
    wj(os.path.join(TMP, "inbox", f"P{i:02d}.json"),
       {"message_id": f"P{i:02d}", "from_user_id": "u@im.wechat",
        "context_token": "CTX",
        "create_time_ms": int((_now + 300 + i) * 1000),
        "type": "text", "text": f"消息{i}", "ref_text": "",
        "recalled": False})
for i in range(5):
    append_jsonl(os.path.join(STATE, "history.jsonl"),
                 {"ts": _now + 300 + i, "role": "assistant",
                  "text": "已回复", "kind": "reply",
                  "message_id": f"P{i:02d}"})
_msgs = W.collect_unprocessed()
_proc = W.load_processed()
check("R1 processed 熔断：旁证 5 条自愈回填、积压只放行最新 25 条",
      len(_msgs) == 25
      and all(f"P{i:02d}" in _proc for i in range(5))
      and all(f"P{i:02d}" in _proc for i in range(5, 10))
      and str(_msgs[0]["message_id"]) == "P10",
      f"n={len(_msgs)} proc={len(_proc)}")
for i in range(35):
    try:
        os.remove(os.path.join(TMP, "inbox", f"P{i:02d}.json"))
    except OSError:
        pass
# 损坏隔离同秒唯一命名（冻结 time.time 构造同秒两次损坏）
_bad = os.path.join(STATE, "broken.json")
with open(_bad, "w", encoding="utf-8") as f:
    f.write("{not json")
with mock.patch("time.time", return_value=1700000000.0):
    _v1 = B.read_json(_bad, {"d": 1})
    with open(_bad, "w", encoding="utf-8") as f:
        f.write("{still not json")
    _v2 = B.read_json(_bad, {"d": 1})
_corrupts = sorted(fn for fn in os.listdir(STATE)
                   if fn.startswith("broken.json.corrupt-"))
check("R2 损坏留痕同秒唯一（两份证据并存、不覆盖）",
      _v1 == {"d": 1} and _v2 == {"d": 1} and len(_corrupts) == 2,
      f"{_corrupts}")
# outbox 三目录豁免：损坏条目不留痕（死信契约优先）
_bad2 = os.path.join(TMP, "outbox", "pending", "X1-1.json")
with open(_bad2, "w", encoding="utf-8") as f:
    f.write("{broken")
_v3 = B.read_json(_bad2, None)
check("R3 outbox 路径损坏豁免留痕",
      _v3 is None and not any("X1-1" in fn and "corrupt" in fn
                              for fn in os.listdir(os.path.join(
                                  TMP, "outbox", "pending"))))
# append_jsonl 半行修复：文件尾无换行时先补行再追加
_half = os.path.join(STATE, "half.jsonl")
with open(_half, "w", encoding="utf-8") as f:
    f.write('{"a": 1}')
B.append_jsonl(_half, {"b": 2})
_rows = read_jsonl(_half)
check("R4 append_jsonl 半行补换行", _rows == [{"a": 1}, {"b": 2}],
      f"{_rows}")
# _prune_history fsync 对齐：旧行删、新行留、fsync ≥2、无 tmp 残留
_hist = os.path.join(STATE, "history.jsonl")
append_jsonl(_hist, {"ts": _now - 40 * 86400, "role": "user",
                     "text": "四十天前的旧消息", "kind": "reply",
                     "message_id": "OLD1"})
_real_fsync = os.fsync
_fsync_n = {"n": 0}


def _counting_fsync(fd):
    _fsync_n["n"] += 1
    return _real_fsync(fd)


with mock.patch("os.fsync", _counting_fsync):
    B._prune_history()
_hrows = read_jsonl(_hist)
# fsync 次数按平台分流：文件 fsync 两平台都有；目录 fsync 是
# POSIX 概念（bridge._fsync_dir 在无 O_DIRECTORY 的 Windows 上
# 按设计跳过），故 Windows 期望 ≥1、POSIX 期望 ≥2。
check("R5 _prune_history：旧行修剪 + 文件/目录 fsync + 无 tmp 残留",
      all(r.get("message_id") != "OLD1" for r in _hrows)
      and _fsync_n["n"] >= (1 if os.name == "nt" else 2)
      and not os.path.exists(_hist + ".tmp"),
      f"fsync={_fsync_n['n']}")
_low, _free = B.disk_space_low(STATE, warn_bytes=10 ** 18)
_low2, _ = B.disk_space_low(STATE, warn_bytes=1)
check("R6 磁盘低位告警纯查询", _low is True and _low2 is False)
# 入站媒体完整性校验
_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 8 + b"\xff\xd9"
_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IEND\xaeB\x60\x82"
_webp_payload = b"WEBP" + b"\x00" * 6
_webp = b"RIFF" + len(_webp_payload).to_bytes(4, "little") \
    + _webp_payload
check("R7 媒体完整性：JPEG/PNG/WebP 完整件通过、截断件拒绝",
      B._image_integrity_ok(_jpg, "jpg") is True
      and B._image_integrity_ok(_jpg[:-2], "jpg") is False
      and B._image_integrity_ok(_png, "png") is True
      and B._image_integrity_ok(_webp, "webp") is True
      and B._image_integrity_ok(_webp[:-4], "webp") is False)
check("R8 STT 配置与解释器解析",
      B._stt_setting("model", "base") == "base"
      and B._stt_setting("language", "zh") == "zh")
os.environ["COMPANION_STT_PYTHON"] = sys.executable
check("R8b COMPANION_STT_PYTHON 优先", B._resolve_stt_python()
      == sys.executable)
del os.environ["COMPANION_STT_PYTHON"]

# R9/R10 claim 释放契约：inbox 落盘后经 on_stored 立即释放（exists
# 接力去重），落盘前 OSError 则 finally 释放且回报 False 等重投——
# 防 claim 在 body 长尾（多次 fsync 写）期间滞留 inbox 的回归。
def _r9_body(session, msg, my_id, state, mid, path, on_stored=None):
    _claim = os.path.join(B.INBOX, mid + ".claim")
    check("R9a body 执行期间 claim 占位存在", os.path.exists(_claim))
    on_stored()
    check("R9b on_stored 后 claim 立即释放", not os.path.exists(_claim))


_orig_body = B._process_inbound_body
try:
    B._process_inbound_body = _r9_body
    _ok_r9 = B._process_inbound({}, {"message_id": "R9M", "message_type": 1,
                                     "from_user_id": "u1"}, "u1")
finally:
    B._process_inbound_body = _orig_body
check("R9c 正常处理返回 True 且无 claim 残留",
      _ok_r9 is True
      and not os.path.exists(os.path.join(B.INBOX, "R9M.claim")))


def _r10_body(session, msg, my_id, state, mid, path, on_stored=None):
    raise OSError("ENOSPC")


try:
    B._process_inbound_body = _r10_body
    _ok_r10 = B._process_inbound({}, {"message_id": "R10M",
                                      "message_type": 1,
                                      "from_user_id": "u1"}, "u1")
finally:
    B._process_inbound_body = _orig_body
check("R10 落盘前 OSError 返回 False 且 claim 已释放（可重投）",
      _ok_r10 is False
      and not os.path.exists(os.path.join(B.INBOX, "R10M.claim")))

# ---------------- M：脚本工具 ----------------
_ffmpeg = shutil.which("ffmpeg")
if _ffmpeg:
    _pngf = os.path.join(TMP, "shot.png")
    subprocess.run([_ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=200x200:d=0.1", "-frames:v", "1",
                    _pngf, "-y"], check=True)
    _ms = subprocess.run([sys.executable,
                          os.path.join(ROOT, "scripts", "media_screen.py"),
                          _pngf], capture_output=True, text=True, timeout=120)
    _msj = json.loads(_ms.stdout)
    check("M1 media_screen 图片机器检通过", _ms.returncode == 0
          and _msj.get("ok") is True and _msj.get("kind") == "image")
    _txtf = os.path.join(TMP, "notmedia.bin")
    with open(_txtf, "wb") as f:
        f.write(b"not a media file" * 100)
    _ms2 = subprocess.run([sys.executable,
                           os.path.join(ROOT, "scripts", "media_screen.py"),
                           _txtf], capture_output=True, text=True, timeout=120)
    check("M1b media_screen 非媒体文件不过", _ms2.returncode == 2)
else:
    check("M1 media_screen 图片机器检通过", False, "ffmpeg 缺失")
_ra = subprocess.run([sys.executable,
                      os.path.join(ROOT, "scripts", "realism_audit.py"),
                      "--json"], env=dict(os.environ, COMPANION_HOME=TMP),
                     capture_output=True, text=True, timeout=120)
_raj = json.loads(_ra.stdout)
check("M2 realism_audit 输出 12 项指标且不回显正文",
      _ra.returncode == 0 and len(_raj.get("metrics", {})) == 12
      and _raj["counts"]["stance_rounds"] >= 6)
# prune_media_in：多图分组 + 有留档才删（dry-run 只报告）
_mi = os.path.join(STATE, "media_in")
_old = time.time() - 100 * 86400
for _fn in ("1001.jpg", "1001.i2.jpg", "1002.jpg"):
    _p = os.path.join(_mi, _fn)
    with open(_p, "wb") as f:
        f.write(_jpg)
    os.utime(_p, (_old, _old))
append_jsonl(_hist, {"ts": _now, "role": "user", "text": "看这张",
                     "kind": "reply", "message_id": "1001",
                     "media_file": "state/media_in/1001.jpg",
                     "img_desc": "一张江边照片"})
if os.name == "nt":
    # prune_media_in.sh 是 POSIX 运维脚本（bash + 部署机清理），
    # Windows 部署不走此脚本：按平台分流，只验脚本在册。
    check("M3 prune_media_in dry-run：整组报告、无留档保留、默认不删"
          "（Windows 分流：POSIX 运维脚本，只验在册）",
          os.path.isfile(os.path.join(ROOT, "scripts", "prune_media_in.sh")))
else:
    _pr = subprocess.run(["bash",
                          os.path.join(ROOT, "scripts", "prune_media_in.sh")],
                         env=dict(os.environ, COMPANION_HOME=TMP),
                         capture_output=True, text=True, timeout=120)
    check("M3 prune_media_in dry-run：整组报告、无留档保留、默认不删",
          _pr.returncode == 0 and "1001.jpg" in _pr.stdout
          and "1001.i2.jpg" in _pr.stdout and "1002" not in _pr.stdout
          and os.path.exists(os.path.join(_mi, "1001.jpg")),
          f"rc={_pr.returncode} out={_pr.stdout[:200]} err={_pr.stderr[:200]}")

# ---------------- CLI 冒烟 ----------------
_cli_env = dict(os.environ, COMPANION_HOME=TMP)
_ea = subprocess.run([sys.executable, os.path.join(ROOT, "worker.py"),
                      "episode-add", "--kind", "inside_joke",
                      "--text", "他管邻居家的猫叫所长"],
                     env=_cli_env, capture_output=True, text=True, timeout=120)
_ea2 = subprocess.run([sys.executable, os.path.join(ROOT, "worker.py"),
                       "episode-add", "--kind", "inside_joke",
                       "--text", "他管邻居家的猫叫所长"],
                      env=_cli_env, capture_output=True, text=True, timeout=120)
_lr = subprocess.run([sys.executable, os.path.join(ROOT, "worker.py"),
                      "learn-report"], env=_cli_env,
                     capture_output=True, text=True, timeout=120)
check("C1 CLI episode-add 落账/判重 + learn-report 可跑",
      _ea.returncode == 0 and json.loads(_ea.stdout).get("added") is True
      and _ea2.returncode == 1
      and json.loads(_ea2.stdout).get("added") is False
      and _lr.returncode == 0
      and "promote_watch" in json.loads(_lr.stdout))

print()
_fails = [n for n, ok in RESULTS if not ok]
print(f"==== round28 selftest: {len(RESULTS) - len(_fails)}/{len(RESULTS)} PASS ====")
if _fails:
    print("FAIL:", "；".join(_fails))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if _fails else 0)
