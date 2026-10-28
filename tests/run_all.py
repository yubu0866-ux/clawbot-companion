#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clawbot-companion 自检总入口（跨平台版）：逐套顺序运行（不并行，避免
计时类用例互相干扰），汇总每套结果；任一失败整体退出码非 0。
用法：python3 tests/run_all.py [套件名子串过滤]
与 tests/run_all.sh 行为等价；Windows 没有 bash 时用这个入口。
子进程统一注入 PYTHONUTF8=1：Windows 的区域默认编码会让中文 fixture
与日志在读写时乱码，Linux 上该变量无副作用。"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    "selftest_audio_screen.py",
    "selftest_env_smoke.py",
    "selftest_tune.py",
    "selftest_jailbreak_guard.py",
    "selftest_stage5.py",
    "selftest_daily_look.py",
    "selftest_brief_battery.py",
    "selftest_burst.py",
    "selftest_calendar.py",
    "selftest_proactive.py",
    "selftest_life.py",
    "selftest_worker.py",
    "selftest_pressure.py",
    "selftest_channel.py",
    "selftest_round28.py",
]
TIMEOUT = 1800


def main():
    # 入口自身输出先切 UTF-8：Windows 默认 cp1252 下打印失败套件中文尾部
    # 会 UnicodeEncodeError 崩溃（CI 首跑 Windows 格实证），子进程另有
    # PYTHONUTF8=1 注入，此处只保入口进程自身的 print。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    env = dict(os.environ)
    env["TZ"] = "Asia/Shanghai"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    passed, failed = [], []
    fail_detail = {}  # 失败套件 -> (失败行列表, 输出尾部)，供文末与 Step Summary 复用
    with tempfile.TemporaryDirectory() as logdir:
        for name in SUITES:
            if only and only not in name:
                continue
            log_path = os.path.join(logdir, name + ".log")
            print(f"== {name} ... ", end="", flush=True)
            try:
                with open(log_path, "wb") as lf:
                    rc = subprocess.run(
                        [sys.executable, os.path.join(HERE, name)],
                        stdout=lf, stderr=subprocess.STDOUT,
                        env=env, timeout=TIMEOUT).returncode
            except subprocess.TimeoutExpired:
                rc = -1
            if rc == 0:
                summary = ""
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as lf:
                        for line in lf:
                            if re.search(r"PASS|通过|共 [0-9]+ 项", line):
                                summary = line.strip()
                except OSError:
                    pass
                print(f"PASS  {summary}")
                passed.append(name)
            else:
                print(f"FAIL (exit={rc})")
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as lf:
                        lines = lf.read().splitlines()
                    # 失败行优先：只打尾部 30 行时，失败项常在日志中段、
                    # CI 里根本看不到（2026-10-27 第三次运行实证）。
                    fails = [l for l in lines if "FAIL" in l]
                    if fails:
                        print(f"---- {name} 失败行（共 {len(fails)} 行） ----")
                        print("\n".join(fails))
                    tail = lines[-30:]
                    print(f"---- {name} 输出尾部 ----")
                    print("\n".join(tail))
                    print("----------------------")
                    fail_detail[name] = (fails, tail)
                except OSError:
                    fail_detail[name] = ([], [])
                failed.append(name)
    print()
    print(f"==== run_all: {len(passed)} 套通过 / {len(failed)} 套失败 ====")
    if failed:
        print("失败套件:" + " ".join(failed))
    _write_step_summary(passed, failed, fail_detail)
    _emit_error_annotations(failed, fail_detail)
    return 1 if failed else 0


def _emit_error_annotations(failed, fail_detail):
    """GitHub Actions annotation 通道：每个失败套件发一条 ::error，
    标题带套件名、正文带头几条 FAIL 行——annotation 在 run/commit 页
    不点开 job 日志也可见，是 Step Summary 之外的第二条定位通道。
    仅 Actions 环境（GITHUB_ACTIONS=true）输出，本地运行不污染输出；
    workflow command 转义：% 先行，再 \\r/\\n。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return

    def esc(s):
        return (s.replace("%", "%25").replace("\r", "%0D")
                 .replace("\n", "%0A"))[:300]

    for name in failed:
        fails, _tail = fail_detail.get(name, ([], []))
        head = " | ".join(f.strip() for f in fails[:3]) or "exit!=0（无 FAIL 行）"
        print(f"::error title=selftest FAIL {esc(name)}::{esc(head)}",
              flush=True)


def _write_step_summary(passed, failed, fail_detail):
    """GitHub Actions Step Summary：CI 里 job 日志不易定位失败项时，
    把逐套结果与失败套件的 FAIL 行/尾部写到 run 页的 Summary 区，
    不开日志也能看到哪套、哪项挂了。仅当 GITHUB_STEP_SUMMARY 存在
    （Actions 环境）时写，本地运行无副作用；写失败静默。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"## selftest run_all：{len(passed)} 套通过 / "
                    f"{len(failed)} 套失败\n\n")
            for name in passed:
                f.write(f"- ✅ {name}\n")
            for name in failed:
                f.write(f"- ❌ {name}\n")
            for name in failed:
                fails, tail = fail_detail.get(name, ([], []))
                f.write(f"\n### ❌ {name}\n\n")
                if fails:
                    f.write("失败行：\n\n```\n" + "\n".join(fails)
                            + "\n```\n\n")
                if tail:
                    f.write("输出尾部：\n\n```\n" + "\n".join(tail)
                            + "\n```\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
