# 流光翻唱 liuguang_sing

AstrBot 插件：群里 **@流光 翻唱** + 附带/引用一个 mp3 文件，调用 **MiniMax music-cover** 生成翻唱歌曲，并把结果以语音消息发回群聊。

> 仅对 `aiocqhttp` 平台（NapCat / OneBot11）生效。

## 效果流程

```
有人发 mp3 文件到群里
  ↓
@流光 翻唱（可加风格描述，如 "翻唱 改成摇滚风格"）
  ↓
流光回复"正在翻唱中…"（预计 1~3 分钟）
  ↓
MiniMax music-cover-free 生成翻唱
  ↓
流光把翻唱音频作为语音消息发到群里
```

## 安装

将本目录放入 `AstrBot/data/plugins/` 后重启 AstrBot，或在 AstrBot WebUI → 插件市场 → 本地安装 中导入。

## 配置

在 AstrBot WebUI → 插件 → liuguang_sing 中配置：

| 配置项 | 说明 | 默认 |
|---|---|---|
| `api_key` | MiniMax API Key（与 TTS 同一账户即可） | 空 |
| `api_base` | MiniMax API 地址 | `https://api.minimax.chat` |
| `model` | 翻唱模型 | `music-cover-free` |
| `default_prompt` | 默认翻唱风格描述 | `原曲风格翻唱，深情演绎` |
| `cooldown_seconds` | 翻唱冷却秒数（防刷限免额度） | `300` |
| `waiting_msg` / `success_msg` / `fail_msg` | 各环节提示语模板 | - |

## 使用说明

- 支持：消息内直接带文件，或 **引用** 一条含 mp3 文件的消息
- 音频要求（MiniMax 官方限制）：时长 6 秒 ~ 6 分钟，大小 ≤ 50MB，常见格式（mp3/wav/flac/m4a 等）
- **必须是人声歌曲**：纯音乐、说话语音会被模型拒绝（`no lyrics detected` / `invalid audio file`）
- 不支持 QQ 语音消息（silk 格式）作为输入
- 限免模型 `music-cover-free` RPM=3（每分钟 3 次），插件内置冷却保护；付费可用 `music-cover`（RPM=120）

## 技术说明

- 解密/转码链路（酷狗等加密格式 → mp3）：可配合 [unlock-music um CLI](https://git.unlock-music.dev/um/cli) 使用
- 翻唱本质是"提取旋律骨架 + AI 重新演绎"，输出音色为 MiniMax 音乐模型默认人声，**不是原唱音色**，也无法指定流光/自定义音色
- MiniMax 官方接口：`POST /v1/music_generation`（music-cover / music-cover-free）

## 许可证

MIT
