# Agent 模式契约（文件唤醒）

当 `config.json` 的 `llm.mode` 为 `agent` 时，bridge 不直接调用模型 API，而是通过本地文件队列与一个外部 agent（可以是任何能读写文件的程序/助手运行时）协作。`api` 模式不需要本契约。

## 总览时序

1. bridge 收到用户消息 → 写入 `inbox/` 一条入站文件，并更新唤醒信号（如同目录的信号文件或外部 hook，具体触发方式由部署方实现）。
2. agent 被唤醒 → 读取入站文件与必要的只读上下文（人设、事实、近期历史摘要）→ 生成回复。
3. agent 把回复写为 `outbox/` 一条出站文件。
4. bridge 轮询/监听 `outbox/` → 按出站文件发送，并把文件移入已处理归档或删除。

## inbox/ 入站文件格式

文件名建议：`<timestamp>-<message_id>.json`，UTF-8 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 入站消息唯一 ID（幂等键） |
| `ts` | string | ISO 8601 时间（含时区） |
| `from` | string | 发送方标识，固定为 `user`（一对一场景） |
| `text` | string | 消息正文；媒体消息时可为空 |
| `media` | object \| null | 可选：`{type: "image"|"video"|"file", path: 本地相对路径}` |
| `context_token` | string | 通道回消息所需的上下文令牌（如平台要求）；只在本机流转，不外传 |
| `reply_to` | string \| null | 被引用的消息 ID，没有则为 null |

## outbox/ 出站文件格式

文件名建议与入站对应：`<timestamp>-<in_reply_to>.json`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 出站唯一 ID（幂等键），建议由入站 `id` 派生 |
| `in_reply_to` | string | 对应入站消息 `id`；主动消息填 `null` 并置 `proactive: true` |
| `proactive` | bool | 是否主动消息 |
| `bubbles` | string[] | 按顺序发送的气泡文本数组，长度与字数受 `limits` 约束 |
| `media_file` | string \| null | 可选：待发送媒体的本地相对路径 |
| `media_type` | string \| null | `image` / `video`，与 `media_file` 同时出现 |
| `created_at` | string | ISO 8601 时间（含时区） |

## 时序与幂等要求

- **幂等**：bridge 以出站 `id` 去重；同一 `id` 的文件重复出现只发送一次。agent 重试时必须复用同一 `id`，不要生成新 ID 重发同一回复。
- **顺序**：同一轮的多个气泡只写在一个出站文件里（`bubbles` 数组），不要拆成多文件竞速。
- **超时与过期**：入站文件超过部署方约定的时限（如 30 分钟）未被处理，agent 应放弃生成迟到回复，避免上下文错位；bridge 侧同样应丢弃过期出站文件并记录日志。
- **原子写**：先写临时文件再重命名为正式文件名，防止 bridge 读到半截 JSON。
- **单写者**：同一时刻只允许一个 agent 实例消费 `inbox/`；处理前先把文件移入处理中目录或加锁，避免重复处理。
- **失败处理**：生成失败时写一条仅含空 `bubbles` 的出站文件并带 `error` 字段说明，或干脆不写并留下日志——两种约定选一并在部署文档中写明，不要静默吞掉。
