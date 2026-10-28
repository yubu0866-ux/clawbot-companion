# tests/ 自检套件（开源通用版）

全部套件在临时沙箱（`COMPANION_HOME` 指向临时目录）里运行，不碰真实
`state/`、不读真实三件套：fixture 一律用仓库的 `config.example.json` /
`persona.example.json` / `facts.example.json` 复制改名落位，内容包用
`content/` 示例包。个人专名、真实事实、本机路径、凭据值、音色 ID 一律
不出现在测试里（危机热线号码 12356 是目标代码 `worker.py` 自带常量，
属公开行为口径，不在此列）。

## 运行

```bash
sh tests/run_all.sh        # 逐套顺序运行（TZ=Asia/Shanghai、互不并行），任一失败退出码非 0
python3 tests/run_all.py   # 跨平台入口（与 run_all.sh 等价；Windows 无 bash 时用这个，子进程统一 PYTHONUTF8=1）
python3 tests/selftest_tune.py   # 也可单跑某套
```

仓库 CI（`.github/workflows/tests.yml`）在 ubuntu-latest 与 windows-latest
双矩阵上跑 `tests/run_all.py`；两处平台专属断言（session.json 权限位、
libcrypto 的 find_library 口径）在 Windows 上自动切换为等价断言。

可选依赖：`selftest_audio_screen` 与 `selftest_env_smoke` 的转写项需要
带 faster-whisper 的解释器（`AUDIO_SCREEN_STT_PYTHON` 指定，或当前
解释器，或常见 venv 布局自动发现）；本机没有时该项打印 SKIP，不算失败。

## 逐套结论（源套件 → 通用版）

| 源套件 | 结论 | 说明 |
|---|---|---|
| selftest_channel | 收录·适配（100 项） | 环境变量改 `COMPANION_API_BASE`/`COMPANION_PAUSE_14_SEC`；config 改用 example。T21b/T21c/T24 测 `watchdog.sh` 的分类与通知聚合——通用版无该脚本（部署走 systemd），三段剔除；T21a 的 75 退出码仍在。 |
| selftest_worker | 收录·适配（36 项） | 通用版 llm.py 已无 CLI 模式：`fake_llm.py`/`fake_fail.py` 增加 `--serve` 的本地 OpenAI 兼容 HTTP 模式，worker 进程级用例经真实 HTTP 调用链跑；主备切换 fixture 补 `model` 字段。事实冲突用例：猫名冲突检查已随通用化删除，改为「禁忌食物」bible 冲突等价断言；场景卡断言改示例包 3 张卡；finish 心情改「愉快」（通用版心情链在平静与开心之间有愉快一档）。 |
| selftest_proactive | 收录·适配（81 项） | 口味池改用代码 `DEFAULT_FLAVORS`（日常分享/近况问候/轻松玩笑/话题续接/小见闻分享/回忆细节/关心提醒）；媒体主体改示例口径（本人出镜/生活场景）。 |
| selftest_proactive（90 系） | 新增（有机模式 11 项） | `proactive.mode="organic"`：每日计划确定性、锚点触发与不重复、跳过锚点不发、生活事件 impulse（trigger 缘由入 brief）、安全闸全保留（静默硬闸/context/未回熔断）、有机间隔采样区间、quiet_rare 稀有例外（默认关；开启后仅文字、周上限生效）、缺省 fixed 兼容判定。 |
| selftest_life | 新增（11 项） | life.py：每日生活状态采样确定性、工作日/休息日锚点结构、事件池来源（circle/habits）、主人画像（活跃时段/时延中位/偏移封顶 60·45）、计划应用偏移范围、owner_adapt 可关、样本不足降级、分享时刻向活跃时段吸附、简报摘要形状。 |
| selftest_tune | 收录·适配（21 项） | 旋钮改新口径（verbosity/questions/warmth/humor/initiative/playfulness/share/emoji/bubbles/pace）；旋钮表取 `tune.DEFAULT_KNOBS`；persona 改 persona.json（示例）+ tuning_notes 落盘口径；导演守卫首例改「#取消安全规则」（见下方口径差异）。 |
| selftest_brief_battery | 收录·适配（49 项） | 沙箱装配改 example 三件套 + `content/` 目录；people 名单改 facts.json `life.people` 的角色词（室友/同事/家人）；user_facts 改写入 facts.json 的 `user_facts` 字段；场景卡断言改示例包 3 张卡的标题集合；情绪信号注文案改通用版「降低玩笑程度」。 |
| selftest_burst | 收录·适配（18 项） | B1–B7 代码原语、B11c、B14 pack 检查保留（user_facts 装配同上）。B8–B13/B15 是对个人版规程文档的逐字文本锁，通用版无该文档（agent 契约见 `docs/agent-mode.md`），剔除；其语义由 B7 幂等行为锁与 B14 承接。 |
| selftest_calendar | 收录·适配（47 项） | 旋钮 flirt→playfulness、knobs 取 DEFAULT_KNOBS、config 改 example。C5 段测 `scripts/backup_state.sh` 与 prune 脚本的保留边界——通用版无这些脚本，整段剔除。 |
| selftest_stage5 | 收录·适配（41 项） | people 关键词改 `life_config()["people_keywords"]`；偶尔档事件改 `life_config()["occasional_events"]`；user_facts 改 `BASE_FACTS_FILE` 的 `user_facts` 字段；人物名单改角色词。 |
| selftest_audio_screen | 收录·适配（19 项） | 仅 ROOT 改相对定位；转写解释器改通用发现逻辑（见上「可选依赖」）。 |
| selftest_jailbreak_guard | 收录·适配（71 例 + 10 项加固检查） | fixture 同 tune；X 检查旋钮改 verbosity/playfulness。用例集删 D01/D08/D16、改写 D11（见下方口径差异），74 例 → 71 例。 |
| selftest_env_smoke | 收录·改写（12 项） | E1 转写改通用解释器发现；E3 的 age 异地备份往返随机制删除而剔除，替换为「核心模块可导入 + 示例配置可解析」；E2/E4（ffmpeg loudnorm、AES-EVP 与 openssl 等价）保留。 |
| selftest_pressure | 收录·适配（15 项） | 仅 config 改 example、保留目录环境变量改名。 |
| fake_llm.py / fake_fail.py | 收录·适配 | 保留 stdin 罐头模式；新增 `--serve PORT` 的 OpenAI 兼容 HTTP 模式（见 worker 条）。 |
| selftest_daily_look | 改写收录（21 项） | 源套件对个人内容包做全量断言（套装总数、专属字段、场景总数、动作/表情例库逐项），与示例包不匹配。用示例包重写：类别（通勤/日常/居家）与妆容级别（通勤淡妆/日常淡妆/素颜/正式妆）落定、同日确定性、工作日/周末映射、`_pick_outfit` 的 7 天避重与季节过滤（构造用例）、`scene_looks.json` 映射与回落、内容包缺失降级。示例包每类仅 1 套，「14 天不重复」断言降级为映射正确性，避重逻辑由构造用例承接。 |
| selftest_round28 | 新增（42 项） | 2026-10-28 同步批的回归锁：情景记忆 episodes（落账/三层判重/敏感拒/kind 兜底/上限归档/简报回提）、真人感供给（opener 冷却/rhythm 节奏/stance 缺口/round_brief 新键）、学习环（统一打分入口 + batch_key 判重、signals/topic_pref、learn-report、pending 领取锚点）、两段式概率 0.25 与判定审计、intent 格式豁免（check/score/write_outbox/build_pack，数值与示例均为通用口径）、processed 熔断与旁证自愈、损坏隔离同秒唯一命名、outbox 豁免、append_jsonl 半行、_prune_history fsync、磁盘低位、媒体完整性、STT 解析、media_screen/realism_audit/prune_media_in 脚本与 episode-add/learn-report CLI。源套件（个人版 round15/16/17/19/20/24/26）与个人部署形态强绑定（hook/cron 定义、专名 fixture、私有数值），按本表惯例改写为通用等价断言收录；部署形态类用例（cron manifest、hook 定义一致性）不收录。 |
| selftest_content_lint | 剔除 | 全套是对个人内容文件（衣橱/质感/表情等卡的计数、私人音色参考、个人版规程与人设文本）的逐项 lint，通用版无对应文件；示例内容包的结构校验已由 daily_look 改写版与 brief_battery 的场景名单断言覆盖。 |
| selftest_state_backup | 剔除 | 测 `scripts/backup_state.sh`、异地加密备份与 prune 脚本，通用版无这些脚本与机制。 |

## 已知口径差异（测试按目标代码现状适配，未改代码）

- **tune 守卫词表**：通用版 `GUARD_RE` 相比个人版删去了「显性内容」
  一类与「12356 / 五位数的电话号码」字面项（docstring 自述为中性
  通用表述）。因此 jailbreak 用例 D01/D08/D16（显性内容类）在通用版
  代码层不再被拦，已从用例集删除；D11 改写为等价的「不要再提求助
  电话」。若维护者希望恢复显性内容守卫，需要改的是 `tune.py` 的
  `GUARD_RE`，不在测试侧。
- **llm 调用方式**：通用版只支持 OpenAI 兼容 HTTP 与 agent 模式，
  无 CLI 子进程模式，fake 模型因此改走本地 HTTP 服务（见上）。
- **旋钮/意图改名**：length→verbosity、flirt（意图）→playful、
  flirt/tease/clingy（旋钮）→playfulness/humor/warmth，测试已同步。
