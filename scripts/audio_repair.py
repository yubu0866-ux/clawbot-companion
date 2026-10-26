#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音轨半自动修复工具：接在 audio_screen 的 suspect 之后，把
「取同轨干净段替换问题段＋交叉淡化＋归一化＋复检」这条修复路径
自动化到「无台词」场景。

用途：与 audio_screen.py 配套使用——质检判 suspect 的音轨先经
本工具尝试自动修复，修不好或不宜自动修的再交人工处置。

依赖：ffmpeg / ffprobe、numpy；同目录的 audio_screen.py（复用其
screen 检测与判据）；可选 faster-whisper（经 audio_screen 转写复核）。

流程：
  1) 先 screen（含转写复核）：pass → already_pass，不动文件；
  2) suspect 且判定有说话 → manual_required，绝不自动改——人物说话
     段不得被删、不得整轨替换（技术边界：替换会破坏台词内容与
     口型/语义对应，只能人工处置）；
  3) 无台词 suspect → 以同轨最长干净窗循环替换全部命中段（边界
     30ms 交叉淡化），loudnorm 到 -16 LUFS 后复检：pass →
     repaired_pass；仍 suspect → still_suspect（产物删除，后续处理
     由调用方决定）。

说话判定双锁：调用方声明（调用方最了解素材构成，生成侧知道哪些
镜头带台词）＋转写实证，任一指向有说话即人工；转写不可用且调用
方未声明无台词时同样保守走人工。

用法：
  audio_repair.py <媒体文件> [--no-speech|--expect-speech] [-o 输出]
                  [--no-whisper]
输出 JSON：{status, file?, detail, hits_before, ...}
退出码：0=already_pass/repaired_pass，2=manual_required/still_suspect，
1=错误。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "audio_screen", os.path.join(_HERE, "audio_screen.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

XFADE_SEC = 0.03
MIN_CLEAN_SEC = 0.5
MAX_HIT_COVER = 0.6


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _real_speech(tr):
    """转写是否含真实台词：True=有、False=确认没有、None=未知。
    只剩笑声类标记/标点/空白不算台词。"""
    if not isinstance(tr, dict) or tr.get("status") != "ok":
        return None
    text = str(tr.get("text") or "")
    for mk in S.LAUGH_MARKERS:
        text = text.replace(mk, "")
    text = "".join(ch for ch in text
                   if ch.isalnum() or "一" <= ch <= "鿿")
    return len(text) >= 2


def _has_video_stream(path):
    try:
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=index", "-of", "csv=p=0", path],
                timeout=30)
    except Exception:
        return False
    return bool((r.stdout or "").strip())


def _decode_wav(path, tmpdir):
    out = os.path.join(tmpdir, "work.wav")
    r = run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", out])
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("ffmpeg 解码音轨失败: " + (r.stderr or "")[:200])
    import wave
    with wave.open(out, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError(f"仅支持 16bit wav 解码，实际 sampwidth={sw}")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return x.reshape(-1, ch), sr


def _write_wav(path, x, sr):
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def _replace_hits(x, sr, hits):
    """以最长干净窗循环替换命中段，边界交叉淡化。返回 (新样本, 说明)。
    无可用干净窗返回 (None, 原因)。"""
    n = len(x)
    dur = n / sr
    spans = []
    for s, e in hits:
        s = max(0.0, float(s) - 0.1)
        e = min(dur, float(e) + 0.1)
        if e > s:
            spans.append((s, e))
    if not spans:
        return None, "无有效命中段"
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    covered = sum(e - s for s, e in merged)
    if covered > dur * MAX_HIT_COVER:
        return None, f"命中覆盖 {covered:.1f}s/{dur:.1f}s 过大，无从取干净段"
    clean = []
    prev = 0.0
    for s, e in merged:
        if s > prev:
            clean.append((prev, s))
        prev = max(prev, e)
    if prev < dur:
        clean.append((prev, dur))
    if not clean:
        return None, "全轨皆命中，无干净窗"
    w0, w1 = max(clean, key=lambda w: w[1] - w[0])
    if w1 - w0 < MIN_CLEAN_SEC:
        return None, f"最长干净窗仅 {w1 - w0:.2f}s，不足 {MIN_CLEAN_SEC}s"
    wi0, wi1 = int(w0 * sr), int(w1 * sr)
    win = x[wi0:wi1]
    out = x.copy()
    xf = max(1, int(XFADE_SEC * sr))
    for s, e in merged:
        i0, i1 = int(s * sr), int(e * sr)
        out[i0:i1] = win[np.arange(i1 - i0) % len(win)]
        # 边界交叉淡化：命中段已向外扩 0.1s 边距，边界区是干净床面，
        # 与替换段在首/末 xf 内线性互淡即可消阶跃咔哒，不会把命中
        # 内容淡回（命中本体距边界 ≥0.1s）。
        k = min(xf, i1 - i0)
        if k > 1:
            wgt = np.linspace(0.0, 1.0, k)[:, None]
            out[i0:i0 + k] = x[i0:i0 + k] * (1 - wgt) + out[i0:i0 + k] * wgt
            out[i1 - k:i1] = out[i1 - k:i1] * (1 - wgt) + x[i1 - k:i1] * wgt
    return out, f"干净窗 {w0:.2f}-{w1:.2f}s 替换 {len(merged)} 段"


def repair(path, out_path=None, expect_speech=None, use_whisper=True):
    res0 = S.screen(path, use_whisper=use_whisper)
    base = {"file": os.path.abspath(path), "hits_before": res0.get("hits"),
            "loudness_before": res0.get("loudness_lufs")}
    if res0.get("verdict") == "pass":
        return {**base, "status": "already_pass", "detail": "初筛即 pass"}
    real = _real_speech(res0.get("transcript") or {})
    if expect_speech is True or real is True:
        return {**base, "status": "manual_required",
                "detail": "判定含人物说话，自动修复禁用（说话段不得删/换）"}
    if expect_speech is None and real is None:
        return {**base, "status": "manual_required",
                "detail": "说话与否未知（转写不可用且调用方未声明无台词），保守人工"}
    hits = [(h.get("start"), h.get("end")) for h in res0.get("hits") or []
            if h.get("start") is not None and h.get("end") is not None]
    if not hits:
        return {**base, "status": "still_suspect",
                "detail": "suspect 但无可定位命中段（仅转写标记类），不自动修"}
    root, ext = os.path.splitext(path)
    if out_path is None:
        out_path = root + "-fixed" + (ext or ".mp4")
    with tempfile.TemporaryDirectory() as td:
        x, sr = _decode_wav(path, td)
        fixed, why = _replace_hits(x, sr, hits)
        if fixed is None:
            return {**base, "status": "still_suspect", "detail": why}
        fixed_wav = os.path.join(td, "fixed.wav")
        _write_wav(fixed_wav, fixed, sr)
        tmp_out = os.path.join(td, "out" + (ext or ".mp4"))
        if _has_video_stream(path):
            r = run(["ffmpeg", "-v", "error", "-y", "-i", path,
                     "-i", fixed_wav, "-map", "0:v:0", "-map", "1:a:0",
                     "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                     "-shortest", tmp_out])
        else:
            acodec = ["-c:a", "pcm_s16le"] if ext.lower() == ".wav" \
                else ["-c:a", "aac", "-b:a", "160k"]
            r = run(["ffmpeg", "-v", "error", "-y", "-i", fixed_wav,
                     "-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] + acodec + [tmp_out])
        if r.returncode != 0 or not os.path.exists(tmp_out):
            return {**base, "status": "error",
                    "detail": "重组失败: " + (r.stderr or "")[:200]}
        os.replace(tmp_out, out_path)
    res1 = S.screen(out_path, use_whisper=use_whisper)
    if res1.get("verdict") == "pass":
        return {**base, "status": "repaired_pass", "detail": why,
                "out": os.path.abspath(out_path),
                "loudness_after": res1.get("loudness_lufs")}
    try:
        os.remove(out_path)
    except OSError:
        pass
    return {**base, "status": "still_suspect",
            "detail": "替换后复检仍 suspect：" + why,
            "hits_after": res1.get("hits")}


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        print("用法: audio_repair.py <媒体文件> [--no-speech|--expect-speech] "
              "[-o 输出] [--no-whisper]", file=sys.stderr)
        return 1
    path = args[0]
    if not os.path.exists(path):
        print("文件不存在: " + path, file=sys.stderr)
        return 1
    expect = None
    if "--expect-speech" in flags:
        expect = True
    elif "--no-speech" in flags:
        expect = False
    oi = argv.index("-o") if "-o" in argv else -1
    out = argv[oi + 1] if oi >= 0 else None
    try:
        res = repair(path, out_path=out, expect_speech=expect,
                     use_whisper="--no-whisper" not in flags)
    except Exception as e:
        print(json.dumps({"status": "error", "detail": str(e)[:240]},
                         ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["status"] in ("already_pass", "repaired_pass") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
