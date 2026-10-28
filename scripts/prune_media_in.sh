#!/usr/bin/env bash
# prune_media_in.sh — state/media_in 收件媒体原件保留策略（保守优先）
#
# 只清理“已有文字留档”的原件：某 message_id 在 history.jsonl 中已有
# img_desc 或 vid_desc，才有资格被清理；**没有文字留档的原件永不删**。
# 两条清理路径（都要先满足“有留档”）：
#   1) 年龄：该消息全部文件的最新 mtime 距今 > AGE_DAYS 天（默认 90）；
#   2) 总量兜底：media_in 总量 > CAP_BYTES（默认 500MB）时，从最旧的
#      已留档原件开始清，直到总量回到上限以内。
# 一条消息的文件按组处理（图片 = 单文件或多图 .i<n> 组；视频 = mp4 +
# .f<n>.jpg 帧图，整组同删）。转写文本随 history 记录留存、不落文件。
# 只碰 media_in/，state 其他文件一律不动。
#
# 用法：
#   scripts/prune_media_in.sh            # 默认 dry-run：只打印将清理清单
#   scripts/prune_media_in.sh --apply    # 真正删除
# 可调环境变量（自测用）：COMPANION_HOME、MEDIA_IN_DIR、HISTORY_FILE、
#   PRUNE_AGE_DAYS、PRUNE_CAP_BYTES、PRUNE_NOW（epoch 秒，覆盖“现在”）

set -u

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

BRIDGE_HOME="${COMPANION_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MEDIA_IN_DIR="${MEDIA_IN_DIR:-$BRIDGE_HOME/state/media_in}"
export HISTORY_FILE="${HISTORY_FILE:-$BRIDGE_HOME/state/history.jsonl}"
export PRUNE_AGE_DAYS="${PRUNE_AGE_DAYS:-90}"
export PRUNE_CAP_BYTES="${PRUNE_CAP_BYTES:-524288000}"
export PRUNE_APPLY="$APPLY"
export PRUNE_NOW="${PRUNE_NOW:-}"

exec python3 - <<'PYEOF'
import json, os, re, sys, time

media_in = os.environ["MEDIA_IN_DIR"]
hist_file = os.environ["HISTORY_FILE"]
age_days = float(os.environ["PRUNE_AGE_DAYS"])
cap = int(os.environ["PRUNE_CAP_BYTES"])
apply_mode = os.environ["PRUNE_APPLY"] == "1"
now = float(os.environ["PRUNE_NOW"]) if os.environ.get("PRUNE_NOW") else time.time()

if not os.path.isdir(media_in):
    print(f"prune_media_in: media_in 不存在，跳过：{media_in}")
    sys.exit(0)

# 1) 有文字留档的 message_id 集合
described = set()
if os.path.exists(hist_file):
    with open(hist_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("img_desc") or row.get("vid_desc"):
                if row.get("message_id"):
                    described.add(str(row["message_id"]))

# 2) 按 message_id 分组：<id>.<ext>、<id>.f<n>.<ext>（视频帧图）、
#    <id>.i<n>.<ext>（同条消息的第 n 张图，多图全收）
pat = re.compile(r"^(\d+)(?:\.(?:f|i)\d+)?\.[^.]+$")
groups = {}
total = 0
for name in sorted(os.listdir(media_in)):
    path = os.path.join(media_in, name)
    if not os.path.isfile(path):
        continue
    m = pat.match(name)
    if not m:
        continue  # 不认识的文件名，不碰
    st = os.stat(path)
    total += st.st_size
    g = groups.setdefault(m.group(1), {"files": [], "size": 0, "newest": 0.0})
    g["files"].append((name, st.st_size))
    g["size"] += st.st_size
    g["newest"] = max(g["newest"], st.st_mtime)

age_cutoff = now - age_days * 86400

# 3) 判定候选（两条路径都要求有留档）
candidates = {}  # id -> reason
for mid, g in groups.items():
    if mid not in described:
        continue
    if g["newest"] < age_cutoff:
        candidates[mid] = f"超过 {age_days:g} 天且已有文字留档"
if total > cap:
    remaining = total
    for mid in sorted(groups, key=lambda k: groups[k]["newest"]):
        if remaining <= cap:
            break
        if mid not in described:
            continue  # 没留档的永不删，哪怕总量超限
        if mid not in candidates:
            candidates[mid] = "总量兜底（超过上限，从最旧的已留档原件清）"
        remaining -= groups[mid]["size"]

mode = "APPLY" if apply_mode else "DRY-RUN"
print(f"prune_media_in [{mode}]: 文件 {sum(len(g['files']) for g in groups.values())} 个 / "
      f"{len(groups)} 条消息，总量 {total/1048576:.1f}MB（上限 {cap/1048576:.0f}MB），"
      f"有文字留档 {len(described)} 条")
if not candidates:
    print("prune_media_in: 将清理 0 组")
    sys.exit(0)

freed = 0
for mid in sorted(candidates, key=lambda k: groups[k]["newest"]):
    g = groups[mid]
    freed += g["size"]
    names = ", ".join(n for n, _ in g["files"])
    print(f"  {'DELETE' if apply_mode else 'WOULD-DELETE'} {mid} "
          f"({g['size']/1024:.0f}KB; {candidates[mid]}): {names}")
    if apply_mode:
        for name, _ in g["files"]:
            os.remove(os.path.join(media_in, name))
print(f"prune_media_in: {'已清理' if apply_mode else '将清理'} {len(candidates)} 组，"
      f"共 {freed/1048576:.2f}MB")
PYEOF
