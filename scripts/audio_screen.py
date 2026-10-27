#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音轨质检工具：检测视频/音频中是否混入「无故笑声」型声纹，
并提供响度归一化（--normalize）。

用途：对生成的或待发布的音视频做交付前音轨质检——笑声型声纹
命中即判 suspect，调用方据此决定修复、重制或降级处理；也可单独
用 --normalize 把响度归一化到 -16 LUFS。

依赖：ffmpeg / ffprobe（提取音轨、响度测量与归一化）、numpy；
可选 faster-whisper（转写复核层，缺失时自动跳过、只走声纹判据）。

判据（声纹三判据）：一段窗口内同时出现
  1) 人声频段（300–3400Hz）能量占比相对全片基线陡升；
  2) 断续爆发峰（短时能量在 1 秒内多次冲高回落）；
  3) 高过零率窗口聚集（笑声的气声/爆发特性）。
三样凑齐且持续 ≥0.4s → suspect。
响度门与爆发计数采用局部基线口径（技术理由：全局中位会被片中
其它响段抬高，中等强度笑声在响床上会被整体漏过）：候选帧的响度
参照取其邻域（±1.5s、排除中心 ±0.3s）的局部中位，帧峰值须达局部
基线 1.6 倍；爆发计数的冲高/回落阈以候选段邻域 rms 基线为尺度
（段内 p90 仅在邻域不可用时回落沿用）。
可选 faster-whisper 转写复核：
转写文本命中笑声类标记（笑声/哈哈/呵呵/[laughter] 等）的段落同样
按段标 suspect 并入 hits；转写不可用时只走声纹并注明（降级不挡结论）。
转写走数组路径：ffmpeg 解 16k 单声道 wav → numpy 数组喂 transcribe，
不按文件路径转写（部分环境下 av 与 faster-whisper 版本不兼容，
路径转写会崩溃，数组输入可绕开）。

用法：
  audio_screen.py <媒体文件> [--no-whisper] [--json]
  audio_screen.py <媒体文件> --normalize [-o 输出文件]

输出 JSON：{verdict: pass|suspect, hits: [{start,end,score,reasons}],
            loudness_lufs, transcript: {...}, file}
退出码：0=pass，2=suspect，1=错误。
--normalize：loudnorm 到 -16 LUFS / TP -1.5，写新文件（不覆盖原文件）。
"""
import json

import os
import subprocess
import sys
import tempfile

import numpy as np

SR = 16000
FRAME = 512            # 32ms
HOP = 256              # 16ms
VOCAL_LO, VOCAL_HI = 300.0, 3400.0
MIN_SEG_SEC = 0.4
# 转写子进程解释器：默认当前解释器；若 faster-whisper 装在独立
# 虚拟环境里，用环境变量 AUDIO_SCREEN_STT_PYTHON 指向该环境的 python。
STT_PYTHON = os.environ.get("AUDIO_SCREEN_STT_PYTHON") or sys.executable
# 转写模型与语言可用环境变量覆盖，默认 tiny / zh。
WHISPER_MODEL = os.environ.get("AUDIO_SCREEN_WHISPER_MODEL", "tiny")
WHISPER_LANG = os.environ.get("AUDIO_SCREEN_WHISPER_LANG", "zh")
LAUGH_MARKERS = ("[laughter]", "[laughing]", "(laughter)", "(laughing)",
                 "笑声", "哈哈", "呵呵", "嘿嘿")


def run(cmd, **kw):
    kw.setdefault("encoding", "utf-8")  # Windows 区域编码会误解码 ffmpeg 输出
    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def extract_wav(path, tmpdir):
    out = os.path.join(tmpdir, "audio.wav")
    r = run(["ffmpeg", "-v", "error", "-y", "-i", path,
             "-vn", "-ac", "1", "-ar", str(SR), out])
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("ffmpeg 提取音轨失败: " + (r.stderr or "")[:200])
    return out


def load_pcm(wav):
    import wave
    with wave.open(wav, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return x


def frame_features(x):
    n_frames = max(0, (len(x) - FRAME) // HOP + 1)
    if n_frames == 0:
        return None
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(FRAME)[None, :]
    rms = np.sqrt(np.mean(x[idx] ** 2, axis=1) + 1e-12)
    peak = np.max(np.abs(x[idx]), axis=1)
    zcr = np.mean(np.abs(np.diff(np.signbit(x[idx]).astype(np.int8), axis=1)), axis=1)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SR)
    band = (freqs >= VOCAL_LO) & (freqs <= VOCAL_HI)
    band_ratio = spec[:, band].sum(axis=1) / (spec.sum(axis=1) + 1e-12)
    times = (np.arange(n_frames) * HOP + FRAME / 2) / SR
    return {"t": times, "rms": rms, "peak": peak, "zcr": zcr, "band": band_ratio}


def local_baseline(v, half_win_s=1.5, excl_s=0.3):
    """逐帧局部基线：邻域（±half_win_s、排除中心 ±excl_s）样本的中位。

    邻域不足时先放宽排除窗、再回落全局中位；全静音/极短序列不崩。
    """
    n = len(v)
    if n == 0:
        return v.copy()
    gmed = float(np.median(v))
    fsec = HOP / SR
    W = max(1, int(half_win_s / fsec))
    C = int(excl_s / fsec)
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        lo, hi = max(0, i - W), min(n, i + W + 1)
        a = v[lo:max(lo, i - C)]
        b = v[min(hi, i + C + 1):hi]
        if len(a) + len(b) >= 4:
            out[i] = np.median(np.concatenate([a, b]))
            continue
        rest = np.concatenate([v[lo:i], v[i + 1:hi]])
        out[i] = float(np.median(rest)) if len(rest) else gmed
    return out


def _context_median(v, i0, i1, span_s=1.5):
    """候选段 [i0, i1) 两侧邻域（各 span_s）的中位；样本不足返回 None。"""
    fsec = HOP / SR
    w = int(span_s / fsec)
    parts = []
    if i0 > 0:
        parts.append(v[max(0, i0 - w):i0])
    if i1 < len(v):
        parts.append(v[i1:min(len(v), i1 + w)])
    ctx = np.concatenate(parts) if parts else np.array([])
    if len(ctx) >= 8:
        return float(np.median(ctx))
    return None


def burst_count(rms, i0, i1, base=None):
    """窗口内断续爆发峰计数：冲高/回落交替计峰。

    尺度优先用段邻域局部基线 base（局部基线口径：hi=1.30×base、
    lo=1.10×base）——段内 p90 定阈在响床上会被床面本身顶穿（床面
    高于 lo 时爆发全并成一个峰，中等强度笑声只数到 1，实测如此）。
    base 不可用（邻域样本不足）时回落段内 p90 尺度（hi=0.55×p90、
    lo=0.35×p90；不用段内中位数：笑声可占近半帧、中位会被抬高）。
    """
    seg = rms[i0:i1]
    if len(seg) < 5:
        return 0
    if base is not None and base > 1e-6:
        hi, lo = base * 1.30, base * 1.10
    else:
        scale = float(np.percentile(seg, 90))
        if scale <= 1e-6:
            return 0
        hi, lo = scale * 0.55, scale * 0.35
    peaks = 0
    above = False
    for v in seg:
        if v > hi and not above:
            peaks += 1
            above = True
        elif v < lo:
            above = False
    return peaks


def detect_laughter(feat):
    t, rms, zcr, band = feat["t"], feat["rms"], feat["zcr"], feat["band"]
    peak = feat["peak"]
    if len(t) == 0:
        return []
    base_band = float(np.median(band))
    # 候选帧：人声占比陡升 + 响度达局部基线 1.6 倍（帧峰值对邻域中位，
    # 局部基线口径见文件头）+ 过零率偏高。绝对下限 0.05 防静默段噪声
    # 误报（原 rms 门的 0.02 下限在峰值统计下的对应护栏）。
    loc_peak = local_baseline(peak)
    cand = (band > max(base_band + 0.15, base_band * 1.8)) & (peak > np.maximum(loc_peak * 1.6, 0.05)) & (zcr > 0.10)
    hits = []
    i = 0
    fsec = HOP / SR
    while i < len(t):
        if not cand[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j + 1 < len(t) and gap <= int(0.3 / fsec):
            j += 1
            gap = 0 if cand[j] else gap + 1
        end = j - gap
        dur = (end - i) * fsec
        if dur >= MIN_SEG_SEC:
            seg_band = float(np.mean(band[i:end + 1]))
            seg_zcr = float(np.percentile(zcr[i:end + 1], 75))
            nb = burst_count(rms, i, end + 1,
                             base=_context_median(rms, i, end + 1))
            reasons = []
            score = 0.0
            if seg_band > base_band + 0.15:
                reasons.append(f"人声频段占比 {seg_band:.2f} 高于基线 {base_band:.2f}")
                score += 0.4
            if nb >= 3:
                reasons.append(f"1 秒级断续爆发峰 {nb} 次")
                score += 0.4
            if seg_zcr > 0.12:
                reasons.append(f"过零率聚集 {seg_zcr:.2f}")
                score += 0.2
            if score >= 0.8:
                hits.append({"start": round(float(t[i]), 2),
                             "end": round(float(t[end]), 2),
                             "score": round(score, 2), "reasons": reasons})
        i = max(end + 1, i + 1)
    return hits


def loudness_lufs(path):
    r = run(["ffmpeg", "-v", "info", "-i", path, "-af",
             "loudnorm=print_format=json", "-f", "null", "-"])
    txt = (r.stderr or "") + (r.stdout or "")
    k = txt.rfind('"input_i"')
    if k >= 0:
        seg = txt[k:txt.find("}", k) + 1]
        try:
            return float(seg.split('"input_i"')[1].split('"')[1])
        except Exception:
            return None
    return None


# 子进程脚本：在转写解释器内把 wav 读成 numpy float32 数组直接喂
# transcribe——数组输入不走 av 的 decode_audio，绕开部分环境下 av 与
# faster-whisper 版本不兼容的 TypeError(metadata_errors)；不要改回
# 按文件路径转写。模型名与语言由 argv[2]/argv[3] 传入。
_WHISPER_CHILD = (
    "import json,sys,wave\n"
    "import numpy as np\n"
    "from faster_whisper import WhisperModel\n"
    "with wave.open(sys.argv[1], 'rb') as w:\n"
    "    raw = w.readframes(w.getnframes())\n"
    "x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0\n"
    "m = WhisperModel(sys.argv[2], device='cpu', compute_type='int8')\n"
    "segs, _info = m.transcribe(x, language=sys.argv[3])\n"
    "out = [{'start': round(float(s.start), 2), 'end': round(float(s.end), 2),"
    " 'text': s.text} for s in segs]\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)


def whisper_check(path):
    """转写复核：ffmpeg 解码 → numpy 数组 → faster-whisper。

    返回 {status: ok|skipped, segments: [{start,end,text}], text,
    laughter_markers}；venv/模型/解码任一不可用 → skipped 并注明原因，
    只降级复核层，不影响声纹结论。
    """
    if not os.path.exists(STT_PYTHON):
        return {"status": "skipped", "reason": "转写解释器不存在: " + STT_PYTHON}
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = extract_wav(path, td)
            r = run([STT_PYTHON, "-c", _WHISPER_CHILD, wav,
                     WHISPER_MODEL, WHISPER_LANG], timeout=600)
        if r.returncode != 0:
            return {"status": "skipped", "reason": (r.stderr or "")[-160:]}
        segs = json.loads(r.stdout.strip().splitlines()[-1])
        if not isinstance(segs, list):
            return {"status": "skipped", "reason": "转写输出格式异常"}
        segs = [s for s in segs if isinstance(s, dict)]
        markers = sorted({mk for s in segs for mk in LAUGH_MARKERS
                          if mk in str(s.get("text", ""))})
        return {"status": "ok", "laughter_markers": markers,
                "segments": segs,
                "text": " ".join(str(s.get("text", "")) for s in segs).strip()}
    except Exception as e:  # 转写只是复核，失败不挡声纹结论
        return {"status": "skipped", "reason": str(e)[:160]}


def has_audio_stream(path):
    """ffprobe 探音频流：True/False；探针本身失败返回 None（未知，
    交由后续提取路径照常报错，不在此吞掉损坏文件）。"""
    try:
        r = run(["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", path],
                timeout=30)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return bool((r.stdout or "").strip())


def screen(path, use_whisper=True):
    if has_audio_stream(path) is False:
        # 无音轨视频：没有声音即没有笑声可言——若不在此短路，ffmpeg
        # 提取必失败被判成 error（退出码 1），与「损坏文件」混为一谈，
        # 调用方按失败处理会误杀正常的静默视频（边界样本实测）。
        return {"file": os.path.abspath(path), "verdict": "pass",
                "hits": [], "loudness_lufs": None,
                "transcript": {"status": "skipped", "reason": "无音轨"},
                "note": "no_audio_track"}
    with tempfile.TemporaryDirectory() as td:
        wav = extract_wav(path, td)
        x = load_pcm(wav)
        feat = frame_features(x)
        if feat is None:
            raise RuntimeError("音轨过短，无法分析")
        hits = detect_laughter(feat)
    lufs = loudness_lufs(path)
    tr = {"status": "skipped", "reason": "--no-whisper"}
    if use_whisper:
        tr = whisper_check(path)
        if tr.get("status") == "ok":
            for s in tr.get("segments") or []:
                found = sorted({mk for mk in LAUGH_MARKERS
                                if mk in str(s.get("text", ""))})
                if found:
                    hits.append({"start": s.get("start"), "end": s.get("end"),
                                 "score": 0.6,
                                 "reasons": ["转写出现笑声类标记: "
                                             + ",".join(found)]})
    verdict = "suspect" if hits else "pass"
    return {"file": os.path.abspath(path), "verdict": verdict, "hits": hits,
            "loudness_lufs": lufs, "transcript": tr}


def normalize(path, out_path=None):
    if out_path is None:
        root, ext = os.path.splitext(path)
        out_path = root + "-norm" + (ext or ".mp4")
    r = run(["ffmpeg", "-v", "error", "-y", "-i", path,
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", out_path])
    if r.returncode != 0:
        raise RuntimeError("loudnorm 失败: " + (r.stderr or "")[:200])
    return out_path


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        print("用法: audio_screen.py <媒体文件> [--no-whisper] [--normalize [-o 输出]]",
              file=sys.stderr)
        return 1
    path = args[0]
    if not os.path.exists(path):
        print("文件不存在: " + path, file=sys.stderr)
        return 1
    try:
        if "--normalize" in flags:
            oi = argv.index("-o") if "-o" in argv else -1
            out = normalize(path, argv[oi + 1] if oi >= 0 else None)
            print(json.dumps({"normalized": out,
                              "loudness_lufs": loudness_lufs(out)},
                             ensure_ascii=False))
            return 0
        res = screen(path, use_whisper="--no-whisper" not in flags)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 2 if res["verdict"] == "suspect" else 0
    except Exception as e:
        print(json.dumps({"error": str(e)[:240]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
