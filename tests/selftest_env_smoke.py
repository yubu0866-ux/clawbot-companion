#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境兼容冒烟例（通用版）：锁死几条「漂移即静默降级」的运行时路径。

  E1 faster-whisper 数组路径转写：合成短音频 → 照 scripts/audio_screen.py
     现行 _WHISPER_CHILD（wave 读 int16 → numpy float32 数组喂
     WhisperModel tiny）跑一轮，断言不抛、返回结构正确。另经生产函数
     whisper_check 端到端验 status == ok。转写解释器按
     AUDIO_SCREEN_STT_PYTHON → 当前解释器 → 常见 venv 布局的顺序发现；
     本机没有任何带 faster-whisper 的解释器时本项 SKIP（可选依赖未装
     不算代码失败），装了却跑不通则响亮 FAIL。
  E2 ffmpeg loudnorm 可用：合成正弦音频跑 loudnorm 测量，
     print_format=summary 输出须含 "Input Integrated"；同时验生产
     口径 print_format=json 经 audio_screen.loudness_lufs 能解析出数值。
  E3 核心模块可导入 + 示例配置可解析：临时 COMPANION_HOME 沙箱里
     import bridge/llm/worker/tune/proactive，bridge.load_config 对
     config.example.json 落位的 config.json 不得抛错。
  E4 openssl 进程内解密路径：import bridge 的 ctypes EVP 实现
     （_aes_ecb_encrypt/_aes_ecb_decrypt），一轮加解密往返须一致；
     密文须与 openssl CLI 路径逐字节等价；错钥须抛 RuntimeError；
     find_library('crypto') 须命中。

（源套件的 age 异地备份往返项随该机制未进入通用版一并剔除，
见 tests/README.md。）

用法：python3 tests/selftest_env_smoke.py。全部产物在临时目录。"""
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name
          + (f"（{detail}）" if detail and not ok else ""))


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def synth_wav(path, seconds=1.0, sr=16000, freq=440.0):
    """合成 16k 单声道 int16 正弦 wav（与 _WHISPER_CHILD 的读法对齐）。"""
    import math
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr)))
        for i in range(int(seconds * sr)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)


def discover_stt_python():
    """发现带 faster-whisper 的解释器（同 selftest_audio_screen 口径）。"""
    import glob
    cands = []
    if os.environ.get("AUDIO_SCREEN_STT_PYTHON"):
        cands.append(os.environ["AUDIO_SCREEN_STT_PYTHON"])
    cands.append(sys.executable)
    home = os.path.expanduser("~")
    for pat in (os.path.join(home, "*venv*", "bin", "python"),
                os.path.join(home, ".venvs", "*", "bin", "python"),
                os.path.join(home, "workspace", "*venv*", "bin", "python"),
                os.path.join(home, "*venv*", "Scripts", "python.exe"),
                os.path.join(home, ".venvs", "*", "Scripts", "python.exe"),
                os.path.join(home, "workspace", "*venv*", "Scripts", "python.exe")):
        cands.extend(sorted(glob.glob(pat)))
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import faster_whisper"],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            return c
    return None


def main():
    td = tempfile.mkdtemp(prefix="env-smoke-")
    # bridge import 只读环境变量定位数据目录：指到临时目录，绝不碰生产 state
    os.environ["COMPANION_HOME"] = td

    stt_py = discover_stt_python()
    if stt_py:
        os.environ["AUDIO_SCREEN_STT_PYTHON"] = stt_py

    screen = load_mod("audio_screen_sbx",
                      os.path.join(ROOT, "scripts", "audio_screen.py"))

    wav_path = os.path.join(td, "tone.wav")
    synth_wav(wav_path)

    # ---- E1: faster-whisper 数组路径（可选依赖：无解释器则 SKIP） ----
    if not stt_py:
        print("SKIP E1 转写冒烟（本机无带 faster-whisper 的解释器）")
    else:
        check("E1a-0 转写解释器可用", os.path.exists(stt_py), stt_py)
        r = subprocess.run([stt_py, "-c", screen._WHISPER_CHILD, wav_path,
                            screen.WHISPER_MODEL, screen.WHISPER_LANG],
                           capture_output=True, text=True, timeout=600)
        segs = None
        if r.returncode == 0:
            try:
                segs = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                segs = None
        check("E1a-1 数组路径转写不抛且输出可解析",
              isinstance(segs, list),
              f"rc={r.returncode} err={(r.stderr or '')[-200:]}")
        if isinstance(segs, list):
            struct_ok = all(
                isinstance(s, dict)
                and isinstance(s.get("start"), (int, float))
                and isinstance(s.get("end"), (int, float))
                and isinstance(s.get("text"), str)
                for s in segs)
            check("E1a-2 转写段结构 {start,end,text} 正确", struct_ok,
                  f"segs={segs!r:.200}")
        # ---- E1b: 生产 whisper_check 端到端（skipped = 降级 = 本例判失败） ----
        wc = screen.whisper_check(wav_path)
        check("E1b whisper_check 端到端 status=ok",
              wc.get("status") == "ok", f"wc={str(wc)[:200]}")

    # ---- E2: ffmpeg loudnorm ----
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", wav_path, "-af",
         "loudnorm=print_format=summary", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120)
    out = (r.stderr or "") + (r.stdout or "")
    check("E2a loudnorm(summary) 输出含 Input Integrated",
          r.returncode == 0 and "Input Integrated" in out,
          f"rc={r.returncode}")
    lufs = screen.loudness_lufs(wav_path)
    check("E2b loudness_lufs(json 口径) 解析出数值",
          isinstance(lufs, float) and -60.0 < lufs < 0.0, f"lufs={lufs!r}")

    # ---- E3: 核心模块导入 + 示例配置解析 ----
    import shutil
    for src_name, dst_name in (("config.example.json", "config.json"),
                               ("persona.example.json", "persona.json"),
                               ("facts.example.json", "facts.json")):
        shutil.copy(os.path.join(ROOT, src_name), os.path.join(td, dst_name))
    os.makedirs(os.path.join(td, "state"), exist_ok=True)
    bridge = None
    try:
        bridge = load_mod("bridge_sbx", os.path.join(ROOT, "bridge.py"))
        cfg = bridge.load_config()
        ok_cfg = cfg.get("tz") == "Asia/Shanghai" and "llm" in cfg
    except Exception as e:
        ok_cfg = False
        check("E3a 示例配置可解析", False, repr(e)[:160])
    else:
        check("E3a 示例配置可解析", ok_cfg)
    if bridge is not None:
        mods_ok = True
        detail = ""
        for name in ("llm", "tune", "proactive", "worker"):
            try:
                load_mod(f"{name}_sbx", os.path.join(ROOT, f"{name}.py"))
            except Exception as e:
                mods_ok, detail = False, f"{name}: {e!r:.120}"
                break
        check("E3b 核心模块（llm/tune/proactive/worker）可导入", mods_ok, detail)

    # ---- E4: bridge ctypes EVP（AES-128-ECB） ----
    import ctypes.util
    libname = ctypes.util.find_library("crypto")
    if os.name == "nt":
        # Windows 的 find_library("crypto") 口径不可靠：改断言 bridge 的
        # 候选加载（含 CPython 自带 libcrypto-3.dll）确实能加载成功
        try:
            _lib_ok = bridge is not None and bridge._libcrypto() is not None
        except Exception:
            _lib_ok = False
        check("E4a libcrypto 可加载（Windows 候选路径）", _lib_ok,
              f"find_library={libname!r}")
    else:
        check("E4a find_library('crypto') 命中", bool(libname), f"{libname!r}")
    if bridge is not None:
        key = bytes(range(16))
        data = "env smoke 明文 roundtrip ✓".encode("utf-8") + b"\x00\xff" * 4
        try:
            ct = bridge._aes_ecb_encrypt(data, key)
            pt = bridge._aes_ecb_decrypt(ct, key)
            rt_ok = pt == data
        except Exception as e:
            ct, rt_ok = b"", False
            check("E4b EVP 加解密往返一致", False, f"抛错 {e!r:.160}")
        else:
            check("E4b EVP 加解密往返一致", rt_ok)
        if ct:
            r = subprocess.run(
                ["openssl", "enc", "-aes-128-ecb", "-K", key.hex()],
                input=data, capture_output=True, timeout=30)
            check("E4c 密文与 openssl CLI 路径逐字节等价",
                  r.returncode == 0 and r.stdout == ct,
                  f"rc={r.returncode}")
            try:
                bridge._aes_ecb_decrypt(ct, bytes(16))
                wrong_ok = False
            except RuntimeError:
                wrong_ok = True
            except Exception:
                wrong_ok = False
            check("E4d 错钥解密抛 RuntimeError（填充校验）", wrong_ok)

    n_pass = sum(1 for _, ok in RESULTS if ok)
    print(f"\n== env_smoke: {n_pass}/{len(RESULTS)} PASS ==")
    return 0 if n_pass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
