#!/usr/bin/env python3
"""
llm.py — 大模型统一入口（开源通用版，OpenAI 兼容接口）。

用途：call(prompt, kind) 统一调模型生成文本（kind ∈ reply/proactive/
summary/judge）；主模型连续失败自动切 config.llm.fallback 备用配置，
备用满 1 小时后试探主模型；usage.json 按天记账（与 bridge 共用）。
配置依赖：config.json 的 llm 段——mode（"api" 默认 / "agent"）、
base_url、model、temperature、timeout_sec、api_key_env、routes 等。
API key 只从环境变量读：变量名由 api_key_env 指定（默认
COMPANION_LLM_API_KEY），绝不从配置文件/代码里读明文 key，绝不打印。
mode="agent" 时本模块不直接调模型（回复由外部 agent 经文件契约生成）。

可独立运行：python3 llm.py "提示词"（自检用，kind=reply）。
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("bridge", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

STATE = bridge.STATE
USAGE_FILE = bridge.USAGE_FILE
LLM_STATE_FILE = os.path.join(STATE, "llm_state.json")
ESCALATIONS_FILE = os.path.join(STATE, "escalations.jsonl")
WORKER_LOG = os.path.join(STATE, "worker.log")

FALLBACK_RETRY_AFTER_SEC = 3600  # 切到备用后，满 1 小时才试探主模型


def today_str():
    # 与 bridge.record_latency 的天 key 保持一致
    return time.strftime("%Y-%m-%d", time.localtime())


def log(msg):
    line = f"[{bridge.iso()}] {msg}"
    try:
        with open(WORKER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _api_key(cfg):
    """API key 只从环境变量读（变量名由 config 的 api_key_env 指定）。"""
    env_name = str(cfg.get("api_key_env") or "COMPANION_LLM_API_KEY")
    return os.environ.get(env_name, ""), env_name


def model_label(cfg, primary=True):
    if not cfg:
        return ""
    if cfg.get("mode") == "agent":
        return "外部 agent（agent 模式）"
    return cfg.get("model") or cfg.get("base_url") or "api"


def _llm_cfg():
    cfg = bridge.load_config()
    return cfg, cfg.get("llm") or {}


def _usage_update(fn):
    with bridge.state_lock():
        obj = bridge.read_json(USAGE_FILE, {}) or {}
        day = obj.setdefault(today_str(), {})
        day.setdefault("calls", {})
        day.setdefault("durations_sec", [])
        fn(day)
        bridge.atomic_write_json(USAGE_FILE, obj)
        return obj


def usage_bump(kind, duration=None, model=None):
    def fn(day):
        day["calls"][kind] = day["calls"].get(kind, 0) + 1
        if duration is not None:
            day["durations_sec"].append(round(duration, 2))
            day["durations_sec"] = day["durations_sec"][-500:]
        if model:
            day["model"] = model
    _usage_update(fn)


def usage_event(name):
    """name: rewrite / generation / no_reply / batch / fallback_reply。"""
    def fn(day):
        day[name] = day.get(name, 0) + 1
    _usage_update(fn)


def usage_today():
    data = bridge.read_json(USAGE_FILE, {}) or {}
    return data.get(today_str(), {})


def usage_alert_once(kind, text):
    """同一天同类告警只发一次（写 escalations，kind=alert）。

    查与记必须在同一次 _usage_update 事务内完成：旧实现先在锁外读
    usage 判断、再进锁追加，两步之间并发调用可同时通过预检、同日
    同类告警重复落盘（bughunt2 疑似项，2026-10-25 结案修复）。"""
    claimed = []

    def fn(day):
        alerts = day.setdefault("alerts_sent", [])
        if kind not in alerts:
            alerts.append(kind)
            claimed.append(True)
    _usage_update(fn)
    if not claimed:
        return False
    bridge.append_jsonl(ESCALATIONS_FILE, {"ts": time.time(), "kind": "alert", "reason": text})
    log(f"告警: {text}")
    return True


class LLMError(Exception):
    pass


def _call_api(cfg, prompt):
    """OpenAI 兼容接口：POST {base_url}/chat/completions。"""
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model")
    if not base or not model:
        raise LLMError("api 模式需要 config.llm.base_url 与 model")
    key, env_name = _api_key(cfg)
    if not key:
        raise LLMError(f"环境变量 {env_name} 未设置，请先导出 API key")
    body_obj = {"model": model,
                "temperature": float(cfg.get("temperature", 0.8)),
                "messages": [{"role": "user", "content": prompt}]}
    if cfg.get("max_tokens"):
        body_obj["max_tokens"] = int(cfg["max_tokens"])
    body = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    timeout = float(cfg.get("timeout_sec", 60))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"模型接口 HTTP {e.code}")
    except Exception as e:
        raise LLMError(f"模型接口调用失败: {type(e).__name__}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型接口返回结构异常")


def _call_once(cfg, prompt):
    mode = cfg.get("mode", "api")
    if mode == "agent":
        raise LLMError("agent 模式：回复由外部 agent 生成，llm.py 不直接调用模型")
    # "http" 是旧别名，与 "api" 同义（OpenAI 兼容接口）
    return _call_api(cfg, prompt)


def _get_state():
    return bridge.read_json(LLM_STATE_FILE, {}) or {}


def _set_state(st):
    bridge.write_json_locked(LLM_STATE_FILE, st)


def _escalate(text):
    bridge.append_jsonl(ESCALATIONS_FILE, {"ts": time.time(), "kind": "llm_switch", "reason": text})
    log(text)


def call(prompt, kind="reply", route=None):
    """统一入口：主备切换 + 记账。失败抛 LLMError。
    route：fast/deep，按 llm.routes 覆盖 timeout/model 等；agent 模式下不走这里。"""
    _, llm = _llm_cfg()
    primary = llm
    fallback = llm.get("fallback")
    st = _get_state()
    now = time.time()

    def _with_route(c):
        if not route or not isinstance(c, dict):
            return c
        r = (llm.get("routes") or {}).get(route) or {}
        if not r:
            return c
        merged = dict(c)
        for k in ("timeout_sec", "mode", "model", "base_url", "api_key_env", "temperature"):
            if k in r:
                merged[k] = r[k]
        return merged

    use_fallback = bool(st.get("using_fallback")) and bool(fallback)
    if use_fallback and now - float(st.get("fallback_since", 0)) < FALLBACK_RETRY_AFTER_SEC:
        cfg, is_primary = fallback, False
    else:
        cfg, is_primary = primary, True

    t0 = time.time()
    try:
        out = _call_once(_with_route(cfg), prompt)
    except LLMError as e:
        dur = time.time() - t0
        usage_bump(kind, duration=dur, model=model_label(cfg, is_primary))
        if is_primary:
            st["fail_streak"] = int(st.get("fail_streak", 0)) + 1
            if st["fail_streak"] >= 2 and fallback and not st.get("using_fallback"):
                st["using_fallback"] = True
                st["fallback_since"] = now
                _set_state(st)
                _escalate(f"主模型连续 {st['fail_streak']} 次失败（{e}），已切换到备用模型 "
                          f"{model_label(fallback, False)}；原因和切换情况已记录，微信回复暂时走备用模型。")
                return call(prompt, kind, route=route)  # 立即用备用重试本次
            _set_state(st)
            if use_fallback:
                # 试探主模型失败：继续留在备用，刷新计时
                st["using_fallback"] = True
                st["fallback_since"] = now
                _set_state(st)
                try:
                    out2 = _call_once(_with_route(fallback), prompt)
                    usage_bump(kind, duration=time.time() - t0, model=model_label(fallback, False))
                    return out2
                except LLMError:
                    pass
            raise
        else:
            # 正在用备用且备用也失败
            st["fallback_fail_streak"] = int(st.get("fallback_fail_streak", 0)) + 1
            _set_state(st)
            raise LLMError(f"备用模型调用失败: {e}")
    dur = time.time() - t0
    usage_bump(kind, duration=dur, model=model_label(cfg, is_primary))
    if is_primary:
        if st.get("using_fallback"):
            st["using_fallback"] = False
            st.pop("fallback_since", None)
            _escalate(f"主模型 {model_label(primary, True)} 已恢复，已切回主模型。")
        st["fail_streak"] = 0
        _set_state(st)
    else:
        st["fallback_fail_streak"] = 0
        _set_state(st)
    return out


def current_model_label():
    _, llm = _llm_cfg()
    st = _get_state()
    if st.get("using_fallback") and llm.get("fallback"):
        return model_label(llm["fallback"], False) + "（备用）"
    return model_label(llm, True)


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) or "只回复：你好"
    try:
        print(call(text, kind="reply"))
    except LLMError as e:
        print(f"调用失败: {e}", file=sys.stderr)
        sys.exit(1)
