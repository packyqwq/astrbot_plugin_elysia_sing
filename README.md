# astrbot_plugin_elysia_sing

爱莉唱歌：让 AstrBot 角色真的唱歌，不是念歌词。用户说“唱一段小幸运的副歌”之类的话，LLM 会调用 `elysia_sing` 工具；插件后台处理后发送约 20-25 秒的演唱语音。

## v2.0.0：只需火山引擎一个平台

v2.0.0 将依赖收敛到火山引擎：实时对话、Seed TTS 和 SAUC 语音识别共用同一个 API Key。配置面板只有两项必须填写：`volc_api_key` 与 `volc_speaker`。

从 v1 升级时，旧版其他平台的配置项已废弃并删除；新增 `volc_tts_speaker`，用于合成发送给实时引擎的点歌指令语音。

完整变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 前置要求

### 火山引擎服务与声音复刻

需要在火山引擎开通豆包实时语音大模型服务，并开通 Seed TTS 与 SAUC 语音识别服务。三个服务使用同一个火山 API Key。

还需要自己训练一个声音复刻音色。音色是账号私有资产，不能从别人账号直接共享过来；想让角色用自己的声音唱歌，必须在自己的账号中训练。

**唱歌能力取决于训练素材，不取决于模型本身。** 使用真人**歌声**素材训练的音色才能唱歌；用日常说话素材训练出来的音色，通常只会用说话的调子朗读歌词。

建议准备约 **8 分钟纯人声**素材，越干净越好，尽量不要有伴奏。带伴奏的歌曲可先用 [demucs](https://github.com/facebookresearch/demucs) 分离人声轨。训练时如有“降噪”选项，建议关闭；它可能削掉气声和颤音，影响演唱效果。

训练完成后会得到形如 `S_xxxxxxxx` 的音色 ID，填入 `volc_speaker`。

### 两个音色参数不要填反

| 配置项 | 填什么 | 用途 |
|---|---|---|
| `volc_speaker` | 自己训练的实时对话音色 ID，形如 `S_xxxxxxxx` | 决定实际唱歌的声音，必须填写 |
| `volc_tts_speaker` | 豆包官方语音合成音色名，默认 `zh_female_vv_uranus_bigtts` | 只合成“请唱XX”这句发给引擎的指令语音，不影响唱歌音色，通常不用改 |

两者不通用。把 `S_xxxxxxxx` 填进 `volc_tts_speaker` 会报 `resource ID is mismatched with speaker related resource`。

### ffmpeg 与 ffprobe

插件依赖 `ffmpeg` 处理、转码和裁剪音频，依赖 `ffprobe` 读取音频信息；这是两个不同的可执行文件，都需要可用。

| 系统 | 安装命令 |
|---|---|
| Windows 10/11 | `winget install ffmpeg` |
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| CentOS / RHEL / Fedora | `sudo dnf install ffmpeg` |
| 较旧的 CentOS / RHEL | `sudo yum install ffmpeg` |
| Arch Linux | `sudo pacman -S ffmpeg` |

Windows 也可从 <https://ffmpeg.org/download.html> 下载 gyan.dev 的 `essentials` ZIP，解压后在插件设置中填写完整路径，例如 `C:\ffmpeg\bin\ffmpeg.exe` 和 `C:\ffmpeg\bin\ffprobe.exe`，不必配置 PATH。手动安装时确认 `bin` 下同时有 `ffmpeg.exe` 和 `ffprobe.exe`。

安装后可运行 `ffmpeg -version` 检查。详细安装与排错步骤见 [使用说明.md](使用说明.md)。

### Python 依赖

AstrBot 环境通常已有 `httpx` 和 `websockets`。若缺失，请使用 AstrBot 自身 venv 的 pip 安装：

```bash
/path/to/astrbot/venv/bin/pip install httpx websockets
```

## 安装与快速配置

1. 将插件目录放入 AstrBot 的 `data/plugins/`。
2. 在 WebUI 的插件设置中填写 `volc_api_key` 和 `volc_speaker`。
3. 确认 `ffmpeg` 与 `ffprobe` 已安装，或填写它们的完整路径。
4. 重启 AstrBot，使插件加载新配置。

之后直接对机器人说“唱一段小幸运”或“唱一下告白气球的副歌”。插件先回复已提交，语音通常在十几秒到一分多钟后送达。

## 配置项说明

| 字段名 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用爱莉唱歌插件 |
| `volc_api_key` | `""` | 火山引擎 API Key，TTS、ASR、实时对话共用；**必填** |
| `volc_speaker` | `""` | 实时对话音色 ID，形如 `S_xxxxxxxx`；**必填，决定唱歌声音** |
| `volc_tts_speaker` | `zh_female_vv_uranus_bigtts` | 合成点歌指令语音的官方音色，一般不用改 |
| `target` | `25.0` | 默认语音片段目标时长，单位秒 |
| `window` | `8.0` | 裁剪目标时长允许的搜索窗口，单位秒 |
| `fade_out` | `1.5` | 裁剪片段淡出时长，单位秒 |
| `fade_in` | `0.5` | 歌词定位片段淡入时长，单位秒 |
| `s2s_retries` | `5` | 实时对话失败重试次数 |
| `s2s_timeout` | `90.0` | 单次实时对话总超时，单位秒 |
| `max_concurrent` | `2` | 全局同时处理的唱歌任务数 |
| `queue_timeout` | `120.0` | 排队等待并发槽位的最长时间，超时放弃本次唱歌，单位秒 |
| `cooldown` | `120.0` | 同一用户完成后的冷却时间，单位秒 |
| `retention_hours` | `24.0` | 成品文件保留时间，单位小时 |
| `notify_llm_on_done` | `true` | 后台任务完成或失败后，是否唤起主对话模型自然通知用户 |
| `notify_max_steps` | `8` | 后台结果通知主对话模型的最大执行步数 |
| `ffmpeg_path` | `ffmpeg` | ffmpeg 可执行文件路径；可填完整路径 |
| `ffprobe_path` | `ffprobe` | ffprobe 可执行文件路径；可填完整路径 |

## 已知限制

- 引擎唱哪个段落不完全受控。同一首歌多次点播，可能唱主歌、副歌或不同版本的段落。插件会用歌词匹配尽量定位用户指定片段；匹配不上时仍会发送音频，并说明唱的是其他段落。
- 有一定概率引擎拒唱、只哼两声或跑调。插件会校验并拦下无法使用的结果，返回失败原因；实测成功率约 **2/3**。
- 每次输出约 20-25 秒，不是整首歌。引擎给出的长度也不完全受控，插件只能裁剪，不能补长。
- 歌名可能被识别错，例如《星晴》被识别成“心情”，导致点歌失败或唱错歌；可换一种说法再试。

## 工作原理

1. Seed TTS 将“请唱歌名”合成为指令音频。
2. 实时对话引擎接收该音频并生成演唱。
3. SAUC 流式识别演唱内容，插件根据歌词尽量定位目标片段。
4. ffmpeg 裁剪并转为可发送的语音文件。

v2.0.0 的代码还修复了重复调用 ASR、WAV 扩展名与实际编码不符、精准歌词匹配被误丢弃的问题；同时增加二进制帧边界校验、gzip 解压上限与 API Key 脱敏，避免异常格式化时泄露凭据。

## selftest.py

默认自检只检查导入、配置 schema 与纯逻辑，不调用网络：

```bash
python selftest.py
```

用 `--live CONFIG.json SONG` 可实际运行完整链路，会消耗火山引擎配额：

```bash
python selftest.py --live config.json 小幸运 --lyrics "与你相遇好幸运"
```

## License

见 [LICENSE](LICENSE)。
