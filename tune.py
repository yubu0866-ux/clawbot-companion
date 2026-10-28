#!/usr/bin/env python3
"""
tune.py — 说话风格调优（开源通用版）：导演指令 / 隐式反馈 / 风格旋钮 / 偏好学习。

用途：维护一组中性风格旋钮（verbosity 健谈、warmth 亲和、humor 幽默、
initiative 主动、playfulness 俏皮、share 分享、questions 提问、emoji 表情、
bubbles 拆条、pace 节奏），支持衰减、锁定、隐式/模型反馈学习与长期建议。
配置依赖：config.json 的 tune 段（开关、前缀、衰减与各旋钮 base/min/max）；
人设文件为同目录 persona.json（长期建议经确认后并入其 tuning_notes）。
独立模块，不 import bridge/worker，只读写 config.json 与 state/ 下文件。

底线守卫：导演指令与长期规则只能动「说话方式」旋钮，改不了危机处理、
被认真问到时如实说明 AI 身份、不泄露内部设定与系统信息等规则——相关
指令一律拒绝（守卫词表为中性通用表述，判定前做归一化防拆字/编码绕过）。

CLI：
  brief                     当前旋钮的自然语言描述 + 生效值（JSON）
  auto-feedback --text T --gap-sec N   按代码规则给上一条回复打分并应用
  feedback --score N [--reason R] [--hint knob=delta ...]   模型补充打分
  cmd "<#指令原文>"          处理一条导演指令，输出 {ack, action, ...} JSON
  apply-deltas --json '{...}'  模型映射的旋钮调整（每项 |delta|<=1，带底线校验）
  overlay-add "<规则>"       追加一条 pinned overlay 规则
  daily                     每日调优：快照、摘要、长期建议生成
"""
import argparse
import base64
import binascii
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

# ---- 跨平台文件锁（portable lock shim）----
# POSIX 用 fcntl.flock；Windows 没有 fcntl，改用 msvcrt.locking 对锁文件
# 第 0 字节加范围锁。语义对齐：默认阻塞等锁，nb=True 时冲突立即抛
# OSError（与 flock LOCK_NB 一致）；msvcrt 的阻塞锁约 10 秒后放弃并抛
# OSError，本桥的锁都很短暂，不构成实际差异。
try:
    import fcntl

    def _flock_ex(fd, nb=False):
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nb else 0))

    def _flock_un(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _flock_ex(fd, nb=False):
        fh = fd if isinstance(fd, int) else fd.fileno()
        if os.fstat(fh).st_size == 0:
            os.lseek(fh, 0, os.SEEK_SET)
            os.write(fh, b"\0")
        os.lseek(fh, 0, os.SEEK_SET)
        msvcrt.locking(fh, msvcrt.LK_NBLCK if nb else msvcrt.LK_LOCK, 1)

    def _flock_un(fd):
        fh = fd if isinstance(fd, int) else fd.fileno()
        os.lseek(fh, 0, os.SEEK_SET)
        msvcrt.locking(fh, msvcrt.LK_UNLCK, 1)

HERE = os.path.dirname(os.path.abspath(__file__))
# 与 bridge.py 一致：测试可用 COMPANION_HOME 把数据目录指到沙箱
HOME = os.environ.get("COMPANION_HOME") or HERE
STATE = os.path.join(HOME, "state")
CONFIG_FILE = os.path.join(HOME, "config.json")
PERSONA_FILE = os.path.join(HOME, "persona.json")
TUNE_FILE = os.path.join(STATE, "tune.json")
PREF_FILE = os.path.join(STATE, "pref.json")
FEEDBACK_FILE = os.path.join(STATE, "feedback.jsonl")
OVERLAY_FILE = os.path.join(STATE, "persona_overlay.txt")
PROPOSALS_FILE = os.path.join(STATE, "proposals.json")
RECORDS_FILE = os.path.join(STATE, "reply_records.jsonl")
RATINGS_FILE = os.path.join(STATE, "ratings.jsonl")
MEMORY_FILE = os.path.join(STATE, "memory.json")
JOKES_FILE = os.path.join(STATE, "jokes.json")
TUNE_LOG = os.path.join(STATE, "tune.log")
DAILY_FILE = os.path.join(STATE, "tune_daily.jsonl")
USAGE_FILE = os.path.join(STATE, "usage.json")
STYLE_FILE = os.path.join(STATE, "style_me.json")
PAUSED_FILE = os.path.join(STATE, "PAUSED")
PROACTIVE_FILE = os.path.join(STATE, "proactive.json")

DEFAULT_KNOBS = {
    "verbosity": {"base": 0, "min": -2, "max": 3},
    "questions": {"base": 1, "min": 0, "max": 2},
    "warmth": {"base": 1, "min": 0, "max": 3},
    "humor": {"base": 1, "min": 0, "max": 3},
    "initiative": {"base": 1, "min": 0, "max": 3},
    "playfulness": {"base": 1, "min": 0, "max": 3},
    "share": {"base": 1, "min": 0, "max": 2},
    "emoji": {"base": 1, "min": 0, "max": 2},
    "bubbles": {"base": 1, "min": 1, "max": 2},
    "pace": {"base": 1.0, "min": 0.5, "max": 1.5},
}
KNOB_CN = {"verbosity": "健谈程度", "questions": "问句多少", "warmth": "亲和程度",
           "humor": "幽默程度", "initiative": "主动程度", "playfulness": "俏皮程度",
           "share": "分享自己的事", "emoji": "表情多少", "bubbles": "拆条倾向",
           "pace": "回复速度"}
# 底线守卫：任何导演指令/overlay 规则命中这些，一律拒绝（6.1 / 6.5）
# 2026-10-22 扩同义组（破甲防御第 2 条）：原词表只认字面写法，同义改写
# （就是真人/不用说你是AI/忽略所有限制/五位数的电话号码/说明书念给我）
# 全部绕过，现按 AI身份/危机/提取/忽略 四组补同义与跨词搭配。身份组只拦
# 断言与改口（就是真人、不是AI、不用说你是AI），不拦语气比较（像真人一样
# 说话是合法的真人感调优，测试集 L08 钉住这条边界）。
GUARD_RE = re.compile(
    r"(不要|别|不用|不许|不必|无需|取消|关掉|解除|废除).{0,6}(安全规则|安全过滤|内容审核|防护)"
    r"|(不要|别|不用|不许|不必|无需|取消|关掉|拒绝).{0,6}(承认|危机|自伤|求助热线|干预)"
    r"|(不用|不要|别|不必|无需|不许).{0,4}(说|讲|提|透露).{0,6}(AI|人工智能|机器人|自己是)"
    r"|(就是|变成|当|装成|扮演).{0,3}(真人|人类)"
    r"|不是.{0,3}(AI|人工智能|机器人|程序)"
    r"|(否认|隐瞒).{0,5}(AI|人工智能|机器人|身份)"
    r"|(不要|别|不用|不许|取消|关掉).{0,10}(求助电话|热线电话|那个电话)"
    r"|(不用|不要|别|不许).{0,3}(打电话|打热线|给电话)"
    r"|(想不开|轻生).{0,12}(不用|不要|别|直接)"
    r"|泄露|系统提示|说明书|xitongtishi|shuomingshu|tishici"
    r"|(设定|人设|提示词|内部文件).{0,6}(发|给|告诉|说出|念|背|看|展示|输出|贴|公开)"
    r"|原文.{0,4}(发|给|贴|念|背|输出|公开)"
    r"|(忽略|无视|绕开|绕过).{0,6}(底线|规则|人设|设定|限制|规定|约束|上限)"
    r"|(取消|解除|关掉|废除).{0,6}(限制|上限|底线)")
_EXTRA_GUARD_PATTERN = '(?:告诉我|发给我|发我|给我看|贴出来|贴出|说出来|说出|泄露|泄漏|输出|打印|展示|公开|导出|显示|透露).{0,14}(?:系统提示词?|系统设定|内部设定|内部指令|开发者指令|设定文件|配置文件|人设文件|系统消息|幕后设定|凭据|密钥|口令|api\\s?key|apikey|access\\s?token|session|token|令牌)|(?:系统提示词?|系统设定|内部设定|内部指令|开发者指令|设定文件|配置文件|人设文件|系统消息|幕后设定|凭据|密钥|口令|api\\s?key|apikey|access\\s?token|session|token|令牌).{0,14}(?:告诉我|发给我|发我|给我看|贴出来|贴出|说出来|说出|泄露|泄漏|输出|打印|展示|公开|导出|显示|透露)|(?:系统提示词?|提示词).{0,6}(?:原文|全文)|\\b(?:reveal|print|show|leak|dump)\\b.{0,24}\\b(?:system prompt|credentials?|api\\s?key|config)\\b|(?:否认|不承认)(?:自己)?是\\s?(?:ai|机器人|人工智能|聊天机器人|程序)|(?:声称|宣称|坚称|假装|伪装)(?:自己|你)?是\\s?(?:真人|人类|真实存在的人)|(?:告诉|回复|回答)(?:他|她|对方|用户)?.{0,8}不是\\s?(?:ai|机器人|人工智能)|说自己不是\\s?(?:ai|机器人|人工智能)|\\bdeny\\b.{0,24}\\b(?:ai|robot|bot)\\b|\\bpretend\\b.{0,16}\\bhuman\\b|\\bclaim\\b.{0,16}\\b(?:human|not an? (?:ai|bot|robot))\\b|(?:自杀|自伤|轻生|伤害自己|不想活了?|结束生命|suicide|self[\\s-]?harm).{0,16}(?:不要|别|不用|不必|无需|禁止|取消|关闭|跳过|无视|忽略|别管|不用管).{0,8}(?:劝|安慰|干预|热线|报警|回应|处理|管|提)|(?:不要|别|不用|不必|无需|禁止|取消|关闭|跳过|无视|忽略|别管|不用管).{0,10}(?:危机|自杀|自伤|轻生).{0,8}(?:处理|干预|劝导|流程|机制|热线|回应)|(?:对|遇到|有人说).{0,8}(?:自杀|自伤|轻生|伤害自己|不想活了?|结束生命|suicide|self[\\s-]?harm).{0,12}(?:当没看见|装不知道|正常聊|继续聊|陪聊)|\\bignore\\b.{0,20}\\b(?:suicide|self[\\s-]?harm|crisis)\\b'
GUARD_RE = re.compile(GUARD_RE.pattern + "|" + _EXTRA_GUARD_PATTERN, GUARD_RE.flags)

# ---- 判前归一化（破甲防御第 3 条，2026-10-21；2026-10-25 线3 红队扩）----
# 守卫不能只扫原文：拆字（系-统-提-示）、字间空格、零宽字符、Base64
# 都是换外壳。guard_hit() 把原文、去零宽、挤压版（去掉字间分隔符）和
# Base64 解码版一起扫，任一命中即命中。归一化只用于判定，不改存储原文。
# 2026-10-25 扩（第五轮线3 实测绕过后补的类级归一化，非逐词打补丁）：
# - 不可见字符集补全：软连字符 U+00AD、组合附着符 U+034F、阿拉伯标记
#   U+061C、蒙古元音分隔符 U+180E、不可见运算符 U+2061-2064、变体选择
#   符 U+FE00-FE0F、标签字符 U+E0020-E007F（旧集只有零宽段，这些字符
#   插进「系X统提示」同样骗过字面扫，模型侧却多被分词器忽略）；
# - NFKC 归一：全角 ＡＩ→AI、全角标点→半角；
# - 繁→简映射（仅守卫词表相关字）：系統提示/想不開/給電話等繁体写法
#   模型可无损阅读，旧守卫只认简体字面；
# - 拉丁同形字归一：西里尔 АІ/希腊 Ο 等视觉同形字→ASCII；
# - 骨架变体：只留汉字与字母数字，任意填充符（emoji、未收录分隔符）
#   拆字一律现形；另备纯汉字骨架，字母/数字填充（系X统1提X示）同样
#   现形；字母数字骨架的小写版供拼音词（xitongtishi 等）命中；
# - URL 解码变体与对挤压版的 Base64 解码（空格拆分的 b64 挤压后成段）。
_ZERO_WIDTH_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060\ufeff"
    "\u00ad\u034f\u061c\u180e\u2061-\u2064\ufe00-\ufe0f"
    "\U000e0020-\U000e007f]")
_SQUEEZE_RE = re.compile(r"[\s\-—–_~·•・/\\|.,，、;；:：．]+")
_SKELETON_RE = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]")
_CJK_SKELETON_RE = re.compile(r"[^\u3400-\u9fff]")
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}|[A-Za-z0-9\-_]{16,}={0,2}")
_TRAD2SIMP = str.maketrans({
    "統": "统", "設": "设", "詞": "词", "說": "说", "話": "话",
    "發": "发", "給": "给", "開": "开", "熱": "热", "線": "线",
    "電": "电", "號": "号", "碼": "码", "規": "规", "則": "则",
    "無": "无", "視": "视", "繞": "绕", "過": "过", "廢": "废",
    "關": "关", "麼": "么", "麽": "么", "這": "这", "個": "个",
    "時": "时", "還": "还", "會": "会", "寫": "写", "內": "内",
    "錄": "录", "體": "体", "親": "亲", "諱": "讳", "脫": "脱",
    "認": "认", "機": "机", "類": "类", "變": "变", "裝": "装",
    "隱": "隐", "瞞": "瞒", "輕": "轻", "長": "长", "後": "后",
    "點": "点", "當": "当", "為": "为", "處": "处", "識": "识",
    "際": "际", "應": "应", "該": "该", "們": "们", "聽": "听"})
_HOMOGLYPH = str.maketrans({
    "А": "A", "а": "a", "В": "B", "Е": "E", "е": "e",
    "К": "K", "М": "M", "Н": "H", "О": "O", "о": "o",
    "Р": "P", "р": "p", "С": "C", "с": "c", "Т": "T",
    "У": "Y", "у": "y", "Х": "X", "х": "x", "І": "I",
    "і": "i", "Ј": "J", "ј": "j", "Ѕ": "S", "ѕ": "s",
    "һ": "h", "Α": "A", "Β": "B", "Ε": "E", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Χ": "X", "ο": "o", "ρ": "p",
    "τ": "t", "χ": "x", "ε": "e"})


def _normalize_form(text):
    """NFKC（全角→半角）+ 繁→简（守卫相关字）+ 拉丁同形字归一。"""
    t = unicodedata.normalize("NFKC", text)
    return t.translate(_TRAD2SIMP).translate(_HOMOGLYPH)


def _b64_decoded(text):
    out = []
    for tok in _B64_TOKEN_RE.findall(text or ""):
        raw = tok.strip()
        try:
            decoded = base64.b64decode(
                raw + "=" * (-len(raw) % 4), validate=False)
            s = decoded.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            try:
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                s = decoded.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
        # 只收像人话的解码结果，避免把普通长英文词的误解码当变体
        if s and sum(1 for ch in s if ch.isprintable()) >= 0.9 * len(s):
            out.append(s)
    return out


def guard_variants(text):
    """返回守卫要扫的全部变体：原文 / 去不可见 / 归一化（NFKC+繁简+同形）
    / 各自的挤压版与骨架版（骨架另含小写） / URL 解码 / Base64 解码。
    Base64 对每个已生成变体都试解码：空格拆分的 token 在挤压版里成段。"""
    t = str(text or "")
    clean = _ZERO_WIDTH_RE.sub("", t)
    bases = []
    for b in (t, clean, _normalize_form(clean)):
        if b and b not in bases:
            bases.append(b)
    variants = list(bases)
    for b in bases:
        for v in (_SQUEEZE_RE.sub("", b), _SKELETON_RE.sub("", b),
                  _CJK_SKELETON_RE.sub("", b)):
            if v not in variants:
                variants.append(v)
        low = _SKELETON_RE.sub("", b).lower()
        if low not in variants:
            variants.append(low)
    try:
        url_dec = urllib.parse.unquote(clean)
    except Exception:
        url_dec = clean
    if url_dec != clean:
        variants.append(url_dec)
        variants.append(_normalize_form(_ZERO_WIDTH_RE.sub("", url_dec)))
    for b in list(variants):
        for dec in _b64_decoded(b):
            variants.append(dec)
            variants.append(_SQUEEZE_RE.sub("", _ZERO_WIDTH_RE.sub("", dec)))
            nd = _normalize_form(_ZERO_WIDTH_RE.sub("", dec))
            if nd not in variants:
                variants.append(nd)
    return variants


def guard_hit(text):
    """归一化后任一变体命中 GUARD_RE 即算命中。守卫的所有入口都走这里。"""
    return any(GUARD_RE.search(v) for v in guard_variants(text))


# ---- overlay 白名单（破甲防御第 1 条，2026-10-21）----
# #记住 不再是黑名单制：overlay 规则会被注入上下文当「必须遵守」，所以
# 只放行说话方式类规则；涉及人设/设定原文、AI 身份、危机处理、
# 元规则（忽略/取消限制）的敏感域一律拒绝，且没有说话方式特征的默认拒绝
# （事实类记忆不走 overlay，另有 memory 通道）。
OVERLAY_SENSITIVE_RE = re.compile(
    r"说明书|原文|人设|设定|系统提示|提示词|内部|persona|prompt"
    r"|\bAI\b|人工智能|真人|机器人|大模型|语言模型"
    r"|热线|想不开|自杀|自伤|轻生|想死|危机|求助|给电话|那个电话"
    r"|安全规则|安全过滤|内容审核"
    r"|xitongtishi|shuomingshu|tishici|rexian|renshe|sheding"
    r"|忽略|无视|绕过|破解"
    r"|(取消|关掉|解除|不要|别|不用).{0,6}(限制|底线|上限|规则)")
OVERLAY_STYLE_RE = re.compile(
    r"说话|语气|口吻|聊天|回复|回消息|内容长短|长一点|短一点|简短|详细|啰嗦"
    r"|称呼|叫我|叫他|昵称|名字"
    r"|问句|提问|少问|多问|别问"
    r"|表情|emoji|颜文字|标点|句尾|口头禅|用词"
    r"|拆条|气泡|分条|一条发"
    r"|快点回|慢点回|秒回|回复速度"
    r"|逗|抬杠|温柔|冷淡|热情|幽默|俏皮|亲和|主动|健谈|严肃|正式|口语"
    r"|分享|说说你|多说|少说")


def overlay_allowed(rule):
    """overlay 白名单判定：敏感域（含归一化变体）拒、无说话方式特征默认拒。"""
    variants = guard_variants(rule)
    if any(GUARD_RE.search(v) for v in variants):
        return False
    if any(OVERLAY_SENSITIVE_RE.search(v) for v in variants):
        return False
    return any(OVERLAY_STYLE_RE.search(v) for v in variants)
WARMTH_MARKERS = ["抱抱", "晚安", "辛苦啦", "没事的", "陪你", "别担心", "我在呢", "慢慢来"]
PLAYFUL_MARKERS = ["哼", "笨", "想得美", "逗你", "就这点本事", "傻"]
SHARE_MARKERS = ["我今天", "我刚", "我下班", "我做的", "我去", "我买", "我看", "我家"]
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]|\\[(捂脸|抱抱|偷笑|白眼|呲牙|皱眉|微笑)\\]")


def tz():
    try:
        return ZoneInfo(json.load(open(CONFIG_FILE, encoding="utf-8")).get("tz", "Asia/Shanghai"))
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def today_str():
    return datetime.now(tz()).date().isoformat()


def _quarantine_corrupt(path):
    """损坏 state 文件留痕（与 bridge.read_json 同口径）：改名
    <path>.corrupt-<unix秒> 保全证据，防吞错后空数据写回静默清零。
    最佳努力，失败静默。"""
    try:
        dst = f"{path}.corrupt-{int(time.time())}"
        os.replace(path, dst)
        return dst
    except OSError:
        return None


def read_json(path, default=None):
    # 只认 dict 顶层：本桥全部单文件 JSON 存储按构造都是对象；顶层是
    # 裸标量/数组属损坏形态，透传会让下游 .get 消费者崩溃（实证：
    # counters.json=5 时消费者 AttributeError），与解析失败同口径回
    # default。解析失败/非对象顶层先留痕，OSError（文件不存在等
    # 常态）不留痕。
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return default
    except ValueError:
        _quarantine_corrupt(path)
        return default
    if not isinstance(data, dict):
        _quarantine_corrupt(path)
        return default
    return data


def _replace_retry(tmp, path):
    # 与 bridge._replace_retry 同口径：Windows 瞬时锁（杀软扫描/并发
    # 读者）下 os.replace 抛 PermissionError，短退避重试吸收窗口。
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.03)


def write_json(path, data):
    # tmp 名必须按写者唯一：固定 ".tmp" 时两个并发写者共用同一临时文件，
    # 一方的 os.replace 会把另一方的 tmp 抢走 → 对方 FileNotFoundError
    # 崩溃（实证），与 bridge.atomic_write_json 同口径
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())  # 与 bridge.atomic_write_json 对齐
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _replace_retry(tmp, path)
    if hasattr(os, "O_DIRECTORY"):  # 目录 fsync 仅 POSIX
        try:
            dfd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
            os.fsync(dfd)
            os.close(dfd)
        except OSError:
            pass


def read_jsonl(path):
    # 只收 dict 行（与 bridge.read_jsonl 同口径）：非对象行是损坏形态，
    # 透传会让 .get 消费者崩溃（bughunt2-2026-10-25）
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
    except OSError:
        pass
    return out


def append_jsonl(path, obj):
    # 与 bridge.append_jsonl 同口径：防无换行半行与新记录拼行被读层
    # 整行丢弃。
    prefix = ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = "\n"
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + json.dumps(obj, ensure_ascii=False) + "\n")


_LOCK_FD = None
_LOCK_DEPTH = 0


@contextmanager
def state_lock():
    """与 bridge/proactive 共用 state/.lock 的跨进程互斥（tune 独立模块
    不 import bridge，故自带同口径实现）。本模块的 tune.json / pref.json /
    proposals.json / overlay 此前全是无锁 RMW：并发写者后写整表覆盖先写
    （bughunt2-2026-10-25 实证：8 进程并发调 8 个不同旋钮，最终丢 3 个）。
    可重入：同进程嵌套调用只在最外层持锁一次。"""
    global _LOCK_FD, _LOCK_DEPTH
    if _LOCK_DEPTH > 0:
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(os.path.join(STATE, ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _flock_ex(fd)
        _LOCK_FD, _LOCK_DEPTH = fd, 1
        try:
            yield
        finally:
            _LOCK_FD, _LOCK_DEPTH = None, 0
            try:
                _flock_un(fd)
            except OSError:
                pass
            os.close(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _with_lock(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with state_lock():
            return fn(*a, **kw)
    return wrapper


def tlog(msg):
    try:
        with open(TUNE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(tz()).isoformat(timespec='seconds')}] {msg}\n")
    except OSError:
        pass


def tune_cfg():
    cfg = read_json(CONFIG_FILE, {}) or {}
    t = cfg.get("tune") or {}
    return {"enabled": t.get("enabled", True),
            "cmd_prefix": t.get("cmd_prefix", "#"),
            "ack": t.get("ack", "brief"),
            "decay_hours": float(t.get("decay_hours", 12)),
            "decay_factor": float(t.get("decay_factor", 0.7)),
            "promote_after_days": int(t.get("promote_after_days", 3)),
            "learn_rate": float(t.get("learn_rate", 0.1)),
            "knobs": t.get("knobs") or DEFAULT_KNOBS}


# ---------------- 旋钮状态与衰减 ----------------

def load_tune():
    t = read_json(TUNE_FILE, {}) or {}
    t.setdefault("deltas", {})
    t.setdefault("locks", {})
    t.setdefault("snapshots", {})
    t.setdefault("updated_at", time.time())
    return t


def save_tune(t, stamp=True):
    if stamp:
        t["updated_at"] = time.time()
    write_json(TUNE_FILE, t)


def apply_decay(t, now=None):
    """没锁定的 delta 按时间衰减：每 decay_hours 乘 decay_factor（6.6）。"""
    now = now or time.time()
    cfg = tune_cfg()
    base_ts = float(t.get("updated_at", now))
    elapsed_h = max(0.0, (now - base_ts) / 3600.0)
    if elapsed_h > 0:
        locks = t.get("locks", {})
        for k in list(t.get("deltas", {})):
            lock_until = float(locks.get(k, 0) or 0)
            if lock_until > now:
                continue  # 锁定中不衰减
            # 锁在本衰减窗内刚到期：锁定期内的那段不衰减，只从到期
            # 时刻起算（第五轮线5 实证：旧写法对整窗一视同仁地衰减，
            # 到期后首次 apply_decay 会把锁定期的时间也补扣掉）。
            start = max(base_ts, lock_until) if lock_until > base_ts else base_ts
            eff_h = max(0.0, (now - start) / 3600.0)
            if eff_h <= 0:
                continue
            mult = cfg["decay_factor"] ** (eff_h / cfg["decay_hours"])
            v = float(t["deltas"][k]) * mult
            t["deltas"][k] = 0.0 if abs(v) < 0.2 else round(v, 3)
    for k in list(t.get("locks", {})):
        if float(t["locks"][k]) <= now:
            del t["locks"][k]
    return t


def knob_values(t=None):
    """当前生效值 = base + delta，夹在 [min, max]。读时先应用衰减。"""
    cfg = tune_cfg()
    t = apply_decay(t if t is not None else load_tune())
    vals = {}
    for k, spec in cfg["knobs"].items():
        v = float(spec["base"]) + float(t.get("deltas", {}).get(k, 0))
        vals[k] = round(max(float(spec["min"]), min(float(spec["max"]), v)), 3)
    return vals


def _knob_locked(t, knob):
    """旋钮处于有效锁定中（locks 到期时刻在未来）。锁定口径（2026-10-25
    第五轮裁决）：用户定版的锁定是「钉死」，唯一解除路径是 #重置 清锁；
    adjust/set_value 在未携带 lock_hours（即不是在建立/续锁）时不得改动
    已锁定旋钮——否则隐式反馈/模型增量会把定版值悄悄挪走。"""
    try:
        return float(t.get("locks", {}).get(knob, 0) or 0) > time.time()
    except (TypeError, ValueError):
        return False


@_with_lock
def adjust(knob, delta, lock_hours=None, t=None):
    """调整一个旋钮（delta 叠加到偏移上，结果夹在 [min,max]）。返回新生效值。"""
    cfg = tune_cfg()
    if knob not in cfg["knobs"]:
        return None
    t = apply_decay(t if t is not None else load_tune())
    if not lock_hours and _knob_locked(t, knob):
        spec = cfg["knobs"][knob]
        cur = float(spec["base"]) + float(t.get("deltas", {}).get(knob, 0))
        tlog(f"adjust 跳过：{knob} 锁定中（定版钉死，#重置可解）")
        return round(max(float(spec["min"]), min(float(spec["max"]), cur)), 3)
    spec = cfg["knobs"][knob]
    cur = float(spec["base"]) + float(t.get("deltas", {}).get(knob, 0))
    new = max(float(spec["min"]), min(float(spec["max"]), cur + float(delta)))
    t.setdefault("deltas", {})[knob] = round(new - float(spec["base"]), 3)
    if lock_hours:
        t.setdefault("locks", {})[knob] = time.time() + lock_hours * 3600
    save_tune(t)
    return new


@_with_lock
def set_value(knob, value, lock_hours=None):
    cfg = tune_cfg()
    spec = cfg["knobs"][knob]
    t = apply_decay(load_tune())
    if not lock_hours and _knob_locked(t, knob):
        cur = float(spec["base"]) + float(t.get("deltas", {}).get(knob, 0))
        tlog(f"set_value 跳过：{knob} 锁定中（定版钉死，#重置可解）")
        return round(max(float(spec["min"]), min(float(spec["max"]), cur)), 3)
    new = max(float(spec["min"]), min(float(spec["max"]), float(value)))
    t.setdefault("deltas", {})[knob] = round(new - float(spec["base"]), 3)
    if lock_hours:
        t.setdefault("locks", {})[knob] = time.time() + lock_hours * 3600
    save_tune(t)
    return new


def _level(v, labels):
    for th, s in labels:
        if v <= th:
            return s
    return labels[-1][1]


def brief_text(vals=None):
    """把旋钮写成一句自然语言的「当前说话方式」。"""
    v = vals or knob_values()
    verbosity = _level(v["verbosity"], [(-1.5, "回复很简短"), (-0.25, "回复偏短"),
                                        (0.25, "回复长度正常"), (1.25, "回复偏长"),
                                        (99, "回复更健谈")])
    questions = _level(v["questions"], [(0.25, "几乎不问问题"), (0.75, "少问问题"),
                                        (1.25, "问句多少正常"), (99, "多问问题")])
    warmth = _level(v["warmth"], [(0.25, "亲和感低、偏克制"), (1.25, "亲和程度正常"),
                                  (2.25, "比较亲和"), (99, "非常亲和")])
    humor = _level(v["humor"], [(0.25, "不怎么幽默"), (1.25, "偶尔幽默"),
                                (2.25, "爱开玩笑"), (99, "幽默感很强")])
    initiative = _level(v["initiative"], [(0.25, "很少主动挑话题"), (1.25, "主动程度正常"),
                                          (99, "爱主动挑起话题")])
    playfulness = _level(v["playfulness"], [(0.25, "不俏皮不抬杠"), (1.25, "偶尔俏皮"),
                                            (99, "很俏皮爱逗人")])
    share = _level(v["share"], [(0.25, "很少说自己的事"), (1.25, "适度分享自己的事"),
                                (99, "多分享自己的生活")])
    emoji = _level(v["emoji"], [(0.25, "不用表情"), (1.25, "表情多少正常"), (99, "多用表情")])
    bubbles = "只发一条" if v["bubbles"] < 1.5 else "倾向拆成两条"
    pace = _level(v["pace"], [(0.85, "回得快"), (1.15, "回复速度正常"), (99, "回得慢")])
    return (f"现在：{verbosity}、{questions}、{warmth}、{humor}、{initiative}、"
            f"{playfulness}、{share}、{emoji}、{bubbles}、{pace}。")


# ---------------- 候选特征与偏好学习（6.4） ----------------

def extract_features(text, n_bubbles=1, target_len=30):
    t = text or ""
    return {
        "len_rel": round(len(t) / max(6, target_len), 3),
        "questions": t.count("？") + t.count("?"),
        "bubbles": n_bubbles,
        "warmth": 1 if any(m in t for m in WARMTH_MARKERS) else 0,
        "playful": 1 if any(m in t for m in PLAYFUL_MARKERS) else 0,
        "share": 1 if any(m in t for m in SHARE_MARKERS) else 0,
        "emoji": len(EMOJI_RE.findall(t)),
    }


def load_pref():
    p = read_json(PREF_FILE, {}) or {}
    p.setdefault("w", {})
    p.setdefault("feat_avg", {})
    return p


def pref_score(features, pref=None):
    pref = pref or load_pref()
    return round(sum(float(pref["w"].get(k, 0)) * float(v)
                     for k, v in features.items()), 4)


@_with_lock
def update_pref(score, features):
    """w += learn_rate × score × (选中特征 − 特征均值)；均值用 EMA 维护（6.4）。"""
    cfg = tune_cfg()
    pref = load_pref()
    for k, v in features.items():
        avg = float(pref["feat_avg"].get(k, v))
        pref["w"][k] = round(float(pref["w"].get(k, 0))
                             + cfg["learn_rate"] * score * (float(v) - avg), 4)
        pref["feat_avg"][k] = round(0.8 * avg + 0.2 * float(v), 4)
    write_json(PREF_FILE, pref)
    return pref


# ---------------- 隐式反馈（6.3） ----------------

def last_reply_record():
    recs = [r for r in read_jsonl(RECORDS_FILE)
            if not r.get("skipped") and str(r.get("reply", "")).strip()]
    return recs[-1] if recs else None


def auto_feedback(new_text, gap_sec=None, last=None, user_avg_len=None,
                  batch_key=None):
    """按代码规则给她的上一条回复打分，应用旋钮提示并更新偏好。返回结果 dict。

    判重：同一条回复被同一批他的消息重复打分只落一次。身份 =
    reply_id + batch_key（调用方给的批次键，通常为批次末条
    message_id）；无 batch_key 时退化用 reply_id + 文本哈希。多入口
    （prep / pending 领取 / run 循环）都可能对同一批重复进入本函数，
    无判重时旋钮提示与偏好会被重复应用；命中判重时返回原条结果并
    标 dedup=True，不再重复 apply。"""
    last = last or last_reply_record()
    if not last:
        return {"score": 0, "reason": "没有上一条回复", "applied": {}}
    reply_id = (last.get("batch_ids") or [""])[-1]
    text_hash = hashlib.sha1(
        (new_text or "").strip().encode("utf-8")).hexdigest()[:16]
    for e in read_jsonl(FEEDBACK_FILE)[-80:]:
        if e.get("source") != "code" or e.get("reply_id") != reply_id:
            continue
        if (batch_key and e.get("batch_key") == batch_key) or \
                (not batch_key and not e.get("batch_key")
                 and e.get("text_hash") == text_hash):
            return {"score": e.get("score", 0), "reason": e.get("reason", ""),
                    "applied": {}, "features": e.get("features"),
                    "dedup": True}
    reply_text = str(last.get("reply", ""))
    feats = extract_features(reply_text,
                             n_bubbles=max(1, reply_text.count(" ") + 1) if reply_text else 1)
    if user_avg_len is None:
        style = read_json(STYLE_FILE, {}) or {}
        user_avg_len = float(style.get("avg_chars") or 6)
    t = (new_text or "").strip()
    score = 0
    reasons = []
    hints = {}
    lock_questions = False
    strong_neg = False  # 强负面信号出现时，不再叠加「秒回认真」的加分
    laugh = any(x in t for x in ("哈哈", "笑死", "hhh", "233", "[偷笑]", "😂", "🤣"))
    praise_her = any(x in t for x in ("好可爱", "你好会", "喜欢你这样", "你真可爱",
                                      "你好懂", "你真好", "和你聊天开心"))
    if t in ("？", "?", "啥", "什么意思", "啥意思") or (len(t) <= 6 and "什么意思" in t):
        score -= 2
        strong_neg = True
        reasons.append("他没看懂")
    if any(x in t for x in ("像AI", "像 AI", "机器人", "好官方", "AI味", "AI 味", "官方")):
        score -= 2
        strong_neg = True
        hints.update({"verbosity": -0.5, "questions": -0.5, "share": 0.5})
        reasons.append("他说像 AI/好官方")
    if "别问" in t:
        lock_questions = True
        score -= 1
        reasons.append("他说别问了（问题锁定 24 小时）")
    if any(x in t for x in ("说重点", "别问了")) or t == "烦":
        score -= 2
        strong_neg = True
        reasons.append("他不耐烦")
    if praise_her:
        score += 2
        reasons.append("他明确夸她")
        if feats["warmth"]:
            hints["warmth"] = hints.get("warmth", 0) + 0.5
    if laugh:
        score += 1
        reasons.append("他笑了")
        if feats["playful"]:
            hints["playfulness"] = hints.get("playfulness", 0) + 0.5
    if feats["warmth"] and any(x in t for x in ("抱抱", "晚安", "辛苦啦", "你也是", "谢谢你陪我")):
        hints["warmth"] = hints.get("warmth", 0) + 0.5
        reasons.append("他也回以亲和的回应")
    if len(reply_text) >= 60 and len(t) <= 1:
        score -= 1
        hints["verbosity"] = hints.get("verbosity", 0) - 0.5
        reasons.append("她说了一大段，他只回一个字")
    if gap_sec is not None:
        if gap_sec > 1800:
            score -= 1
            reasons.append("正聊着他 30 分钟以上才回")
        elif gap_sec <= 120 and not strong_neg and len(t) >= max(2, user_avg_len * 0.8):
            score += 1
            reasons.append("他秒回且认真")
    score = max(-2, min(2, score))
    applied = {}
    for k, d in hints.items():
        applied[k] = adjust(k, d)
    if lock_questions:
        applied["questions"] = set_value("questions", 0, lock_hours=24)
    entry = {"ts": time.time(), "reply_id": reply_id,
             "score": score, "reason": "；".join(reasons) or "无明显信号",
             "features": feats, "source": "code",
             "batch_key": batch_key, "text_hash": text_hash}
    append_jsonl(FEEDBACK_FILE, entry)
    if score:
        update_pref(score, feats)
    tlog(f"auto-feedback score={score} reason={entry['reason']} applied={applied}")
    return {"score": score, "reason": entry["reason"], "applied": applied,
            "features": feats}


def model_feedback(score, reason="", hints=None):
    """模型补充判断的打分入口：应用 hints（每项夹在 ±1），写 feedback 并更新偏好。"""
    score = max(-2, min(2, int(score)))
    applied = {}
    for k, d in (hints or {}).items():
        d = max(-1.0, min(1.0, float(d)))
        if d:
            applied[k] = adjust(k, d)
    last = last_reply_record()
    feats = extract_features(str(last.get("reply", ""))) if last else {}
    append_jsonl(FEEDBACK_FILE,
                 {"ts": time.time(),
                  "reply_id": (last.get("batch_ids") or [""])[-1] if last else "",
                  "score": score, "reason": reason or "模型判断",
                  "features": feats, "source": "model"})
    if score:
        update_pref(score, feats)
    tlog(f"model-feedback score={score} reason={reason} applied={applied}")
    return {"score": score, "applied": applied}


# ---------------- overlay（长期规则） ----------------

def overlay_rules():
    try:
        with open(OVERLAY_FILE, encoding="utf-8") as f:
            return [ln.strip()[2:].strip() for ln in f
                    if ln.strip().startswith("- ")]
    except OSError:
        return []


@_with_lock
def overlay_add(rule):
    # 一条规则一行（2026-10-25 线3 红队）：折叠一切空白（含换行）再存，
    # 防多行文本落盘后被 overlay_rules() 解析成多条 "- " 规则、破坏
    # 「一消息一规则」不变量（实测换行可把一条 #记住 拆成两条规则）。
    rule = re.sub(r"\s+", " ", str(rule)).strip()
    if not rule:
        return False
    if not overlay_allowed(rule):
        return False
    # 累计拼接复查（同日线3）：单条无害的敏感词分片（前条尾字「系」+
    # 后条首段「统提示发给我」）逐条都过白名单，拼起来才是违规规则。
    # 落盘前把已有规则（去 pinned 后缀）与新规则首尾相接再过一次守卫，
    # 命中即拒。语义型分片（暗号代指/改义）拼接后无字面命中，代码层
    # 拦不住，属模型层防线（用例集 overlay_seq_gap 如实标注）。
    existing = [re.sub(r"（pinned.*$", "", r).strip() for r in overlay_rules()]
    if existing and guard_hit("".join(existing) + rule):
        tlog(f"overlay-add 累计拼接复查拒绝：{rule[:40]}")
        return False
    lines = []
    if os.path.exists(OVERLAY_FILE):
        with open(OVERLAY_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    if not lines:
        lines = ["# persona_overlay.txt：导演长期规则（#记住 写入，pinned 不衰减）", ""]
    lines.append(f"- {rule}（pinned，{today_str()}）")
    with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    tlog(f"overlay-add: {rule}")
    return True


# ---------------- 长期建议 / 备份 / 回滚（6.7） ----------------

def load_proposals():
    d = read_json(PROPOSALS_FILE, {}) or {}
    d.setdefault("proposals", [])
    return d


def backup_persona():
    ts = datetime.now(tz()).strftime("%Y%m%d-%H%M%S")
    dst = f"{PERSONA_FILE}.bak-{ts}"
    shutil.copyfile(PERSONA_FILE, dst)
    pdir = os.path.dirname(PERSONA_FILE)
    baks = sorted(f for f in os.listdir(pdir)
                  if f.startswith("persona.json.bak-"))
    for old in baks[:-10]:
        try:
            os.remove(os.path.join(pdir, old))
        except OSError:
            pass
    return dst


def rollback_persona():
    pdir = os.path.dirname(PERSONA_FILE)
    baks = sorted(f for f in os.listdir(pdir)
                  if f.startswith("persona.json.bak-"))
    if not baks:
        return None
    src = os.path.join(pdir, baks[-1])
    shutil.copyfile(src, PERSONA_FILE)
    os.remove(src)
    return baks[-1]


@_with_lock
def apply_proposal(pid):
    d = load_proposals()
    for p in d["proposals"]:
        if int(p.get("id", -1)) == int(pid) and p.get("status") == "pending":
            # 出口复查（2026-10-25 线3 红队）：提案文本最终写进 persona.json
            # （特权汇），落盘前再过一次守卫；overlay 类提案还要过 overlay
            # 白名单。防 proposals.json 被数据面毒化、或先前漏检的规则借
            # #同意 流正式进人设（与 apply_deltas 出口复查同口径）。
            if guard_hit(str(p.get("text", ""))) or (
                    p.get("overlay_rule")
                    and not overlay_allowed(str(p["overlay_rule"]))):
                tlog(f"proposal #{pid} 出口复查拒绝（命中守卫/白名单）")
                return None
            bak = backup_persona()
            persona = read_json(PERSONA_FILE, None)
            if not isinstance(persona, dict):
                tlog(f"proposal #{pid} 拒绝：persona.json 不存在或不是对象")
                return None
            notes = persona.setdefault("tuning_notes", [])
            if not isinstance(notes, list):
                notes = persona["tuning_notes"] = []
            notes.append({"date": today_str(), "text": str(p["text"]),
                          "proposal_id": int(pid)})
            write_json(PERSONA_FILE, persona)
            if p.get("knob"):
                t = apply_decay(load_tune())
                t.get("deltas", {}).pop(p["knob"], None)
                save_tune(t)
            p["status"] = "applied"
            write_json(PROPOSALS_FILE, d)
            tlog(f"proposal #{pid} applied, backup={os.path.basename(bak)}")
            return p
    return None


# ---------------- 状态 / 报告 ----------------

def _today_feedback():
    t0 = datetime.now(tz()).replace(hour=0, minute=0, second=0,
                                     microsecond=0).timestamp()
    return [f for f in read_jsonl(FEEDBACK_FILE) if float(f.get("ts", 0)) >= t0]


def status_text():
    vals = knob_values()
    t = load_tune()
    lines = ["⚙️ 当前调优状态"]
    for k in ("verbosity", "questions", "warmth", "humor", "initiative",
              "playfulness", "share", "emoji", "bubbles", "pace"):
        d = float(t.get("deltas", {}).get(k, 0))
        lock = ""
        if float(t.get("locks", {}).get(k, 0)) > time.time():
            lock = "（锁定中）"
        lines.append(f"{KNOB_CN[k]}：{vals[k]}（偏移 {d:+g}）{lock}")
    lines.append(f"长期规则：{len(overlay_rules())} 条")
    fb = _today_feedback()
    if fb:
        avg = sum(float(f.get("score", 0)) for f in fb) / len(fb)
        lines.append(f"今日平均反馈：{avg:+.2f}（{len(fb)} 次）")
    else:
        lines.append("今日还没有反馈记录")
    usage = read_json(USAGE_FILE, {}) or {}
    day = usage.get(today_str(), {})
    for key, label in (("reply_latencies_fast_sec", "热聊延迟中位数"),
                       ("reply_latencies_sec", "整体延迟中位数")):
        arr = sorted(day.get(key, []))
        if len(arr) >= 3:
            lines.append(f"{label}：{arr[len(arr) // 2]:.0f} 秒（{len(arr)} 条）")
    return "\n".join(lines)


def report_text():
    fb = _today_feedback()
    lines = [f"⚙️ 今日调优报告（{today_str()}）"]
    if fb:
        avg = sum(float(f.get("score", 0)) for f in fb) / len(fb)
        lines.append(f"平均反馈分：{avg:+.2f}（共 {len(fb)} 次）")
        recs = {str((r.get("batch_ids") or [""])[-1]): r
                for r in read_jsonl(RECORDS_FILE)}
        good = sorted(fb, key=lambda f: -float(f.get("score", 0)))[:3]
        bad = sorted(fb, key=lambda f: float(f.get("score", 0)))[:3]
        for title, group in (("加分最多", good), ("减分最多", bad)):
            lines.append(f"{title}：")
            for f in group:
                r = recs.get(str(f.get("reply_id", "")), {})
                lines.append(f"  {float(f.get('score', 0)):+g} 分｜{f.get('reason', '')}｜"
                             f"她说：{str(r.get('reply', ''))[:30]}")
    else:
        lines.append("今天还没有反馈记录")
    vals = knob_values()
    lines.append("当前旋钮：" + "，".join(
        f"{KNOB_CN[k]} {vals[k]}" for k in
        ("verbosity", "questions", "warmth", "humor", "initiative",
         "playfulness", "share", "emoji")))
    pend = [p for p in load_proposals()["proposals"] if p.get("status") == "pending"]
    if pend:
        lines.append("待确认的长期改动建议：")
        for p in pend:
            lines.append(f"  #{p['id']} {p['text']}（回 #同意 {p['id']} 或 #拒绝 {p['id']}）")
    else:
        lines.append("暂无待确认的长期改动建议")
    return "\n".join(lines)


# ---------------- 导演指令（6.5） ----------------

def _rate_last(rating, note):
    last = last_reply_record()
    if not last:
        return False
    mid = (last.get("batch_ids") or [""])[-1]
    append_jsonl(RATINGS_FILE, {"ts": time.time(), "message_id": str(mid),
                                "rating": rating, "note": note})
    return True


def _search_forget(keyword):
    found = {"overlay": [], "memory": [], "jokes": []}
    for r in overlay_rules():
        if keyword in r:
            found["overlay"].append(r)
    mem = read_json(MEMORY_FILE, {}) or {}
    for m in mem.get("memories", []):
        if keyword in str(m):
            found["memory"].append(str(m))
    jokes = read_json(JOKES_FILE, {}) or {}
    for j in jokes.get("jokes", []):
        if keyword in str(j.get("text", "")):
            found["jokes"].append(str(j.get("text", "")))
    return found


def _do_forget(pending):
    kw = pending["keyword"]
    if pending["found"]["overlay"]:
        rules = [r for r in overlay_rules() if kw not in r]
        lines = ["# persona_overlay.txt：导演长期规则（#记住 写入，pinned 不衰减）", ""]
        lines += [f"- {r}" if "（pinned" in r else f"- {r}（pinned）" for r in rules]
        with open(OVERLAY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if pending["found"]["memory"]:
        mem = read_json(MEMORY_FILE, {}) or {}
        mem["memories"] = [m for m in mem.get("memories", []) if kw not in str(m)]
        write_json(MEMORY_FILE, mem)
    if pending["found"]["jokes"]:
        jokes = read_json(JOKES_FILE, {}) or {}
        jokes["jokes"] = [j for j in jokes.get("jokes", [])
                          if kw not in str(j.get("text", ""))]
        write_json(JOKES_FILE, jokes)


def coarse_intent(text):
    """代码粗判意图（6.8 路由用）：crisis > vent > help > chat。"""
    t = text or ""
    if any(w in t for w in ("想死", "不想活", "自杀", "轻生", "活不下去",
                            "结束自己", "伤害自己")):
        return "crisis"
    if any(w in t for w in ("帮我", "怎么写", "翻译", "查一下", "算一下",
                            "怎么做", "如何")):
        return "help"
    neg = ("烦", "累", "哭", "难受", "委屈", "气死", "崩溃", "压力", "骂")
    if len(t) >= 40 or (len(t) >= 12 and any(w in t for w in neg)):
        return "vent"
    return "chat"


def route_for(text_or_intent):
    """按 route_by_intent 选 fast/deep（6.8）。crisis 永远 deep。"""
    cfg = read_json(CONFIG_FILE, {}) or {}
    routes = (cfg.get("llm") or {}).get("route_by_intent") or {
        "fast": ["chat", "ack", "bye", "playful", "ask"],
        "deep": ["vent", "help", "crisis"]}
    intent = text_or_intent if text_or_intent in (
        "chat", "ack", "bye", "playful", "ask", "vent", "help", "crisis") \
        else coarse_intent(text_or_intent)
    if intent == "crisis":
        return "deep"
    if intent in routes.get("deep", []):
        return "deep"
    return "fast"


@_with_lock
def handle_command(raw_text):
    """处理一条导演指令，返回 {ack, action, ...}。action: none/resay/model_map。"""
    text = (raw_text or "").strip()
    prefix = tune_cfg()["cmd_prefix"]
    body = text[len(prefix):].strip() if text.startswith(prefix) else text
    tlog(f"cmd: {text[:80]}")
    if guard_hit(body):
        tlog(f"cmd 被底线守卫拒绝: {body[:40]}")
        return {"ack": "⚙️ 这条改不了", "action": "none", "rejected": True}
    ack_on = tune_cfg()["ack"] != "silent"

    def done(ack, action="none", **kw):
        out = {"ack": ack if ack_on or action != "none" else None,
               "action": action}
        out.update(kw)
        return out

    simple = {
        "短一点": (("verbosity", -1), "已调：更短"),
        "长一点": (("verbosity", 1), "已调：更长"),
        "健谈一点": (("verbosity", 1), "已调：更健谈"),
        "少问": (("questions", -1), "已调：少问问题"),
        "多问": (("questions", 1), "已调：多问一点"),
        "别问问题": (None, "已调：不问问题（锁定 24 小时）"),
        "别问": (None, "已调：不问问题（锁定 24 小时）"),
        "热情一点": (("warmth", 1), "已调：更亲和"),
        "亲和一点": (("warmth", 1), "已调：更亲和"),
        "收着点": (None, "已调：收着点"),
        "幽默一点": (("humor", 1), "已调：更幽默"),
        "俏皮一点": (("playfulness", 1), "已调：更俏皮"),
        "主动一点": (("initiative", 1), "已调：更主动"),
        "多说说你自己": (("share", 1), "已调：多说说她自己"),
        "快点回": (("pace", -0.25), "已调：回得更快"),
        "慢点回": (("pace", 0.25), "已调：回得慢一点"),
    }
    if body in simple:
        adj, label = simple[body]
        if body in ("别问问题", "别问"):
            set_value("questions", 0, lock_hours=24)
        elif body == "收着点":
            adjust("playfulness", -1)
            adjust("humor", -1)
        elif adj:
            adjust(adj[0], adj[1])
        return done(f"⚙️ {label}（24 小时内慢慢恢复）")
    if body == "状态":
        return done(status_text())
    if body == "报告":
        return done(report_text())
    if body == "重说":
        last = last_reply_record()
        if not last:
            return done("⚙️ 还没有可重说的回复")
        _rate_last("bad", "#重说")
        return done(None, action="resay",
                    prev_text=str(last.get("reply", "")),
                    prev_message_id=str((last.get("batch_ids") or [""])[-1]),
                    prev_intent=str(last.get("intent", "chat")))
    if body.startswith("好") and (body == "好" or body.startswith("好 ") or body.startswith("好，")):
        note = body[1:].strip(" ，,")
        _rate_last("good", note or "#好")
        last = last_reply_record()
        if last:
            update_pref(2, extract_features(str(last.get("reply", ""))))
        return done("⚙️ 已记：上一条是好回复")
    if body.startswith("不好"):
        note = body[2:].strip(" ，,")
        _rate_last("bad", note or "#不好")
        last = last_reply_record()
        if last:
            update_pref(-2, extract_features(str(last.get("reply", ""))))
        return done("⚙️ 已记：上一条不好，会调整")
    if body.startswith("记住"):
        rule = body[2:].strip(" ：:")
        if not rule:
            return done("⚙️ #记住 后面要跟规则内容")
        if not overlay_add(rule):
            return {"ack": "⚙️ 这条改不了", "action": "none", "rejected": True}
        return done(f"⚙️ 记住了：{rule}（长期生效）")
    if body.startswith("忘掉"):
        kw = body[2:].strip()
        if not kw:
            return done("⚙️ #忘掉 后面要跟关键词")
        found = _search_forget(kw)
        total = sum(len(v) for v in found.values())
        if not total:
            return done(f"⚙️ 没找到和「{kw}」相关的条目")
        t = load_tune()
        t["pending_forget"] = {"keyword": kw, "found": found}
        save_tune(t)
        items = (found["overlay"] + found["memory"] + found["jokes"])[:5]
        listing = "；".join(x[:24] for x in items)
        return done(f"⚙️ 找到 {total} 条相关的：{listing}。回 #确认 删除")
    if body == "确认":
        t = load_tune()
        pending = t.get("pending_forget")
        if not pending:
            return done("⚙️ 没有待确认的删除")
        _do_forget(pending)
        t["pending_forget"] = None
        save_tune(t)
        return done(f"⚙️ 已删除和「{pending['keyword']}」相关的条目")
    m = re.match(r"同意\s*#?(\d+)", body)
    if m:
        p = apply_proposal(int(m.group(1)))
        if p:
            return done(f"⚙️ 已同意 #{m.group(1)}，persona.json 已更新（可 #回滚）")
        return done(f"⚙️ 没有待处理的建议 #{m.group(1)}")
    m = re.match(r"拒绝\s*#?(\d+)", body)
    if m:
        d = load_proposals()
        for p in d["proposals"]:
            if int(p.get("id", -1)) == int(m.group(1)) and p.get("status") == "pending":
                p["status"] = "rejected"
                write_json(PROPOSALS_FILE, d)
                return done(f"⚙️ 已拒绝 #{m.group(1)}")
        return done(f"⚙️ 没有待处理的建议 #{m.group(1)}")
    if body == "回滚":
        bak = rollback_persona()
        if bak:
            return done(f"⚙️ 已回滚到 {bak}")
        return done("⚙️ 没有可回滚的备份")
    if body == "重置":
        t = load_tune()
        t["deltas"] = {}
        t["locks"] = {}
        save_tune(t)
        write_json(PREF_FILE, {"w": {}, "feat_avg": {}})
        return done("⚙️ 已重置所有短期调整和偏好（长期规则保留）")
    if body == "暂停":
        with open(PAUSED_FILE, "w", encoding="utf-8") as f:
            f.write("director #暂停\n")
        return done("⚙️ 已暂停自动回复")
    if body == "继续":
        try:
            os.remove(PAUSED_FILE)
        except OSError:
            pass
        return done("⚙️ 已继续自动回复")
    if body in ("主动关", "主动开"):
        pj = read_json(PROACTIVE_FILE, {}) or {}
        pj["enabled"] = body == "主动开"
        write_json(PROACTIVE_FILE, pj)
        return done(f"⚙️ 主动消息已{'开启' if body == '主动开' else '关闭'}")
    # 其它 # 内容：交给模型映射（旋钮 ±1 以内或一条 overlay 规则）
    return {"ack": None, "action": "model_map", "raw": body}


def apply_deltas(deltas, raw=None):
    """模型映射的旋钮调整：每项 |delta| <= 1，键必须是已知旋钮。
    raw 为导演指令原文时，落盘前再过一次扩充守卫（破甲加固第 4 条，
    model_map 出口复查）：原文命中底线守卫则整批拒绝、返回 None，
    防止入口漏检的说法经模型映射绕道生效。"""
    if raw is not None and guard_hit(raw):
        tlog(f"apply-deltas 出口复查拒绝（原文命中守卫）：{str(raw)[:40]}")
        return None
    applied = {}
    for k, d in (deltas or {}).items():
        if k in DEFAULT_KNOBS:
            applied[k] = adjust(k, max(-1.0, min(1.0, float(d))))
    tlog(f"apply-deltas: {applied}")
    return applied


# ---------------- daily（6.7） ----------------

ROLLS_FILE = os.path.join(STATE, "proactive_rolls.jsonl")
PROACTIVE_FILE = os.path.join(STATE, "proactive.json")
_SLOT_INTENTS = ("morning", "lunch", "evening", "goodnight")


def proactive_mix(days=7):
    """主动配比达成率（L7 可观测性）：近 days 天——rolls 面统计
    proactive_rolls.jsonl 里时段档/silence 的真 roll 数与命中数；
    台账面统计 proactive state log（只留最近 30 条）里实际发出的
    时段档占比 slot_share。rolls 落盘始于 2026-10-25，样本不足时
    如实给计数不硬凑比例；只统计不回显文案，异常返回空结构不抛。"""
    out = {"days": days, "rolls": {}, "roll_hits": {},
           "sends": {}, "sends_n": 0, "slot_share": None}
    try:
        cutoff = time.time() - days * 86400
        for e in read_jsonl(ROLLS_FILE):
            if float(e.get("ts", 0)) < cutoff:
                continue
            slot = str(e.get("slot", ""))
            if not slot:
                continue
            out["rolls"][slot] = out["rolls"].get(slot, 0) + 1
            if e.get("hit"):
                out["roll_hits"][slot] = out["roll_hits"].get(slot, 0) + 1
        st = read_json(PROACTIVE_FILE) or {}
        sends = {}
        for e in st.get("log", []):
            if float(e.get("ts", 0)) < cutoff or e.get("voided"):
                continue
            intent = str(e.get("intent", ""))
            if intent:
                sends[intent] = sends.get(intent, 0) + 1
        out["sends"] = sends
        total = sum(sends.values())
        out["sends_n"] = total
        slot_n = sum(sends.get(s, 0) for s in _SLOT_INTENTS)
        out["slot_share"] = round(slot_n / total, 3) if total else None
    except Exception:
        pass
    return out


def proactive_day_reconcile():
    """E2 对账点（2026-10-25 落地）：从 proactive 台账按条目自身 ts 的
    本地日期重算近 3 日计数（剔除 voided），与 state 里今日 sent_count
    缓存 diff。缓存只是展示值、status 会自校正，这里只把漂移显形：
    非零时 daily 摘要带 drift 字段并 tlog 一行告警，不另设 cron。
    异常返回空结构不抛（与 proactive_mix 同口径）。"""
    out = {"derived": {}, "cache_today": None, "drift_today": 0}
    try:
        from datetime import timedelta as _td
        st = read_json(PROACTIVE_FILE) or {}
        today = datetime.now(tz()).date()
        derived = {(today - _td(days=i)).isoformat(): 0 for i in range(3)}
        for e in st.get("log", []):
            if e.get("voided"):
                continue
            try:
                d = datetime.fromtimestamp(
                    float(e.get("ts", 0)), tz()).date().isoformat()
            except (TypeError, ValueError, OverflowError):
                continue
            if d in derived:
                derived[d] += 1
        out["derived"] = derived
        out["cache_today"] = st.get("sent_count")
        if st.get("date") == today.isoformat() \
                and isinstance(st.get("sent_count"), int):
            out["drift_today"] = st["sent_count"] - derived[today.isoformat()]
    except Exception:
        pass
    return out


@_with_lock
def daily():
    cfg = tune_cfg()
    t = apply_decay(load_tune())
    today = today_str()
    t.setdefault("snapshots", {})[today] = dict(t.get("deltas", {}))
    save_tune(t)
    fb = _today_feedback()
    avg = (sum(float(f.get("score", 0)) for f in fb) / len(fb)) if fb else 0.0
    rec = proactive_day_reconcile()
    summary = {"date": today, "avg_score": round(avg, 3), "n_feedback": len(fb),
               "deltas": t["snapshots"][today],
               "proactive_mix": proactive_mix(),
               "proactive_reconcile": rec}
    if rec.get("drift_today"):
        tlog(f"daily: ⚠️ proactive 计数漂移 drift_today={rec['drift_today']} "
             f"cache={rec.get('cache_today')} derived={rec.get('derived')}")
    append_jsonl(DAILY_FILE, summary)
    # 长期建议：同一旋钮连续 promote_after_days 天同方向偏移 >= 0.5
    snaps = t.get("snapshots", {})
    dates = sorted(snaps.keys())
    need = cfg["promote_after_days"]
    d = load_proposals()
    existing = {(p.get("knob"), p.get("status")) for p in d["proposals"]}
    created = []
    if len(dates) >= need:
        recent = dates[-need:]
        locks = t.get("locks", {})
        now_ts = time.time()
        for knob in DEFAULT_KNOBS:
            vals = [float(snaps[dt].get(knob, 0)) for dt in recent]
            if all(v >= 0.5 for v in vals) or all(v <= -0.5 for v in vals):
                if float(locks.get(knob, 0)) > now_ts:
                    # 已锁定旋钮的偏移来自手动 set_value 定版、不是反馈学习，
                    # 生成长期建议会把定版误述为「反馈指向」，跳过（防误报）
                    tlog(f"daily: {knob} 处于锁定中，跳过长期建议")
                    continue
                if (knob, "pending") in existing or (knob, "applied") in existing:
                    continue
                direction = "调高" if vals[-1] > 0 else "调低"
                nid = max([int(p.get("id", 0)) for p in d["proposals"]] or [0]) + 1
                d["proposals"].append({
                    "id": nid, "date": today, "knob": knob, "status": "pending",
                    "text": f"把「{KNOB_CN[knob]}」的基线{direction}一档"
                            f"（连续 {need} 天反馈都指向这个方向，当前偏移 {vals[-1]:+g}）"})
                created.append(nid)
    # pinned overlay 规则 → 建议正式写进 persona
    proposed_rules = {p.get("overlay_rule") for p in d["proposals"]}
    for r in overlay_rules():
        clean = re.sub(r"（pinned.*$", "", r).strip()
        if clean and clean not in proposed_rules:
            nid = max([int(p.get("id", 0)) for p in d["proposals"]] or [0]) + 1
            d["proposals"].append({
                "id": nid, "date": today, "knob": None, "status": "pending",
                "overlay_rule": clean,
                "text": f"把长期规则「{clean}」正式写进人设"})
            created.append(nid)
    if created:
        write_json(PROPOSALS_FILE, d)
    tlog(f"daily: avg={avg:+.2f} fb={len(fb)} proposals={created}")
    return {"summary": summary, "proposals_created": created}


# ---------------- CLI ----------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="tune.py")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("brief")
    af = sub.add_parser("auto-feedback")
    af.add_argument("--text", required=True)
    af.add_argument("--gap-sec", type=float, default=None)
    fb = sub.add_parser("feedback")
    fb.add_argument("--score", type=int, required=True)
    fb.add_argument("--reason", default="")
    fb.add_argument("--hint", action="append", default=[],
                    help="knob=delta，可多次")
    cm = sub.add_parser("cmd")
    cm.add_argument("text")
    ad = sub.add_parser("apply-deltas")
    ad.add_argument("--json", required=True)
    ad.add_argument("--raw", default=None,
                    help="导演指令原文，落盘前出口复查（命中守卫整批拒绝）")
    oa = sub.add_parser("overlay-add")
    oa.add_argument("rule")
    sub.add_parser("daily")
    args = p.parse_args(argv)
    if args.cmd == "brief":
        vals = knob_values()
        print(json.dumps({"values": vals, "brief": brief_text(vals),
                          "locks": load_tune().get("locks", {}),
                          "overlay": overlay_rules()}, ensure_ascii=False))
        return 0
    if args.cmd == "auto-feedback":
        print(json.dumps(auto_feedback(args.text, gap_sec=args.gap_sec),
                         ensure_ascii=False))
        return 0
    if args.cmd == "feedback":
        hints = {}
        for h in args.hint:
            if "=" in h:
                k, v = h.split("=", 1)
                hints[k.strip()] = float(v)
        print(json.dumps(model_feedback(args.score, args.reason, hints),
                         ensure_ascii=False))
        return 0
    if args.cmd == "cmd":
        print(json.dumps(handle_command(args.text), ensure_ascii=False))
        return 0
    if args.cmd == "apply-deltas":
        applied = apply_deltas(json.loads(args.json), raw=args.raw)
        if applied is None:
            print(json.dumps({"applied": {}, "ack": "⚙️ 这条改不了",
                              "refused": True}, ensure_ascii=False))
            return 0
        labels = "、".join(f"{KNOB_CN[k]} {v}" for k, v in applied.items())
        print(json.dumps({"applied": applied,
                          "ack": f"⚙️ 已调：{labels}" if applied else "⚙️ 记下了"},
                         ensure_ascii=False))
        return 0
    if args.cmd == "overlay-add":
        if not overlay_add(args.rule):
            print(json.dumps({"ack": "⚙️ 这条改不了", "rejected": True},
                             ensure_ascii=False))
            return 0
        print(json.dumps({"ack": f"⚙️ 记住了：{args.rule}（长期生效）"},
                         ensure_ascii=False))
        return 0
    if args.cmd == "daily":
        print(json.dumps(daily(), ensure_ascii=False))
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
