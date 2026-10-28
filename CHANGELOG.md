# 更新日志（CHANGELOG）

本项目所有值得注意的变更都记录在这里。格式参考 Keep a Changelog，按日期倒序排列（最新在最上方）。维护约定：后续每次更新都在本文件顶部新增一段，与代码变更同一提交进仓。

## 2026-10-28 — 情景记忆、入站视频理解与可靠性硬化

### Added

- **情景记忆 episodes**（`worker.py` + `state/episodes.json`）：与 facts 分工的共同经历记忆——6 类（共同经历/对方细节/情绪事件/两人之间的梗/约定/纪念日）、三层判重（互含/相似度/facts 互含）、敏感信息拒落账、active 上限归档（约定/纪念日不归档）；三路捕获（当轮 `episodes_add` 或 `episode-add` 命令、每日摘要抽取、可选的每周固化），简报按日轮换回提近期条目与 7 天内约定/纪念日。每日摘要的抽取口径同步写明：各类事件同等收录，不因题材淡化、略过或改写失真。
- **真人感简报供给**：`round_brief` 新增 `opener_recent`（句首计数＋冷却标记）、`rhythm_recent`（气泡句长 CV 与长短交错提示）、`stance_today`（今日表态缺口显性化）、`topic_pref`（话题偏好聚合）；新增 `scripts/realism_audit.py`——12 项真人感指标的可复跑审计（只出数值、不回显聊天正文，首次 `--record` 即基线，适合每周定时跑、回归时才报告）；`build_pack` 新增 `scene_look` 全表下发（接通内容包 `scene_looks.json`，附 `content/scene_looks.example.json` 示例）。
- **入站视觉全量理解**：图片改多图全收（单消息 ≤9 张逐张留存，逐图凭证补取）；视频理解管线——ffprobe 元信息、均匀＋场景切换抽帧（封顶 8 帧）、faster-whisper 转写（只转前 180 秒、段级过滤、五档如实状态 `ok/partial/no_audio/no_speech/failed`、异步回填＋24 小时内补扫）；转写运行时解析改中性口径（`COMPANION_STT_PYTHON` → 同级/家目录 `stt-venv` → serve 解释器自身），模型缓存目录按家目录解析；新增 `config.stt` 段（model/language）；`prep`/`pending` 输出带多图清单与视频简报（帧图＋元信息＋转写）；新增 `scripts/prune_media_in.sh`（收件原件保留清理：有文字留档才删、多图/帧图整组处理、默认 dry-run）。
- **出站视觉质检成文**：新增 `scripts/media_screen.py`（一键机器检：解码/规格/抽首中尾帧＋自动串 `audio_screen` 音轨门）；`docs/media-and-tts.md` 新增「出站视觉质检」（机器检→视觉判断→重出一次→降级）与「提示词包络与平台接受台账（方法论）」（只记生产实证、拦截三类归因、描述性事实写满、被拦降最近的可过版本、换说法不减信息量）两节；`docs/agent-mode.md` 新增出站 `intent` 字段与「媒体生成锚点」（`state/media_jobs/`）协议。
- **学习环修复与可见性**：隐式反馈打分改锚每轮必经的领取动作——`pending` 首领、`prep`（无 `--text` 时从当前批次派生）、`run` 共用统一入口 `score_batch_feedback`；`tune.auto_feedback` 新增批次键判重（同批重复进入只落一次，落盘带 `batch_key`/`text_hash`）；行为信号 `state/signals.jsonl` 与话题偏好聚合进简报；新增 `worker.py learn-report [--write]` 学习可见性报告（反馈覆盖、逐旋钮 promote 门槛评估）。
- **intent 格式豁免（部署者可配置）**：新配置 `limits.intent_overrides`——按 intent 名自定气泡/单条/整轮上限与镜像豁免、平坦长度分，bridge 数值硬门与 worker 校验/打分/提示词/outbox 共用同一条目，未配置时行为与旧版完全一致；`docs/agent-mode.md` 与 `docs/configuration.md` 同步说明。
- **新自检套件** `tests/selftest_round28.py`（42 项），注册进 `run_all.py`/`run_all.sh`。

### Changed

- **两段式概率 0.15 → 0.25**（`worker.two_stage_probability` 可配），且每次判定（含未触发）落 `state/two_stage_log.jsonl` 审计，事后可查「为什么没触发」。
- **媒体数口径**：`limits.max_media_per_reply` 示例默认改为 `null`（不单独设上限、媒体条不计气泡），填整数恢复上限；硬门气泡计数同步改只计非媒体条。
- **媒体救援节奏**：outbox 失败清道夫对服务端 5xx 类失败改分段拉长救援间隔（即时/30 分钟/2 小时，覆盖约 2.5 小时的瞬时故障窗），连接类错误不加延迟；CDN 上传失败留存错误响应体摘要与 `server`/`via`/`x-cache` 诊断头，下次 5xx 不再只剩裸状态码。

### Fixed

- **接收游标与落盘解耦**：入站落盘失败（磁盘满等）不再推进接收游标（平台重投＋message_id 去重兜底，不丢消息）；内容异常类照常推进不阻塞队列。
- **state 损坏留痕防覆盖**：bridge/worker/proactive/tune/life 的 `read_json` 遇解析损坏先把原文件隔离为 `.corrupt-<秒>` 留痕再回默认，同秒多次损坏以 pid/序号唯一命名绝不覆盖（outbox 三目录豁免，以免误伤死信契约）。
- **reply 组 token 失效整组止损**：发送遇上下文失效类错误（errcode -3 等）时同组其余待发条目一并判死，不再逐条撞墙。
- **processed 熔断与自愈**：未处理积压异常激增（processed 损坏/回滚签名）时，已有答复旁证的消息跳过并回填 processed，批量只放行最新 25 条，宁可漏答存量不集体重答。
- **fsync 对齐与磁盘监测**：多处 `write_json` 与 history 修剪补文件＋目录 fsync（跨平台守卫）；磁盘余量低于 500MB 时 serve 周期性告警。
- **入站媒体完整性校验**：图片按格式验尾部结构（JPEG EOI/PNG IEND/GIF 终止符/WebP RIFF 长度），视频比对 md5 与 ffprobe 探空降级，截断件不当完整件落盘。
- **outbox 毒值防阻塞**：待发条目关键字段为毒值（类型异常）时判死离队，不再让一条坏条目堵住整个队列。
- `append_jsonl` 尾行无换行时先补行再追加，修半行拼行损坏后续所有行的问题。

## 2026-10-27 — README 增加反馈入口

### Added

- **微信群二维码反馈入口**：README 新增「反馈与交流」一节，附微信群「反馈bug」二维码（`docs/images/wechat-group-qr.png`），方便使用者扫码进群反馈 bug 与提出建议；二维码会不定期更新，有效期以图片为准。

## 2026-10-27 — CI 双矩阵第三轮失败修复

### Fixed

- **Windows 格 channel 自检 T18a/T18c/T19b/T19e 失败**：入站图片/视频落盘后，`bridge.py` 用 `os.path.join` 拼存储相对路径，在 Windows 上产出反斜杠（`state\media_in\...`），与 inbox/history 里 `state/media_in/...` 的既定口径不符，断言逐项失败（CI 日志 `mf=state\media_in\IMGOK1.png` 实证）。三处存储路径（图片、视频、抽帧）一律改正斜杠拼接；正斜杠在 Windows 文件操作里同样合法，Linux 行为不变。
- **Windows 格 channel 自检 T21a 失败**：退出码 75 本身是对的，栽在 pid 断言上。根因是 Windows 的 `terminate()` 直接杀进程、不走优雅退出，旧 serve 的 pid 文件不会被清理；T21a 的 `wait_until` 立刻命中旧文件，`pid_before` 读到 stale 值。测试侧在起新 serve 前先删旧 pid 文件（断言本身未动：仍要求第二 serve 以 75 退出、日志留痕、pid 未被覆盖）。
- **proactive 自检偶发 99/100（两格同败）**：20 项断言 `due=True`，但情绪调制把 morning 槽概率从 1.0 压到 0.85，不强制 roll 时约 15% 的运行会偶发失败（本地干净环境 5 遍复现 1 次，失败项正是 20 项）。按 21b 先例强制该项的 roll 命中（`random`/`randint`/`choice`/`uniform` 一并给确定性实现）；断言本身未动，产品行为未改。
- **Windows 格 pressure 自检失败（P4 多进程 hammer）**：`bridge.py` 的 Windows 阻塞锁用 `msvcrt` 的 LK_LOCK，约 10 秒即放弃并抛错，而 `_StateLock` 会把该错误吞掉继续跑——高并发下悄悄退化成无锁，计数丢失。阻塞锁改用非阻塞轮询 + 让步实现真阻塞（上限 120 秒，到时仍抛错走原有兜底）。

### Changed

- **失败定位**：`tests/run_all.py` 对失败套件除打印尾部 30 行外，另打印全部 FAIL 行——此前 CI 第三次运行的 proactive/pressure 失败项因只打尾部、日志里全是 PASS 行而无法定位（本次修复范围，教训记入）。

## 2026-10-27 — CI 双矩阵首跑修复

### Fixed

- **Ubuntu 格 channel 自检 T1a/T1b 失败**：根因为 `requirements.txt` 漏列 `bridge.py` login 实际依赖的 `qrcode` 包（本地开发机装有该包、干净检出环境没有，导致 ASCII 二维码与 `qr.png` 双双生成失败）。已补 `qrcode>=7.4`；干净 venv（仅旧依赖）复现 T1a/T1b 双败、补装后全过，与 CI 日志逐项对齐。
- **Windows 格 tune 套件崩溃 + 汇总打印崩溃**：`tests/selftest_tune.py`、`selftest_proactive.py`、`selftest_worker.py` 的并发写用例直接调 `multiprocessing.get_context("fork")`（Windows 无 fork，建上下文即抛错；另两处子进程函数还是嵌套定义、spawn 下无法 pickle）。三处改为按平台选 fork/spawn，两个嵌套子进程函数提为模块级；`tests/run_all.py` 入口自身输出先切 UTF-8（此前 Windows 默认 cp1252 下打印中文失败尾部会 `UnicodeEncodeError` 二次崩溃、掩盖真失败）。工作流自检步骤另加 `PYTHONUTF8=1` 兜底（workflow 文件因应用权限需经网页端更新，见提交说明）。

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
