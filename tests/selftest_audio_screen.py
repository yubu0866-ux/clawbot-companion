#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audio_screen 自检：纯音乐 pass / 爆发笑声型 suspect / 正常说话 pass /
中强度笑声（局部基线）suspect / 静音与极短音频边界 pass /
转写路径真实走通（数组喂 whisper）/ 空转写不误判 / 转写标记按段并入 hits。"""
import importlib.util
import os
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SR = 16000
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"（{detail}）" if detail and not cond else ""))


def load_screen():
    spec = importlib.util.spec_from_file_location(
        "audio_screen", os.path.join(ROOT, "scripts", "audio_screen.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_repair():
    spec = importlib.util.spec_from_file_location(
        "audio_repair", os.path.join(ROOT, "scripts", "audio_repair.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def save_wav(path, x):
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def music_bed(dur=6.0, amp=0.25):
    t = np.arange(int(SR * dur)) / SR
    env = 0.8 + 0.2 * np.sin(2 * np.pi * 0.5 * t)
    return amp * env * (np.sin(2 * np.pi * 220 * t) + 0.6 * np.sin(2 * np.pi * 277 * t)
                        + 0.4 * np.sin(2 * np.pi * 330 * t))


def speech_like(dur=6.0):
    t = np.arange(int(SR * dur)) / SR
    env = 0.7 + 0.3 * np.sin(2 * np.pi * 1.3 * t) ** 2
    x = np.zeros_like(t)
    for k in range(1, 14):
        x += (0.55 / k) * np.sin(2 * np.pi * 130 * k * t + 0.3 * k)
    return 0.5 * env * x / np.max(np.abs(x))


def laughter_like():
    rng = np.random.default_rng(7)
    x = music_bed(6.0, amp=0.10)
    t = np.arange(int(SR * 6.0)) / SR
    start, end = 2.2, 3.8
    noise = rng.standard_normal(len(t))
    # 带通到人声频段
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(len(t), 1 / SR)
    spec[(freqs < 400) | (freqs > 3200)] = 0
    buzz = np.fft.irfft(spec, len(t))
    seg = (t >= start) & (t < end)
    gate = np.zeros(len(t))
    tt = t[seg] - start
    gate[seg] = (np.sin(2 * np.pi * 5.2 * tt) > 0.2).astype(float)
    laugh = (1.0 * buzz / (np.max(np.abs(buzz)) + 1e-9)
             + 0.15 * np.sign(np.sin(2 * np.pi * 190 * t))) * gate
    x = x + laugh * seg
    return x / max(1.0, np.max(np.abs(x)))


def bed2(dur=8.0, amp=0.05):
    # 第四轮校准样本集的低频床（与 gen_samples 同构造）
    t = np.arange(int(SR * dur)) / SR
    return amp * (np.sin(2 * np.pi * 110 * t) + 0.7 * np.sin(2 * np.pi * 165 * t)
                  + 0.5 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 440 * t)) \
        * (0.85 + 0.15 * np.sin(2 * np.pi * 0.2 * t))


def laugh_burst(dur_total, start, end, amp, seed=7):
    # 气声带通噪声笑声段（5.2Hz 门控），与第四轮校准样本同构造
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * dur_total)) / SR
    noise = rng.standard_normal(len(t))
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(len(t), 1 / SR)
    spec[(freqs < 400) | (freqs > 3200)] = 0
    buzz = np.fft.irfft(spec, len(t))
    buzz = buzz / (np.max(np.abs(buzz)) + 1e-9)
    gate = np.zeros(len(t))
    seg = (t >= start) & (t < end)
    tt = t[seg] - start
    gate[seg] = (np.sin(2 * np.pi * 5.2 * tt) > 0.2).astype(float)
    return amp * buzz * gate


def medium_laugh():
    # 中等强度笑声：笑声幅值 ≈ 床幅参数 4.4 倍（0.22/0.05），注入段 3.0–4.6s。
    # 旧全局中位 rms 门下声纹层 0 帧入选漏检（第四轮量化）；局部基线后须检出。
    return bed2(8.0) + laugh_burst(8.0, 3.0, 4.6, 0.22)


def _discover_stt_python():
    """通用版转写解释器发现：AUDIO_SCREEN_STT_PYTHON 优先，其次当前
    解释器，再次常见 venv 布局里能 import faster_whisper 的解释器。
    都没有 → 返回 None（转写项 SKIP：可选依赖未装不算代码失败）。"""
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


STT_PY = _discover_stt_python()
if STT_PY:
    os.environ["AUDIO_SCREEN_STT_PYTHON"] = STT_PY


def main():
    scr = load_screen()
    with tempfile.TemporaryDirectory() as td:
        p_music = os.path.join(td, "music.wav")
        p_speech = os.path.join(td, "speech.wav")
        p_laugh = os.path.join(td, "laugh.wav")
        save_wav(p_music, music_bed())
        save_wav(p_speech, speech_like())
        save_wav(p_laugh, laughter_like())
        r1 = scr.screen(p_music, use_whisper=False)
        check("纯音乐 pass", r1["verdict"] == "pass", str(r1["hits"]))
        # 无音轨视频：pass + note，不得混作损坏 error（第四轮边界样本）
        p_noaudio = os.path.join(td, "noaudio.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=black:s=160x120:d=1", "-an",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", p_noaudio],
                       check=True)
        r0 = scr.screen(p_noaudio, use_whisper=False)
        check("无音轨视频 pass+note", r0["verdict"] == "pass"
              and r0.get("note") == "no_audio_track", str(r0)[:160])
        r2 = scr.screen(p_speech, use_whisper=False)
        check("正常说话 pass", r2["verdict"] == "pass", str(r2["hits"]))
        r3 = scr.screen(p_laugh, use_whisper=False)
        check("笑声型 suspect", r3["verdict"] == "suspect", str(r3["hits"]))
        hit_ok = any(h["start"] is not None and h["start"] < 3.2 and h["end"] > 2.0
                     for h in r3["hits"])
        check("笑声命中点落在注入段", hit_ok, str(r3["hits"]))
        # 中等强度笑声（≈床幅 4.4 倍）：局部基线口径下声纹层必须检出
        p_med = os.path.join(td, "medium_laugh.wav")
        save_wav(p_med, medium_laugh())
        r6 = scr.screen(p_med, use_whisper=False)
        check("中强度笑声 suspect（局部基线）", r6["verdict"] == "suspect",
              str(r6["hits"]))
        med_ok = any(h["start"] is not None and h["start"] < 4.6 and h["end"] > 3.0
                     for h in r6["hits"])
        check("中强度命中点落在注入段", med_ok, str(r6["hits"]))
        # 边界：全静音与极短音频不崩、判 pass
        p_sil = os.path.join(td, "silence.wav")
        save_wav(p_sil, np.zeros(SR * 5))
        r7 = scr.screen(p_sil, use_whisper=False)
        check("全静音 pass", r7["verdict"] == "pass", str(r7["hits"]))
        t_short = np.arange(int(SR * 0.3)) / SR
        p_short = os.path.join(td, "short.wav")
        save_wav(p_short, 0.2 * np.sin(2 * np.pi * 440 * t_short))
        r8 = scr.screen(p_short, use_whisper=False)
        check("极短音频 pass 不崩", r8["verdict"] == "pass", str(r8["hits"]))
        check("响度可测", isinstance(r1["loudness_lufs"], float), str(r1["loudness_lufs"]))
        out = scr.normalize(p_music, os.path.join(td, "music-norm.wav"))
        check("normalize 产出新文件", os.path.exists(out) and out != p_music)
        # 真实转写路径：ffmpeg→numpy 数组→faster-whisper（不走 av 解码）。
        # 纯音乐经 tiny 模型转写为空段落——status 必须 ok 而非 skipped，
        # 且空转写不得误判 suspect、不得产生转写类 hit。
        r4 = scr.screen(p_music, use_whisper=True)
        tr4 = r4.get("transcript") or {}
        if STT_PY:
            check("转写路径真实走通", tr4.get("status") == "ok", str(tr4)[:160])
        else:
            print("SKIP 转写路径真实走通（本机无可用的 faster-whisper 解释器）")
        tr_hit4 = any("转写" in " ".join(h.get("reasons") or []) for h in r4["hits"])
        check("空转写不误判 suspect",
              r4["verdict"] == "pass" and not tr_hit4
              and not tr4.get("laughter_markers")
              and not (tr4.get("text") or ""),
              str({"hits": r4["hits"], "tr": tr4})[:200])
        # 转写标记命中 → 按段并入 hits（替身注入转写层，验合并逻辑）
        _orig_wc = scr.whisper_check
        scr.whisper_check = lambda p: {
            "status": "ok", "laughter_markers": ["哈哈"],
            "segments": [{"start": 1.0, "end": 2.0, "text": "哈哈真好笑"}],
            "text": "哈哈真好笑"}
        try:
            r5 = scr.screen(p_music, use_whisper=True)
        finally:
            scr.whisper_check = _orig_wc
        tr_hit5 = any(h.get("start") == 1.0
                      and "转写" in " ".join(h.get("reasons") or [])
                      for h in r5["hits"])
        check("转写标记按段并入 hits", r5["verdict"] == "suspect" and tr_hit5,
              str(r5["hits"]))
        # ---- 半自动修复（audio_repair，台账 #28 选项 5b，第八轮落地） ----
        rep = load_repair()
        _orig_wc2 = rep.S.whisper_check
        rep.S.whisper_check = lambda p: {"status": "skipped", "reason": "test"}
        try:
            ra = rep.repair(p_laugh,
                            out_path=os.path.join(td, "laugh-fixed.wav"),
                            expect_speech=False, use_whisper=False)
            check("半自动修复：无台词笑声段替换后复检 pass",
                  ra["status"] == "repaired_pass"
                  and bool(ra.get("out")) and os.path.exists(ra["out"]),
                  str(ra)[:200])
            rb = rep.repair(p_laugh,
                            out_path=os.path.join(td, "laugh-manual.wav"),
                            expect_speech=True, use_whisper=False)
            check("半自动修复：声明有台词 → 人工且不产出文件",
                  rb["status"] == "manual_required"
                  and not os.path.exists(os.path.join(td, "laugh-manual.wav")),
                  str(rb)[:160])
            rc = rep.repair(p_music, expect_speech=False, use_whisper=False)
            check("半自动修复：初筛 pass → already_pass",
                  rc["status"] == "already_pass", str(rc)[:160])
            rd = rep.repair(p_laugh, expect_speech=None, use_whisper=False)
            check("半自动修复：说话未知（转写不可用+未声明）→ 保守人工",
                  rd["status"] == "manual_required", str(rd)[:160])
        finally:
            rep.S.whisper_check = _orig_wc2
        rep.S.whisper_check = lambda p: {
            "status": "ok", "laughter_markers": [],
            "segments": [{"start": 0.5, "end": 2.0,
                          "text": "你回来啦今天累不累"}],
            "text": "你回来啦今天累不累"}
        try:
            re2 = rep.repair(p_laugh, expect_speech=False, use_whisper=True)
            check("半自动修复：转写实证台词覆盖无台词声明 → 人工",
                  re2["status"] == "manual_required", str(re2)[:160])
        finally:
            rep.S.whisper_check = _orig_wc2
    print(f"\n==== audio_screen selftest: {sum(1 for _, c in RESULTS if c)}/{len(RESULTS)} PASS ====")
    return 0 if all(c for _, c in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
