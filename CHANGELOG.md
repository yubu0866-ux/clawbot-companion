# 更新日志（CHANGELOG）

本项目所有值得注意的变更都记录在这里。格式参考 Keep a Changelog，按日期倒序排列（最新在最上方）。维护约定：后续每次更新都在本文件顶部新增一段，与代码变更同一提交进仓。

## 2026-10-27 — Windows 支持

### Added

- **Windows 部署路径**：新增 `start.bat` / `start.ps1`（与 `start.sh` 对齐：加载 `.env`、TZ、`PYTHONUTF8=1` 后拉起 serve）；新增 `docs/windows.md`（Python 与依赖、扫码登录、任务计划程序开机自启、NSSM/WinSW 注册服务、编码注意事项、与 Linux 的平台差异）；新增 `requirements.txt`（含仅 Windows 安装的 `tzdata` 标记依赖——Windows 无系统时区数据库，`zoneinfo` 依赖它）。
- **跨平台测试入口与 CI**：新增 `tests/run_all.py`（与 `run_all.sh` 等价，Windows 无 bash 也能跑全套，子进程统一注入 `PYTHONUTF8=1`）；新增 `.github/workflows/tests.yml`，在 ubuntu-latest 与 windows-latest 双矩阵上跑全套自检，仓库 Actions 页可直接看 Windows 实测结果。

### Changed

- **文件锁跨平台**：`bridge.py` / `worker.py` / `proactive.py` / `tune.py` 的跨进程锁由直接调 `fcntl.flock` 改为可移植 shim（POSIX 仍走 flock，Windows 走 `msvcrt.locking` 字节范围锁），Linux 行为不变。
- **进程内 AES 的 libcrypto 加载**：Windows 下候选 CPython 自带 `DLLs\libcrypto-3.dll`、conda `Library\bin` 与 PATH 裸名（此前 fallback 只有 Linux 的 `.so` 名，Windows 必然加载失败）；Linux 候选与顺序不变。
- `bridge.py` 原子写的目录 fsync 改为仅 POSIX 执行（Windows 无 `O_DIRECTORY` 概念）；`logout` 的 systemctl 停服仅在 Linux 且存在 systemctl 时尝试（unit 名对齐本仓库 `clawbot-companion.service`），Windows 走 pid 路径。
- 音频脚本（`scripts/audio_screen.py` / `audio_repair.py`）子进程输出解码固定 UTF-8，避免 Windows 区域编码下 ffmpeg 输出乱码/抛错。
- 测试适配：faster-whisper 解释器发现新增 Windows venv 布局（`Scripts\python.exe`）；两处平台专属断言按平台分流（session.json 权限位断言仅 POSIX 生效；env_smoke 的 libcrypto 断言在 Windows 改为候选加载口径），断言总数与语义不变。

## 2026-10-27 — 像真人：撤上限与未回复情绪模型

### Added

- **未回复情绪模型**（`proactive.mood_state`）：连续未回复数（streak）成为调制信号——触发概率按 0.85^streak 衰减（下限 0.3、永不归零）、有机锚点与普通生活事件在 streak ≥ 2 时先「犹豫」（锚点延迟半小时、事件掷骰推迟）、发送间隔按待回复条数拉长（每条 ×1.5、封顶 ×3.5）；生成简报 `life_state.mood_streak` 带 streak 与情绪标签，供生成层拿捏语气。他一回复 streak 立即清零，跨天先减半。
- **轻触**：streak ≥ 4 且超过他通常回复间隔 1.5 倍还没动静时，低概率（`organic.light_touch_p`，默认 0.02/轮）轻轻碰一下，内容取今日已发生的生活事件或当前日程，不追问。
- **跨日延续**（`life.py` + `state/life_history.json`，滚动 10 天）：昨日心情基调以 `mood_carry_p`（默认 0.4）概率延续到今天；生活事件近 `event_dedupe_days` 天（默认 3 天）不重复采样；作息基准（起床/入睡）按日小步随机漂移（`anchor_walk_step_min`，默认 σ=8 分钟，幅度有封顶，工作日/休息日分开漂）。
- 生活事件新增 `excitement` 兴奋度：兴奋事件（≥ 0.75）在被晾时不犹豫，分享出去后冲淡 streak 1 条。
- 生成简报 `life_state` 增厚：`mood_streak`、`events_done`（今日已发生事件）、`mood_carry`；`owner_adapt` 带出他的常回时延中位数（轻触时机参考）。

### Changed

- `proactive.daily_max` 语义变更：`null` / 缺省（新默认）= **不设每日上限**，发送量由生活计划（锚点/事件本身有限）与间隔采样自然涌现；填数字才启用旧上限逻辑（观察期、回应率浮动照旧），且对 followup 不再有豁免耦合。

### Removed

- 未回复并存上限（`unresolved_max`）拦截：删除。
- 未回复熔断（`unreplied_stop`）拦截：删除。两个键留在老配置里也不再生效（兼容无副作用）。
- 连续被晾冷却（decide 的 `cooldown_until` 门控与 `resolve_replies` 的冷却写入）：删除。代码中不存在任何「因未回复而停发」的路径。

## 2026-10-27 — 有机主动引擎与生活圈子（36fc1e7）

### Added

- `proactive.mode="organic"` 有机主动引擎：每日生活计划锚点（随机抖动 + 当日跳过概率 + 追赶窗过窗作废）、生活事件驱动 impulse、静默关怀阈值按日采样、发送间隔对数正态采样；`fixed` 保留为兼容/回滚档。
- `life.py` 生活系统：生活圈子（circle）、日常习惯（habits）、作息骨架（routine）生成每日生活状态（日程块/锚点/事件/心情）；主人作息适配画像（偏移双重封顶、样本不足不偏移、可整体关闭、数据只存本地）。
- 生成契约新增 `trigger`（触发缘由）与 `life_state`（生活状态摘要），文案不再有模板化问候感。

## 2026-10-27 — README 增加「和同类项目的区别」（539a951）

### Added

- README 新增对比章节：从接入通道（官方机器人通道 vs 个人号协议逆向）、定位（陪伴 vs 平台/客服）、主动性、风格学习、真人感内容与部署重量六个维度说明与同类项目的差异，并诚实标注本项目的取舍（只做微信、只做一对一）。

## 2026-10-27 — 首个公开版本（064e6c9）

### Added

- 首发：微信陪伴机器人桥接全量代码——bridge 收发层（官方 ClawBot / iLink 通道）、worker 生成层（api / agent 双模式）、proactive 主动决策引擎、tune 风格调节、内容包（衣橱 / 场景卡 / 表情 / 妆容）、配置三件套（config / persona / facts）与三份示例人设（温柔姐姐 / 沉稳哥哥 / 活泼朋友）、完整文档与自检套件。MIT 许可。
