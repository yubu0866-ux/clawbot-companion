#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站视觉质检的一键机器检（与 audio_screen 并列）。

docs/media-and-tts.md「出站视觉质检」分两层：机器检（解码/规格/
音轨门）与视觉判断（脸与参考图一致、穿搭一致、畸变/乱码）。本
脚本把机器检收成一个命令，省掉执行体分别手工跑 ffprobe/ffmpeg
与 audio_screen 的步骤：

  media_screen.py <媒体文件> [--frames-dir DIR] [--no-audio]

- 图片：ffprobe 取流信息 + ffmpeg 整图试解码（解不出即不过）。
- 视频：ffprobe 核对时长>0、含视频流、编码/分辨率、文件大小
  （50KB<大小<50MB，默认值，部署者可按自家口径改下面两个常量）；
  抽首/中/尾三帧到 frames 目录供执行体打开逐项看；有音轨时自动
  串同目录 audio_screen.py 过音轨门，其 verdict 并入结论
  （--no-audio 跳过）。
- 输出 JSON：{file, kind, ok, checks, frames, audio_screen}。
  ok 只表达机器检结论（解码/规格/音轨门）；脸与参考图一致、穿搭
  一致、畸变/乱码等视觉判断仍由执行体打开成品与抽帧逐项看——
  本脚本不冒充视觉判断。

依赖：ffmpeg / ffprobe 在 PATH 中；被串的 audio_screen.py 另需
numpy、可选 faster-whisper。
退出码：0=ok，2=机器检不过，1=错误（用法/文件不存在/ffprobe 缺失）。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_SCREEN = os.path.join(HERE, "audio_screen.py")
MIN_VIDEO_BYTES = 50 * 1024
MAX_BYTES = 50 * 1024 * 1024


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _probe(path):
    proc = _run(["ffprobe", "-v", "error", "-show_streams",
                 "-show_format", "-of", "json", path], timeout=60)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


def _decode_ok(path):
    proc = _run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                timeout=120)
    return proc.returncode == 0


def _extract_frame(path, at_sec, out_jpg):
    proc = _run(["ffmpeg", "-v", "error", "-ss", f"{at_sec:.2f}",
                 "-i", path, "-frames:v", "1", "-y", out_jpg], timeout=60)
    if proc.returncode != 0 or not os.path.exists(out_jpg):
        return False
    try:
        with open(out_jpg, "rb") as f:
            return f.read(2) == b"\xff\xd8"
    except OSError:
        return False


def _audio_screen(path):
    """串 audio_screen 音轨门：返回 (结论 dict, 是否 suspect/错误)。"""
    try:
        proc = _run([sys.executable, AUDIO_SCREEN, path, "--json"],
                    timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": False, "error": f"{type(e).__name__}"}, True
    try:
        res = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return {"ran": True, "error": "unparsable_output",
                "rc": proc.returncode}, True
    bad = res.get("verdict") != "pass"
    return {"ran": True, "verdict": res.get("verdict"),
            "loudness_lufs": res.get("loudness_lufs"),
            "hits": len(res.get("hits") or [])}, bad


def screen(path, frames_dir=None, with_audio=True):
    out = {"file": os.path.abspath(path), "kind": None, "ok": False,
           "checks": {}, "frames": [], "audio_screen": None}
    if not os.path.isfile(path):
        out["checks"]["exists"] = False
        return out
    size = os.path.getsize(path)
    out["checks"]["size_bytes"] = size
    meta = _probe(path)
    if meta is None:
        out["checks"]["probe"] = False
        return out
    out["checks"]["probe"] = True
    streams = meta.get("streams") or []
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = meta.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not vstreams:
        out["checks"]["has_video_stream"] = False
        return out
    out["checks"]["has_video_stream"] = True
    vs = vstreams[0]
    out["checks"]["codec"] = vs.get("codec_name")
    out["checks"]["width"] = vs.get("width")
    out["checks"]["height"] = vs.get("height")
    # 单帧图片（图像编码 + 容器时长≈0）按图片检；有真实时长的按
    # 视频检。GIF 动图时长>0 会走视频路（抽帧+规格），可接受。
    is_image = duration <= 0.05 and vs.get("codec_name") in (
        "mjpeg", "png", "webp", "gif", "bmp", "tiff")
    out["kind"] = "image" if is_image else "video"
    out["checks"]["decode"] = _decode_ok(path)
    if not out["checks"]["decode"]:
        return out
    if is_image:
        out["checks"]["size_ok"] = size < MAX_BYTES
        out["ok"] = bool(out["checks"]["size_ok"])
        return out
    out["checks"]["duration_sec"] = round(duration, 2)
    out["checks"]["duration_ok"] = duration > 0
    out["checks"]["size_ok"] = MIN_VIDEO_BYTES < size < MAX_BYTES
    if not (out["checks"]["duration_ok"] and out["checks"]["size_ok"]):
        return out
    # 抽首/中/尾帧供执行体看脸漂移等视觉项；默认目录 <文件>.frames
    frames_dir = frames_dir or (os.path.abspath(path) + ".frames")
    try:
        os.makedirs(frames_dir, exist_ok=True)
    except OSError:
        frames_dir = None
    if frames_dir:
        for label, frac in (("first", 0.05), ("mid", 0.5), ("last", 0.95)):
            fp = os.path.join(frames_dir, f"{label}.jpg")
            if _extract_frame(path, max(duration * frac, 0.0), fp):
                out["frames"].append(fp)
    out["checks"]["frames_ok"] = len(out["frames"]) == 3
    if not out["checks"]["frames_ok"]:
        return out
    # 音轨门：无音轨不串（无轨可查），有音轨必过 audio_screen
    if not astreams:
        out["audio_screen"] = {"ran": False, "note": "no_audio_stream"}
        out["ok"] = True
        return out
    if with_audio:
        res, bad = _audio_screen(path)
        out["audio_screen"] = res
        out["ok"] = not bad
    else:
        out["audio_screen"] = {"ran": False, "note": "skipped_by_flag"}
        out["ok"] = True
    return out


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    frames_dir = None
    if "--frames-dir" in argv:
        i = argv.index("--frames-dir")
        frames_dir = argv[i + 1] if i + 1 < len(argv) else None
    if len(args) != 1:
        print(json.dumps({"error": "usage: media_screen.py <file> "
                                   "[--frames-dir DIR] [--no-audio]"},
                         ensure_ascii=False))
        return 1
    try:
        res = screen(args[0], frames_dir=frames_dir,
                     with_audio="--no-audio" not in argv)
    except Exception as e:  # 工具自身异常也要给结论，不许静默
        print(json.dumps({"error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
