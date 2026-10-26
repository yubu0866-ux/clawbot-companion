# 媒体生成与配音/TTS（可选接口）

媒体与配音都是**可选能力，默认关闭**（`config.json` 的 `media.photo_enabled` / `media.video_enabled` 均为 `false`）。本项目只约定接入点，不绑定任何具体厂商，也不包含任何声音克隆流程。

## 生成器接口约定

部署方如需图片/视频能力，实现一个生成器并在 worker 生成层调用即可。约定（语言无关的逻辑接口）：

```
generate(kind, prompt, references, options) -> { file_path, width, height, duration_sec? }
```

| 参数 | 说明 |
|---|---|
| `kind` | `image` 或 `video` |
| `prompt` | 文本提示；应结合内容包（衣橱/场景卡/表情/妆容）组装，保证角色与场景一致 |
| `references` | 参考图本地路径数组（可为空）；只使用部署方有权使用的素材，本地素材不要擅自上传到公网服务 |
| `options` | 尺寸、时长等可选参数 |

返回值 `file_path` 为本地相对路径，随后通过出站通道发送（agent 模式下填入出站文件的 `media_file` / `media_type`）。

行为约定：

- 生成失败时降级为纯文字回复，不要让整轮聊天失败。
- 主动消息是否带媒体由 proactive 决策层按 `media.*_weight` 裁定一次，生成层不要二次随机。
- 生成内容应与人设与内容包一致，并遵守所用生成服务的内容政策。

## TTS / 配音接入点（可选）

如需语音消息或为视频配音，在媒体流水线的「拿到最终文本之后、合成/发送之前」接入任意 TTS 服务：

```
synthesize(text, voice_options) -> { audio_path, duration_sec }
```

- `voice_options` 只放通用参数（如语言、语速、音量），音色选择由部署方在服务商侧自行配置。
- 文本应先按气口切分短句再逐段合成，段间留自然停顿；合成后统一响度再与视频/消息合并。
- 本文档不提供、不建议任何针对真人声音的克隆流程；使用他人声音须有明确授权。
- TTS 不可用时降级为文字消息即可。
