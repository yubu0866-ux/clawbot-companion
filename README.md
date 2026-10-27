# clawbot-companion

> An open-source WeChat companion bridge: a long-running bot that chats with one user in WeChat, keeps a consistent fictional persona, can initiate gentle daily check-ins, and supports both API-based generation and a file-based "agent mode". All state stays on your own machine.

一个开源的微信陪伴型机器人桥接程序：它以一个设定一致的虚构角色与单个使用者日常聊天，支持主动问候、风格微调、内容包（衣橱/场景/表情/妆容）与可选的媒体生成接口。默认通过兼容 OpenAI 格式的 API 生成回复，也支持文件队列驱动的 agent 模式。所有会话与状态只保存在本地。

## 这是什么

- 面向「一对一陪伴聊天」场景，不是群聊机器人，也不是客服系统。
- 角色人设、生活事实、内容包全部是本地 JSON / Markdown 文件，改文件即可换角色，仓库自带 3 份完整示例人设。
- 主动消息只做「决策」不直接生成文案：先判断时段、概率与静默，再交给生成层写消息；未回复只影响她的情绪与节奏（概率衰减、间隔拉长），永不构成拦截。

## 和同类项目的区别

- **通道**：走微信官方 ClawBot / iLink 机器人通道，以官方机器人身份接入个人微信一对一私聊；不走个人号协议逆向（那类接法有封号风险，也不建议用主力号），不需要企业微信或公众号资质。
- **定位**：只做一对一陪伴聊天，不做群聊、客服与工作流平台。角色由 persona.json + facts.json + 内容包完整定义，改配置即可换人。
- **主动性**：内置主动消息决策引擎，还有一档「有机模式」：每天按她的生活计划现采样——几点出声带随机抖动、有的日子干脆跳过、分享由她当天真实发生的生活事件触发、安静多久才惦记你也是每天现定的；同类项目大多只有被动回复或简单的定时推送。
- **风格学习**：说话方式旋钮可随聊天反馈缓慢微调（tune），也支持聊天内导演指令实时调整语气；人设不是一段写死的提示词。
- **真人感内容**：内容包机制管理场景、衣橱、妆容与表情，配合可选的图/视频生成与音频交付检查，目标是「像真人在过日子」，不只是问答。
- **轻量**：单机 Python + 本地文件即可运行，没有 Web 后台、数据库与插件市场的部署负担。
- **取舍**：只支持微信、只支持一对一；需要多平台、群管理或企业客服的场景，本项目不适合，请选平台型项目。

## 架构（文字版）

```
微信用户
  ↕  (ClawBot 通道收发)
bridge（收发层：登录会话、消息去重、气泡拆分与发送节奏）
  ↕  inbox/ · outbox/（agent 模式） 或 进程内调用（api 模式）
worker（简报 / 生成层：组装人设+事实+历史 → 生成回复）
  ├── proactive（主动决策：时段窗、概率、静默跟进、情绪模型）
  ├── tune（风格调节：长度/提问/表情等旋钮 + 隐式反馈学习）
  └── content/（内容包：衣橱、场景卡、表情、妆容）
state/（本地状态：会话、历史、计数、每日简报；只存本机，禁止提交 git）
```

## 快速开始

1. **在微信里申请开通 ClawBot 机器人**：按微信官方 ClawBot 的指引完成机器人开通（以官方当期流程为准），确保可以用该机器人收发一对一消息。
2. **复制三件套示例并填写**：
   ```bash
   cp config.example.json  config.json
   cp persona.example.json persona.json
   cp facts.example.json   facts.json
   ```
   按 `docs/configuration.md` 填写；API 密钥不要写进任何 JSON，只设置环境变量（变量名由 `llm.api_key_env` 指定，例如 `export COMPANION_LLM_API_KEY=...`，也可放进本地 `.env`）。
3. **扫码登录**：运行登录命令后用微信扫码（具体命令见随附代码说明）；登录会话保存在本地 `state/`，不要分享、不要提交。
4. **启动**：`./start.sh`（或参考 `clawbot-companion.service` 用 systemd 托管，先按文件内注释改占位路径与用户）。Windows 用 `start.bat` / `start.ps1`，效果等价。
5. **测试**：用你自己的微信给机器人发一条消息，确认能收到符合人设的回复；再检查主动消息开关（默认示例开启，可先关掉 `proactive.enabled` 观察几天）。

### Windows

平台支持：**Linux 与 Windows 均可完整部署运行**（Python 3.10+，推荐 3.12）。Windows 路径与 Linux 的差异只有三处：依赖多装一个 `tzdata`（已写进 `requirements.txt` 并限定仅 Windows 安装）、启动脚本换 `start.bat` / `start.ps1`、常驻方式换任务计划程序或 NSSM/WinSW 服务。完整步骤（扫码登录、计划任务开机自启、服务注册、编码注意事项）见 `docs/windows.md`。

## 功能清单

- 一对一聊天收发、长消息拆气泡、发送节奏与字数/问号/emoji 上限
- 人设系统：`persona.json` + `facts.json` + 内容包，保持角色与生活细节一致
- 两种生成模式：`api`（兼容接口直连）/ `agent`（文件唤醒，见 `docs/agent-mode.md`）
- 主动消息决策：时段窗与概率、静默跟进、随机 impulse；每日总量默认不限（`daily_max` 可选数字上限），未回复走情绪模型调制而非熔断——概率衰减、间隔拉长、偶尔轻触，他一回复立即清零
- 有机主动引擎（`proactive.mode="organic"`）：每日按日期种子采样生活计划——锚点时刻带随机抖动与当日跳过、生活事件驱动分享、静默阈值与发送间隔按日采样；带跨日延续（心情延续、事件防重复、作息小步漂移）；fixed 保留兼容
- 生活圈子与主人适配（`life.py`）：persona/facts 的 circle / habits / routine 生成她的每日生活状态（日程、事件、心情），聊天与主动消息共用；主人作息画像只存本地、偏移有封顶、可整体关闭
- 风格调节（tune）：多个说话方式旋钮，可随反馈缓慢学习（可关闭）
- 可选媒体与配音接口：默认关闭，接入点见 `docs/media-and-tts.md`
- 本地状态与备份友好：状态集中在 `state/`，便于迁移与清理
- 跨平台部署：Linux（systemd）与 Windows（任务计划 / 服务）均可运行，自检套件在双平台 CI 持续验证

## 配置说明摘要

三件套分工：`config.json` 管行为参数（模型、主动、限额、媒体、时区、目录）；`persona.json` 管角色是谁；`facts.json` 管角色的生活事实。逐字段说明见 `docs/configuration.md`。示例人设在 `personas/`，内容包示例在 `content/`。

## 安全须知

- `session` 与 `state/` 只存本地，包含登录会话与聊天历史，**切勿提交 git、切勿上传网盘公开链接**；`.gitignore` 已默认排除它们与正式三件套文件。
- API key 一律走环境变量，不要写进 JSON、脚本或 service 文件。
- 本项目是虚构角色陪伴工具：角色应在被问及时诚实表明自己是 AI；不要用它冒充真人、不要用于骚扰式群发。
- 聊天数据在你自己的机器上，请自行做好磁盘加密与备份。

## 路线图

- [ ] 更完整的安装脚本与健康检查命令
- [ ] 内容包校验工具（字段完整性检查）
- [ ] 多角色切换命令
- [ ] 可插拔媒体/TTS 参考实现（当前仅接口约定）

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。维护约定：后续每次更新都在 CHANGELOG 顶部新增一段（Added / Changed / Removed 分类），与代码变更同一提交进仓，仓库页面即可看到每次更新了什么。

## 许可

MIT，见 [LICENSE](LICENSE)。
