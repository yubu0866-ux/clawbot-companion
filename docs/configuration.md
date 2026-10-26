# 配置说明（三件套逐字段）

三件套均为 JSON。仓库只提交 `*.example.json`；正式文件（`config.json` / `persona.json` / `facts.json`）已被 `.gitignore` 排除。`config.example.json` 中以 `_about_<key>` 命名的同级字符串键是对相邻键的说明，程序会忽略所有 `_about_*` 键，保留它们不影响运行，删掉也可以。

本文的口径以代码实际读取为准：照着 `config.example.json` 抄一份 `config.json` 即可跑通，不需要再改键名或结构。

## config.json

### 顶层

| 字段 | 类型 | 说明 |
|---|---|---|
| `tz` | string | IANA 时区名，如 `Asia/Shanghai`。键名就是 `tz`（bridge / proactive / tune 都读它），不是 `timezone` |
| `content_pack_dir` | string | 内容包目录（相对数据目录或绝对路径），默认 `content` |
| `nickname_for_me` | string | （可选）角色对使用者的昵称等顶层行为键，缺失时用代码默认值 |

运行状态不在配置里指定目录：状态固定落在数据目录的 `state/` 下，数据目录由环境变量 `COMPANION_HOME` 决定（默认程序所在目录）。

### llm

| 字段 | 类型 | 说明 |
|---|---|---|
| `llm.mode` | string | `api`：直连兼容接口；`agent`：文件唤醒模式（见 agent-mode.md） |
| `llm.base_url` | string | 兼容 OpenAI 格式的接口基址（api 模式用） |
| `llm.model` | string | 模型名，以服务商提供为准 |
| `llm.api_key_env` | string | 存放密钥的**环境变量名**，不是密钥本身。代码在该字段为空/缺失时的默认名是 `COMPANION_LLM_API_KEY`；示例配置也填这个名字，部署时 `export COMPANION_LLM_API_KEY=...` 即可 |
| `llm.temperature` | number | 采样温度，建议 0.5–0.9 |
| `llm.timeout_sec` / `llm.max_calls_per_day` | number | 单次超时与每日调用上限（成本护栏） |

### proactive（主动消息）

结构与 `proactive.py` 的实际读取完全一致：**扁平键直接挂在 `proactive` 对象上，节奏/调谐键集中在 `proactive.rhythm` 里，时段窗在 `proactive.slots` 里**。代码里没有 `proactive.impulse{}`、`proactive.silence{}` 这类嵌套对象——impulse 的键在 `rhythm` 里，静默触发的键是扁平键，写成嵌套对象不会生效。

| 字段 | 类型 | 说明 |
|---|---|---|
| `proactive.enabled` | bool | 主动消息总开关 |
| `proactive.daily_max` | number | 主动消息每日条数上限（观察期过后生效，见下两行） |
| `proactive.observation_days` / `proactive.observation_cap` | number | 观察期：首次启动后前 N 天每日上限为 `min(observation_cap, daily_max)`；其间若已发的主动条条被回复则提前放开到 `daily_max` |
| `proactive.quiet_hours` | [number, number] | 静默时段起止小时（本地时间），如 `[1, 7]` |
| `proactive.min_gap_hours` | number | 两条主动消息最小间隔（小时）的兜底值；正常节拍由 `rhythm.gap_hours` 随机化决定，此键只在没有随机节拍记录时兜底 |
| `proactive.max_context_age_hours` | number | 上下文超过该时长则不主动发消息（bridge 侧同读此键） |
| `proactive.active_skip_min` | number | 用户最近 N 分钟内在聊天则跳过主动消息 |
| `proactive.unresolved_max` / `proactive.unreplied_stop` | number | 未回复主动消息的并存上限 / 连续未回复多少条后停发 |
| `proactive.slots.<name>.window` | [string, string] | 时段窗起止 `HH:MM`；`<name>` 为意图名：`morning` / `lunch` / `evening` / `goodnight` |
| `proactive.slots.<name>.p` | number | 该时段触发概率 0–1（连败两天有保底必中、连中两天概率减半的修正由代码处理） |
| `proactive.silence_hours` | number | **扁平键**：用户静默至少该时长后，长静默消息才可 roll（傍晚/周末白天改用 `rhythm.silence_evening_hours`） |
| `proactive.silence_p` | number | **扁平键**：长静默触发概率，默认 0.85 |
| `proactive.silence_window` | [number, number] | **扁平键**：长静默可触发时段 `[起, 止)`（小时），默认 `[9, 22]` |
| `proactive.send_jitter_min` | [number, number] | 仅 followup（约定跟进）消息的发送抖动分钟范围；其余意图走 `rhythm.jitter_min`（上限封顶 10 分钟），impulse 固定 0–3 分钟 |
| `proactive.flavors` | string[] | 主动消息的内容口味轮换表，逐条随机取且避开最近用过的；缺失时用代码内置的默认口味表 |
| `proactive.photo_subjects` / `proactive.video_subjects` | string[] | 主动照片/视频的主体表：首项为「本人出镜」类主体，按 `rhythm.photo_self_p` / `video_self_p` 概率选中，其余项按日期轮换 |

`proactive.rhythm`（节奏子表，全部可缺省，缺失走括号内默认值）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `rhythm.gap_hours` | [number, number] | 一条主动记账后，下一次可主动的随机间隔（小时）范围（默认 `[2.5, 4.5]`，示例给 `[0.4, 1.0]`） |
| `rhythm.jitter_min` | [number, number] | 时段/静默类消息的发送抖动分钟范围（上限封顶 10） |
| `rhythm.followup_expire_hours` | number | 约定跟进（followup）的过期时长，超过不再提（默认 48） |
| `rhythm.reply_window_hours` / `rhythm.cooldown_hours` | number | 判定一条主动「被回复」的窗口 / 连续被晾后的冷却时长 |
| `rhythm.silence_evening_hours` | number | 傍晚（17–22 点）与周末白天用的静默阈值（小时，默认 3） |
| `rhythm.impulse_daily_max` / `rhythm.impulse_p` | number | 时段外随机主动（impulse）的每日条数上限（默认 1）与每轮触发概率（默认 0.03）。注意这两个键在 `rhythm` 里 |
| `rhythm.double_p` | number | silence/evening/lunch 意图偶尔二连发的概率（默认 0.18） |
| `rhythm.photo_p` / `rhythm.video_p` | number | 主动媒体形式裁定的相对权重：照片可达意图 morning/evening/silence，视频可达 evening/silence/impulse；同为可达时按权重二选一，两者都为 0 则纯文字（默认各 1.0）。分形式条数不单独设限，只受 `daily_max` 总量约束 |
| `rhythm.photo_self_p` / `rhythm.video_self_p` | number | 媒体主体抽中首项（本人出镜）的概率（默认 0.7） |

### limits（数值硬门）

`bridge.py` 的发送侧数值硬门（只管 `kind="reply"` 轮次，违规整组作废留痕）**优先读本段**，键名如下；任一键缺失或非法时回落到代码内的同值常量默认。所以调小这里的某个上限，硬门判定会随之变严，不需要改代码。

| 字段 | 类型 | 说明 |
|---|---|---|
| `limits.max_bubbles` | number | 单轮回复最多气泡（条）数（默认 3） |
| `limits.max_chars_per_bubble` | number | 单条气泡字数上限（默认 90） |
| `limits.max_chars_per_reply` | number | 整轮回复总字数上限（默认 300） |
| `limits.max_question_marks` | number | 整轮问号数上限（半角 `?` 与全角 `？` 合计，默认 2） |
| `limits.max_emoji` | number | 整轮 emoji 数上限（默认 2） |
| `limits.max_bracket_expressions` | number | 整轮方括号表情（如 `[微笑]`）数上限（默认 2） |
| `limits.bracket_window_rounds` | number | 方括号表情的滚动统计窗口（最近 N 个回复轮次，默认 5） |
| `limits.max_bracket_in_window` | number | 窗口内方括号表情合计上限（默认 4） |
| `limits.max_media_per_reply` | number | 单轮媒体条数上限（默认 1） |

### media（可选接入约定）

`media.photo_enabled` / `media.video_enabled`（bool，默认关闭）与 `media.photo_weight` / `media.video_weight` 是给部署方自接媒体生成器用的约定开关（见 media-and-tts.md），**核心代码不直接读本段**；主动消息是否带媒体由 `proactive.rhythm.photo_p` / `video_p` 裁定，不要在两处同时调权重。

## persona.json

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 角色名（虚构） |
| `gender` / `age` / `job` | string / number / string | 性别、年龄、职业（虚构设定） |
| `personality` | string[] | 性格关键词数组 |
| `speaking_style` | string | 说话方式的自由描述，会进入生成提示 |
| `call_user` | string | 角色对使用者的称呼，如「你」 |
| `call_self` | string | 角色自称，如「我」 |
| `relationship` | string | 关系定位，如朋友型陪伴、兄妹式朋友等 |
| `life` | object | 自由字段：住处、宠物、爱好、工作日常、偏好等，键可增减 |
| `boundaries` | string[] | 底线数组；至少保留「被问身份时诚实表明是 AI」一条 |

## facts.json

**规范结构**是一个对象，含三个键：`facts`、`user_facts`、`life`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `facts` | string[] | 角色事实列表（规范字段）。`worker.py` 优先读它；日常维护只维护这一处 |
| `user_facts` | array | 关于**使用者**的长期事实，按重要度在前排序。条目可为字符串，或对象 `{"fact": "...", "status": "active"}`（`status` 缺省即 active，非 active 的条目会被跳过） |
| `life` | object | 每日生活生成池，逐键覆盖代码内置的中性默认池（见下） |
| `life.moods` | object | 心情池，按忙碌档分键：`{"low": [...], "mid": [...], "high": [...]}` |
| `life.schedules` | object | 日程池，同样按 `low` / `mid` / `high` 分键 |
| `life.small_events` | string[] | 每日小事件池；每天抽 2 条，且尽量保证至少 1 条命中 `people_keywords` 的人际类事件 |
| `life.occasional_events` | string[] | 偶尔档小事件池（周末/节日/不忙时才并入抽取） |
| `life.seeds` | string[] | 话题种子池，每天随机取至多 3 条 |
| `life.people_keywords` | string[] | 人际类事件的识别关键词，如朋友/同事/家人 |
| `life.people` | string[] | 核心人际名单（用角色口中的称呼词即可，如室友/同事/家人，不要写真实姓名）；用于人际事件与「很久没提起谁」的检查，缺省为空 |

**与主题分节结构的关系**：`facts.example.json` 底部还保留了旧式的主题分节写法（键是分类名如 `home_and_daily`、`work`、`food`、`hobbies`、`pet`、`preferences`、`schedule`，值是事实字符串数组）。代码对它的处理是**容错回落**：只有当 `facts` 字段不存在时，才会把主题分节里的字符串递归扁平化当作事实列表；一旦 `facts` 存在，主题分节会被忽略。`user_facts` 与 `life` 则始终独立读取，不受这个回落影响。因此：新部署直接用规范结构（事实统一写进 `facts`）；主题分节只适合给还没迁移的旧文件兜底，两处不要同时维护同一条事实。只写角色设定内的事实，不写任何真实个人信息；分类与条目可按角色需要增减。
