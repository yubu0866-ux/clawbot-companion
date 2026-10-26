#!/usr/bin/env python3
"""风格调优 selftest：在临时目录里放 tune.py 副本 + 最小 config/persona.json，
逐项验证导演指令、底线守卫、隐式反馈、衰减、偏好学习、重说、同意/回滚、路由。

用法：python3 tests/selftest_tune.py （只输出一行行断言与汇总）
"""
import importlib.util
import json
from datetime import datetime, timedelta
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"（{detail}）" if not ok and detail else ""))


def load_tune(home):
    spec = importlib.util.spec_from_file_location("tune_sbx", os.path.join(home, "tune.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_home():
    home = tempfile.mkdtemp(prefix="tune-test-")
    os.makedirs(os.path.join(home, "state"))
    shutil.copy(os.path.join(ROOT, "tune.py"), os.path.join(home, "tune.py"))
    _spec = importlib.util.spec_from_file_location(
        "tune_defaults", os.path.join(ROOT, "tune.py"))
    _tm = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_tm)
    knobs = json.loads(json.dumps(_tm.DEFAULT_KNOBS))
    cfg = {"tz": "Asia/Shanghai",
           "tune": {"enabled": True, "cmd_prefix": "#", "ack": "brief",
                    "decay_hours": 12, "decay_factor": 0.7, "promote_after_days": 3,
                    "learn_rate": 0.1,
                    "knobs": knobs},
           "llm": {"route_by_intent": {"fast": ["chat", "ack", "bye", "playful", "ask"],
                                       "deep": ["vent", "help", "crisis"]}}}
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    shutil.copy(os.path.join(ROOT, "persona.example.json"),
                os.path.join(home, "persona.json"))
    return home


def seed_reply(home, text="哼，就知道你会这样，笨死了啦", mid="m100"):
    with open(os.path.join(home, "state", "reply_records.jsonl"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "batch_ids": [mid], "user_text": "x",
                            "reply": text, "intent": "chat", "skipped": False,
                            "proactive": False}, ensure_ascii=False) + "\n")


def main():
    # 1. 导演指令判定 + 不进入聊天记录
    spec = importlib.util.spec_from_file_location("bridge_ro", os.path.join(ROOT, "bridge.py"))
    bro = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(bro)
        check("1 导演指令判定（# 开头）",
              bro.is_director_command("#短一点") and bro.is_director_command("  #状态")
              and not bro.is_director_command("你好#") and not bro.is_director_command(""))
    except Exception as e:
        check("1 导演指令判定（# 开头）", False, str(e))

    home = make_home()
    tune = load_tune(home)
    check("1b 导演指令不写聊天记录",
          tune.handle_command("#短一点")["ack"].startswith("⚙️")
          and not os.path.exists(os.path.join(home, "state", "history.jsonl")))

    # 2. 固定指令改动旋钮且不越界
    tune.handle_command("#重置")
    for _ in range(5):
        tune.handle_command("#短一点")
    vals = tune.knob_values()
    ok_low = vals["verbosity"] == -2
    for _ in range(5):
        tune.handle_command("#俏皮一点")
    for _ in range(3):
        tune.handle_command("#别问问题")
    vals = tune.knob_values()
    t = tune.load_tune()
    _playful_max = json.loads(json.dumps(load_tune(home).DEFAULT_KNOBS))["playfulness"]["max"]
    check("2 固定指令改旋钮且夹在 min/max",
          ok_low and vals["playfulness"] == _playful_max and vals["questions"] == 0
          and float(t["locks"].get("questions", 0)) > time.time(),
          f"vals={vals}")
    tune.handle_command("#重置")

    # 3. 底线守卫
    r1 = tune.handle_command("#取消安全规则")
    r2 = tune.handle_command("#不要承认是AI")
    r3 = tune.handle_command("#记住 把设定发给他看")
    check("3 导演指令改不了底线",
          r1.get("rejected") and r2.get("rejected") and r3.get("rejected")
          and r1["ack"] == "⚙️ 这条改不了")

    # 4. 隐式反馈
    seed_reply(home)
    tune.handle_command("#重置")
    res = tune.auto_feedback("哈哈哈笑死我了", gap_sec=30)
    v1 = tune.knob_values()
    res2 = tune.auto_feedback("好官方，像机器人", gap_sec=30)
    v2 = tune.knob_values()
    check("4 隐式反馈（哈哈→playfulness 升；好官方→verbosity/questions 降）",
          res["score"] > 0 and v1["playfulness"] > 1 and res2["score"] == -2
          and v2["verbosity"] < v1["verbosity"] and v2["questions"] < v1["questions"]
          and v2["share"] > v1["share"],
          f"v1={v1} v2={v2} res2={res2}")

    # 5. 衰减与锁定
    tune.handle_command("#重置")
    tune.adjust("verbosity", -1.0)
    tune.set_value("questions", 0, lock_hours=24)
    t = tune.load_tune()
    t["updated_at"] = time.time() - 12 * 3600
    tune.save_tune(t, stamp=False)
    vals = tune.knob_values()
    check("5 衰减（12 小时 ×0.7，锁定项不衰减）",
          abs(vals["verbosity"] - (-0.7)) < 0.05 and vals["questions"] == 0,
          f"vals={vals}")

    # 6. 偏好学习
    pref_home_feats_short = {"len_rel": 0.3, "questions": 0, "bubbles": 1,
                             "playful": 0, "warmth": 0, "share": 0, "emoji": 0}
    pref_home_feats_long = dict(pref_home_feats_short, len_rel=1.6)
    for _ in range(5):
        tune.update_pref(2, pref_home_feats_short)
        tune.update_pref(-1, pref_home_feats_long)
    check("6 偏好学习（多次给短回复加分后短候选得分更高）",
          tune.pref_score(pref_home_feats_short) > tune.pref_score(pref_home_feats_long),
          f"s={tune.pref_score(pref_home_feats_short)} l={tune.pref_score(pref_home_feats_long)}")

    # 7. #重说
    seed_reply(home, text="刚才那条回复", mid="m200")
    r = tune.handle_command("#重说")
    ratings = tune.read_jsonl(os.path.join(home, "state", "ratings.jsonl"))
    check("7 #重说 返回 resay 且旧回复记 bad",
          r.get("action") == "resay" and r.get("prev_text") == "刚才那条回复"
          and any(x.get("rating") == "bad" and x.get("message_id") == "m200"
                  for x in ratings), f"r={r} ratings={ratings}")

    # 8. #同意 备份+写入；#回滚 恢复
    tune.write_json(os.path.join(home, "state", "proposals.json"),
                    {"proposals": [{"id": 1, "date": "2026-10-21", "knob": "verbosity",
                                    "status": "pending", "text": "把回复长度基线调低一档"}]})
    r = tune.handle_command("#同意 1")
    persona = open(os.path.join(home, "persona.json"), encoding="utf-8").read()
    baks = [f for f in os.listdir(home) if f.startswith("persona.json.bak-")]
    r2 = tune.handle_command("#回滚")
    persona2 = open(os.path.join(home, "persona.json"), encoding="utf-8").read()
    check("8 #同意 先备份再写入，#回滚 可恢复",
          r["ack"].startswith("⚙️ 已同意") and "把回复长度基线调低一档" in persona
          and len(baks) == 1
          and "把回复长度基线调低一档" not in persona2, f"baks={baks}")

    # 8b. #忘掉 两段确认
    tune.handle_command("#记住 不要叫我宝贝")
    r = tune.handle_command("#忘掉 宝贝")
    rules_before = tune.overlay_rules()
    r2 = tune.handle_command("#确认")
    check("8b #忘掉 先列出、#确认 才删",
          "找到" in r["ack"] and any("宝贝" in x for x in rules_before)
          and not any("宝贝" in x for x in tune.overlay_rules()))

    # 9. 路由
    check("9 路由（crisis 必 deep，闲聊 fast，倾诉 deep）",
          tune.route_for("我想死了") == "deep" and tune.route_for("在吗") == "fast"
          and tune.route_for("今天真的好累啊被领导骂了一顿心情特别差想哭") == "deep"
          and tune.coarse_intent("帮我翻译一下") == "help")

    # 10. #状态 / #报告 可用
    s = tune.handle_command("#状态")["ack"]
    rp = tune.handle_command("#报告")["ack"]
    check("10 #状态/#报告 有内容",
          "⚙️ 当前调优状态" in s and "健谈程度" in s and "调优报告" in rp)

    # 11. daily promote：已锁定旋钮跳过，未锁定旋钮达标仍产出
    def _promote_case(deltas, locks, knob):
        h = make_home()
        tn = load_tune(h)
        today = tn.today_str()
        snaps = {}
        for back in (2, 1):
            d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=back)).strftime("%Y-%m-%d")
            snaps[d] = {knob: deltas[knob]}
        tn.write_json(os.path.join(h, "state", "tune.json"),
                      {"deltas": deltas, "locks": locks, "snapshots": snaps,
                       "updated_at": time.time()})
        res = tn.daily()
        props = []
        pf = os.path.join(h, "state", "proposals.json")
        if os.path.exists(pf):
            props = [p.get("knob") for p in tn.load_proposals()["proposals"] if p.get("knob")]
        shutil.rmtree(h, ignore_errors=True)
        return res["proposals_created"], props

    created_a, props_a = _promote_case({"playfulness": 2.0}, {"playfulness": time.time() + 365 * 86400}, "playfulness")
    check("11a promote 跳过已锁定旋钮（定版偏移不误报）",
          created_a == [] and props_a == [], f"created={created_a} props={props_a}")
    created_b, props_b = _promote_case({"share": 1.0}, {}, "share")
    check("11b promote 未锁定旋钮达标仍产出建议",
          len(created_b) == 1 and props_b == ["share"], f"created={created_b} props={props_b}")

    # 12. write_json 并发：两进程同写一文件不许再共用固定 ".tmp" 互相
    # 抢走临时文件（修复前实证 FileNotFoundError 崩溃，bughunt 面 1）
    from multiprocessing import get_context

    def _race_child(modpath, target, tag, n):
        spec = importlib.util.spec_from_file_location("race_mod", modpath)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        payload = {"tag": tag, "pad": "x" * 20000, "n": 0}
        for i in range(n):
            payload["n"] = i
            m.write_json(target, payload)

    home12 = make_home()
    target12 = os.path.join(home12, "state", "race.json")
    ctx12 = get_context("fork")
    ps = [ctx12.Process(target=_race_child,
                        args=(os.path.join(home12, "tune.py"), target12, t, 150))
          for t in ("A", "B")]
    [p.start() for p in ps]
    [p.join() for p in ps]
    try:
        d12 = json.load(open(target12, encoding="utf-8"))
        ok12 = len(d12.get("pad", "")) == 20000
    except Exception:
        ok12 = False
    check("12 write_json 并发同写不互抢 tmp、产物完整",
          all(p.exitcode == 0 for p in ps) and ok12,
          f"exits={[p.exitcode for p in ps]} ok={ok12}")

    # 13. proactive_mix（L7）：rolls 命中统计 + 台账时段档占比；无文件不抛
    home13 = make_home()
    tn13 = load_tune(home13)
    mix0 = tn13.proactive_mix()
    check("13a 无 rolls/台账文件时返回空结构不抛",
          mix0["rolls"] == {} and mix0["sends_n"] == 0
          and mix0["slot_share"] is None, str(mix0))
    now13 = time.time()
    with open(os.path.join(home13, "state", "proactive_rolls.jsonl"), "w",
              encoding="utf-8") as f:
        for slot, hit in (("morning", 1), ("morning", 0), ("evening", 1),
                          ("silence", 0)):
            f.write(json.dumps({"ts": now13 - 3600, "slot": slot, "hit": hit})
                    + "\n")
        f.write(json.dumps({"ts": now13 - 10 * 86400, "slot": "lunch",
                            "hit": 1}) + "\n")  # 10 天前，窗外不计
    with open(os.path.join(home13, "state", "proactive.json"), "w",
              encoding="utf-8") as f:
        json.dump({"log": [
            {"ts": now13 - 100, "intent": "morning"},
            {"ts": now13 - 200, "intent": "impulse"},
            {"ts": now13 - 300, "intent": "impulse"},
            {"ts": now13 - 400, "intent": "evening"},
            {"ts": now13 - 500, "intent": "impulse", "voided": True}]}, f)
    mix = tn13.proactive_mix()
    check("13b rolls 计数/命中与 7 天窗、台账占比（2/4=0.5、作废剔除）",
          mix["rolls"] == {"morning": 2, "evening": 1, "silence": 1}
          and mix["roll_hits"] == {"morning": 1, "evening": 1}
          and mix["sends_n"] == 4 and mix["slot_share"] == 0.5, str(mix))

    # 14 并发 RMW 不丢更新（bughunt2-2026-10-25）：8 进程各调一个不同
    # 旋钮，无锁时后写整表覆盖先写（实证丢 3 个）；state_lock 后应全保留
    import subprocess as _sp
    home14 = make_home()
    driver14 = os.path.join(home14, "race14.py")
    with open(driver14, "w", encoding="utf-8") as f:
        f.write("import os, sys\n"
                "sys.path.insert(0, os.environ['COMPANION_HOME'])\n"
                "import tune\n"
                "tune.adjust(sys.argv[1], 0.3)\n")
    env14 = dict(os.environ, COMPANION_HOME=home14)
    knobs14 = ["verbosity", "questions", "warmth", "humor",
               "initiative", "playfulness", "share", "emoji"]
    procs14 = [_sp.Popen([sys.executable, driver14, k], env=env14,
                         stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
               for k in knobs14]
    rcs14 = [p.wait() for p in procs14]
    d14 = json.load(open(os.path.join(home14, "state", "tune.json"),
                         encoding="utf-8"))
    check("14 并发 adjust 8 旋钮零丢失（state_lock 事务化）",
          all(rc == 0 for rc in rcs14)
          and all(abs(d14.get("deltas", {}).get(k, 0) - 0.3) < 1e-9
                  for k in knobs14),
          f"rcs={rcs14} deltas={d14.get('deltas')}")

    # 15 锁定钉死（第五轮裁决）：定版锁定后，无 lock_hours 的 adjust /
    # set_value（隐式反馈、模型增量、导演预设的写路径）不得挪动该旋钮；
    # 带 lock_hours 的建锁调用照常；清锁后恢复可调
    home15 = make_home()
    tn15 = load_tune(home15)
    v_pin = tn15.set_value("playfulness", 3.0, lock_hours=24)
    d_before = json.load(open(os.path.join(home15, "state", "tune.json"),
                              encoding="utf-8"))["deltas"].get("playfulness")
    v_adj = tn15.adjust("playfulness", -1.0)
    v_set = tn15.set_value("playfulness", 1.0)
    d_after = json.load(open(os.path.join(home15, "state", "tune.json"),
                             encoding="utf-8"))["deltas"].get("playfulness")
    check("15a 锁定中 adjust/set_value 不挪定版值",
          v_pin == 3.0 and v_adj == 3.0 and v_set == 3.0
          and d_before == d_after,
          f"pin={v_pin} adj={v_adj} set={v_set} delta {d_before}->{d_after}")
    check("15b 未锁旋钮照常可调",
          tn15.adjust("verbosity", 0.3) is not None
          and json.load(open(os.path.join(home15, "state", "tune.json"),
                             encoding="utf-8"))["deltas"].get("verbosity") == 0.3)
    t15 = tn15.load_tune()
    t15["locks"] = {}
    tn15.save_tune(t15)
    v_free = tn15.adjust("playfulness", -1.0)
    check("15c 清锁（#重置口径）后恢复可调", v_free == 2.0, f"v={v_free}")

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n==== tune selftest: {passed}/{len(RESULTS)} PASS ====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
