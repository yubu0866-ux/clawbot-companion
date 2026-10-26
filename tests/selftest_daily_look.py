#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日形象自检（通用版改写）：用仓库自带示例内容包（content/）验证
衣橱/妆容卡解析、daily_look 按日期确定性落定（同日同结果、类别与
妆容映射、工作日/周末口径）、_pick_outfit 的 7 天避重与季节过滤、
look_for_scene 的 scene_looks.json 映射与回落、内容包缺失降级。

源套件对个人内容包的全量断言（套装总数/专属字段/场景总数/
动作例库等）不适用于示例包，已剔除，见 tests/README.md。

沙箱纪律：worker.py 副本 + example 三件套 + 示例内容包全部落在
/tmp 临时目录，绝不碰生产 state/；结束自清理。运行：
TZ=Asia/Shanghai python3 tests/selftest_daily_look.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

os.environ["TZ"] = "Asia/Shanghai"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = tempfile.mkdtemp(prefix="ccb-daily-look-")
os.environ["COMPANION_HOME"] = SANDBOX

for fn in ("worker.py", "bridge.py", "llm.py", "tune.py"):
    shutil.copy(os.path.join(ROOT, fn), os.path.join(SANDBOX, fn))
for src_name, dst_name in (("config.example.json", "config.json"),
                           ("persona.example.json", "persona.json"),
                           ("facts.example.json", "facts.json")):
    shutil.copy(os.path.join(ROOT, src_name), os.path.join(SANDBOX, dst_name))
shutil.copytree(os.path.join(ROOT, "content"),
                os.path.join(SANDBOX, "content"))
STATE = os.path.join(SANDBOX, "state")
for d in ("state", "inbox", "outbox/pending", "outbox/sent", "outbox/failed"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)

_spec = importlib.util.spec_from_file_location(
    "worker_look", os.path.join(SANDBOX, "worker.py"))
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"（{detail}）" if detail and not cond else ""))


def reset_look_state():
    p = os.path.join(STATE, "daily_look.json")
    if os.path.exists(p):
        os.remove(p)


# ---------- L01 衣橱卡解析（示例包） ----------
cats = worker.wardrobe_outfits()
check("L01-1 示例衣橱 3 类齐备（通勤/日常/居家）",
      set(cats) == {"通勤", "日常", "居家"}, str(sorted(cats)))
check("L01-2 每类 ≥1 套、每套字段齐（上装/下装/鞋履/配饰）且非空",
      all(len(v) >= 1 for v in cats.values())
      and all(set(o["items"]) == {"上装", "下装", "鞋履", "配饰"}
              and all(o["items"].values())
              for v in cats.values() for o in v),
      str({k: [o["name"] for o in v] for k, v in cats.items()}))
check("L01-3 套装 id 为「类别|名」、结构节不入类别",
      all(o["id"] == f"{o['category']}|{o['name']}"
          for v in cats.values() for o in v)
      and "结构" not in cats)

# ---------- L02 妆容发型卡解析（示例包） ----------
pool = worker.looks_pool()
check("L02-1 妆容 4 级齐备（通勤淡妆/日常淡妆/素颜/正式妆）且构成非空",
      set(pool["makeup"]) == {"通勤淡妆", "日常淡妆", "素颜", "正式妆"}
      and all(pool["makeup"].values()),
      str(sorted(pool["makeup"])))
check("L02-2 发型池与示例卡一致（4 种）",
      pool["hair"] == ["自然披发", "低马尾", "丸子头", "短发利落款"],
      str(pool["hair"]))

# ---------- L03 同日确定性 ----------
reset_look_state()
D0 = date(2026, 10, 20)  # 周二
a = worker.daily_look(D0)
b = worker.daily_look(D0)
check("L03-1 同日两次落定全等（同日同结果）", a == b and a["outfit_name"],
      f"{a.get('outfit_name')} vs {b.get('outfit_name')}")
saved = json.load(open(os.path.join(STATE, "daily_look.json"),
                       encoding="utf-8"))
check("L03-2 落盘条目与返回值一致",
      any(e["date"] == D0.isoformat() and e["outfit_id"] == a["outfit_id"]
          for e in saved["entries"]))
_by_id = {o["id"]: o for v in cats.values() for o in v}
check("L03-3 工作日类别=通勤、妆容=通勤淡妆，shoes 与套装鞋履字段全等",
      a["outfit_category"] == "通勤" and a["makeup_level"] == "通勤淡妆"
      and a["shoes"] == _by_id[a["outfit_id"]]["items"]["鞋履"]
      and a["outfit_items"] == _by_id[a["outfit_id"]]["items"],
      f"{a.get('outfit_name')} / {a.get('shoes')}")

# ---------- L04 连续 14 天：类别/妆容映射（示例包每类仅 1 套，
# 不验 7 天不重复——那由 L08 的 _pick_outfit 构造用例承接） ----------
reset_look_state()
MON = date(2026, 10, 19)
assert MON.weekday() == 0
seq = [worker.daily_look(MON + timedelta(days=i)) for i in range(14)]
check("L04-1 工作日类别=通勤、妆容=通勤淡妆",
      all(s["outfit_category"] == "通勤" and s["makeup_level"] == "通勤淡妆"
          for i, s in enumerate(seq)
          if (MON + timedelta(days=i)).weekday() < 5))
check("L04-2 周末类别在日常/居家、妆容随类别（日常淡妆/素颜）",
      all((s["outfit_category"] == "日常" and s["makeup_level"] == "日常淡妆")
          or (s["outfit_category"] == "居家" and s["makeup_level"] == "素颜")
          for i, s in enumerate(seq)
          if (MON + timedelta(days=i)).weekday() >= 5))
check("L04-3 发型恒在池内、妆容构成非空",
      all(s["hair"] in pool["hair"] and s["makeup_desc"] for s in seq),
      str([(s["hair"], s["makeup_level"]) for s in seq]))

# ---------- L05 简报含今日形象 ----------
pack = worker.build_pack()
dl = pack.get("daily_look") or {}
check("L05-1 build_pack 带 daily_look 且为真实今日、字段齐",
      dl.get("date") == worker.today_local().date().isoformat()
      and dl.get("outfit_name") and dl.get("makeup_level")
      and dl.get("hair") and dl.get("outfit_items"),
      str(dl)[:160])
check("L05-2 简报 daily_look 鞋款与落定套装一致",
      dl.get("shoes") == _by_id[dl["outfit_id"]]["items"]["鞋履"],
      str(dl.get("shoes")))

# ---------- L06 场景修正：无映射回落当日基准、有映射优先 ----------
check("L06-1 场景名单与示例 scene_cards.md 全等（3 个）",
      worker.scene_card_names() == ["客厅 · 傍晚", "书桌 · 夜晚", "公园 · 清晨"],
      str(worker.scene_card_names()))
check("L06-2 无 scene_looks.json 时映射为空、look_for_scene 回落当日基准",
      worker.scene_look_map() == {}
      and worker.look_for_scene("客厅 · 傍晚")["makeup_level"] == dl["makeup_level"]
      and worker.look_for_scene("没这个场景")["hair"] == dl["hair"])
with open(os.path.join(SANDBOX, "content", "scene_looks.json"), "w",
          encoding="utf-8") as f:
    json.dump({"客厅 · 傍晚": ["正式妆", "丸子头"]}, f, ensure_ascii=False)
sc = worker.look_for_scene("客厅 · 傍晚")
check("L06-3 scene_looks.json 映射优先（妆容+发型覆盖、穿搭透传当日）",
      sc["makeup_level"] == "正式妆" and sc["hair"] == "丸子头"
      and sc["shoes"] == dl["shoes"], str(sc))
os.remove(os.path.join(SANDBOX, "content", "scene_looks.json"))

# ---------- L07 内容包缺失降级 ----------
reset_look_state()
os.rename(os.path.join(SANDBOX, "content"),
          os.path.join(SANDBOX, "content_bak"))
try:
    deg = worker.daily_look(date(2026, 11, 2))  # 周一
    check("L07-1 内容包缺失不崩、降级带基准妆容与默认发型、穿搭为空",
          deg["makeup_level"] == "通勤淡妆" and deg["hair"] == "自然披肩"
          and deg["outfit_name"] == ""
          and worker.wardrobe_outfits() == {}
          and worker.looks_pool() == {"makeup": {}, "hair": []},
          str(deg)[:120])
finally:
    os.rename(os.path.join(SANDBOX, "content_bak"),
              os.path.join(SANDBOX, "content"))
    reset_look_state()

# ---------- L08 _pick_outfit：7 天避重与季节过滤（构造用例） ----------
_pool = [{"name": "夏令测试装", "id": "测试|夏令测试装", "items": {}},
         {"name": "四季测试装", "id": "测试|四季测试装", "items": {}}]
_sea = {"夏令测试装": "盛夏", "四季测试装": "四季"}
_oct = date(2026, 10, 25)
_jul = date(2026, 7, 25)
assert _oct.toordinal() % 2 == _jul.toordinal() % 2  # 轮换位同指夏令装
got_oct = worker._pick_outfit(_pool, set(), _oct, _sea)
got_jul = worker._pick_outfit(_pool, set(), _jul, _sea)
check("L08-1 季节过滤：夏令项深秋跳过、盛夏可选",
      got_oct["name"] == "四季测试装" and got_jul["name"] == "夏令测试装",
      f"10月={got_oct['name']} 7月={got_jul['name']}")
got_used = worker._pick_outfit(_pool, {"测试|四季测试装"}, _oct, _sea)
check("L08-2 近 7 天用过的整套跳过（回落未用过的）",
      got_used["name"] == "夏令测试装", str(got_used))
check("L08-3 空池返回 None、示例包无季节表时返回空表",
      worker._pick_outfit([], set(), _oct, _sea) is None
      and worker.outfit_seasons() == {})

# ---------- L09 简报内容包引用行 ----------
check("L09-1 简报场景行带 3 个场景名、expressions 引用行在、realism 缺失降级为空",
      "固定场景 3 个" in (pack.get("scene_cards") or "")
      and "expressions.md" in (pack.get("expressions") or "")
      and pack.get("realism") == "",
      str({k: pack.get(k) for k in ("scene_cards", "realism", "expressions")})[:160])

fails = [n for n, ok in RESULTS if not ok]
print(f"\ndaily_look 自检（通用版）：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过"
      + (f"；失败：{fails}" if fails else ""))
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if fails else 0)
