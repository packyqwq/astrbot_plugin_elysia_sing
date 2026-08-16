# astrbot_plugin_elysia_sing

爱莉唱歌：联网歌词定位后生成并发送语音消息。

## 功能说明

用户对角色说"唱一段小幸运的副歌"之类的话,LLM 会调用本插件的 `elysia_sing` 工具。工具立即返回"已提交,稍等"的提示,后台跑完整条链路(约 60-90 秒)后,以语音消息的形式发一段 20-25 秒的演唱片段给用户——用的是角色自己训练的音色在唱,不是文字转语音朗读。

## ⚠️ 前置要求

这是本插件门槛最高的部分,请仔细看完再决定是否安装。

### 1. 火山引擎豆包实时语音大模型

需要开通火山引擎的**豆包实时语音大模型**服务(`/api/v3/realtime/dialogue`),拿到 API Key,并且**自己训练一个声音复刻音色**。音色是账号私有资产,官方不提供、也无法从别处共享给你——想让"你的角色"唱歌,必须用你自己的账号训练属于这个角色的音色。

关键坑点:

- **唱歌能力取决于训练素材,不取决于模型本身**。用真人**歌声**素材训练出来的音色才能唱歌;用日常说话素材训练的音色只会用说话的调子朗读歌词,唱不出旋律。
- 素材建议准备约 **8 分钟纯人声**,可以用 [demucs](https://github.com/facebookresearch/demucs) 从任意歌曲里分离出人声轨作为素材。
- 注册音色时把 `enable_audio_denoise` 设为 `false`。降噪会把气声和颤音一起削掉,唱歌的表现力会明显变差。

训练完成后,拿到的**槽位音色 ID**(形如 `S_xxxxxxxxxx`)填入配置项 `volc_speaker`。

### 2. MiniMax 语音合成(推荐)

用来合成"请唱 XXX"这句点歌指令的语音,发给豆包当作对话输入。需要 MiniMax 的 API Key。

也可以改用火山自己的 TTS(`/api/v3/tts/unidirectional`,`X-Api-Resource-Id: volc.megatts.default`)代替 MiniMax,这样能少一个外部依赖,但合成出来的点歌语音音色贴合度可能不如 MiniMax,需要自行改代码接入(当前代码里写的是 MiniMax 调用,见 `_sing_core.py` 的 `synth_query`)。

### 3. 阿里云 DashScope(用于识别演唱内容)

模型是 `qwen-audio-3.0-asr-flash`,用来把豆包唱出的音频转成文字并拿到词级时间戳,插件靠这个时间戳定位歌词片段、判断有没有唱成功。

**必须用公共端点** `https://dashscope.aliyuncs.com/api/v1`,私有 maas 网关对这个模型会返回 400 + 空 body,实测不可用。

### 4. ffmpeg

ffmpeg 是插件用来处理和剪辑音频的免费工具,`ffprobe` 用来读取音频时长和采样率。两者都是独立的可执行文件,都必须安装。

插件不把 ffmpeg 打包进来,原因是：

- 动态构建依赖约 215 个动态库,难以随插件完整携带；
- 静态版本单个平台约 150 MB,同时支持多个平台会超过 300 MB；
- ffmpeg 涉及 GPL 许可,随插件分发还需要处理相应的合规要求。

#### 安装命令

| 平台 | 命令 |
|---|---|
| Windows 10/11（有 winget） | `winget install ffmpeg` |
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| CentOS / RHEL / Fedora | `sudo dnf install ffmpeg`（较旧系统可用 `sudo yum install ffmpeg`） |
| Arch Linux | `sudo pacman -S ffmpeg` |

Windows 也可以手动安装：从 <https://ffmpeg.org/download.html> 进入 Windows 的 **gyan.dev** 构建下载页,下载 `essentials` ZIP,解压到固定目录（例如 `C:\\ffmpeg`）,再在插件配置中填写 `C:\\ffmpeg\\bin\\ffmpeg.exe` 和 `C:\\ffmpeg\\bin\\ffprobe.exe`。这种方式不需要配置 PATH。

安装后可运行 `ffmpeg -version` 验证。包管理器通常会同时安装 `ffmpeg` 和 `ffprobe`;手动安装时要确认 `bin` 目录中两个文件都存在。

#### 路径配置与优先级

`ffmpeg_path` 和 `ffprobe_path` 默认分别为 `ffmpeg`、`ffprobe`,表示从系统 `PATH` 查找。也可以填对应可执行文件的完整路径,例如 Windows 下的 `C:\\ffmpeg\\bin\\ffmpeg.exe` 和 `C:\\ffmpeg\\bin\\ffprobe.exe`。配置项优先于 `PATH`:填写后插件使用配置的路径,只有留空（或使用默认文件名）时才依赖系统 `PATH`。启动时找不到文件只会记录 warning,但实际唱歌时会失败。

`imageio-ffmpeg` 这个 pip 包可以提供 ffmpeg 二进制,但不包含 `ffprobe`;当前版本不能只安装它来运行,仍需另外提供 `ffprobe`。

### 5. Python 依赖

以下清单是实际逐个扫描插件目录下所有 `.py` 文件的 `import`/`from` 语句得出的,不是凭印象列的:

| 库 | 是否需要额外安装 | 说明 |
|---|---|---|
| `httpx` | 需要额外安装 | AstrBot 自带的 venv 里实测已包含(0.28.1),多数环境应该都有,没有的话装一下 |
| `websockets` | 需要额外安装 | AstrBot 自带的 venv 里实测已包含(15.0.1),多数环境应该都有,没有的话装一下 |
| `asyncio` / `os` / `shutil` / `time` / `uuid` / `pathlib` / `gzip` / `json` / `struct` / `argparse` / `base64` / `logging` / `subprocess` / `typing` / `difflib` / `re` / `tempfile` / `sys` | 不需要 | Python 标准库 |
| `astrbot.api.*` / `astrbot.core.*` | 不需要 | AstrBot 框架自带 |

如果 `httpx` 或 `websockets` 在你的 AstrBot 运行环境里缺失,用 AstrBot 所在的 venv 装:

```bash
/path/to/astrbot/venv/bin/pip install httpx websockets
```

## 安装步骤

1. 解压插件包到 AstrBot 的 `data/plugins/` 目录下,得到 `data/plugins/astrbot_plugin_elysia_sing/`
2. 重启 AstrBot
3. 在 WebUI 的插件配置页面里填好 `volc_api_key`、`minimax_api_key`、`aliyun_asr_api_key` 三个 Key,以及你自己训练的 `volc_speaker` 音色 ID 和你自己创建的 `minimax_voice` 音色 ID

## 配置项说明

| 字段名 | 说明 | 默认值 | 必填 |
|---|---|---|---|
| `enabled` | 是否启用爱莉唱歌插件 | `true` | 否 |
| `volc_api_key` | 豆包实时语音 API Key | `""` | **是** |
| `volc_app_key` | 豆包实时语音 App Key | `PlgvMymc7f3tQnJ6` | 否 |
| `volc_resource_id` | 豆包实时语音资源 ID | `volc.speech.dialog` | 否 |
| `volc_ws_endpoint` | 豆包实时语音 WebSocket 地址 | `wss://openspeech.bytedance.com/api/v3/realtime/dialogue` | 否 |
| `volc_speaker` | 豆包槽位音色 ID(不是训练素材文件名) | `""`(如 `S_xxxxxxxx`) | **是**(填你自己训练的) |
| `minimax_api_key` | MiniMax 语音合成 API Key | `""` | **是** |
| `minimax_base_url` | MiniMax API 基础地址 | `https://api.minimaxi.com` | 否 |
| `minimax_voice` | MiniMax 查询合成音色 | `""`(填你自己创建的音色 ID) | **是**(填你自己创建的) |
| `aliyun_asr_api_key` | 阿里云 DashScope ASR API Key | `""` | **是** |
| `ffmpeg_path` | ffmpeg 可执行文件名或完整路径 | `ffmpeg` | 否 |
| `ffprobe_path` | ffprobe 可执行文件名或完整路径 | `ffprobe` | 否 |
| `target` | 默认语音片段目标时长(秒) | `25.0` | 否 |
| `window` | 裁剪目标时长允许的搜索窗口(秒) | `8.0` | 否 |
| `fade_out` | 裁剪片段淡出时长(秒) | `1.5` | 否 |
| `fade_in` | 歌词定位片段淡入时长(秒) | `0.5` | 否 |
| `s2s_retries` | 豆包 S2S 失败重试次数 | `5` | 否 |
| `s2s_timeout` | 单次豆包 S2S 总超时(秒) | `90.0` | 否 |
| `max_concurrent` | 全局同时处理的唱歌任务数 | `2` | 否 |
| `queue_timeout` | 排队等待并发槽位的最长时间(秒),超时则放弃本次唱歌 | `120.0` | 否 |
| `cooldown` | 同一用户完成后的冷却时间(秒) | `120.0` | 否 |
| `retention_hours` | 成品文件保留时间(小时) | `24.0` | 否 |

`volc_app_key`、`volc_resource_id`、`volc_ws_endpoint`、`minimax_base_url` 这几项默认值是火山/MiniMax 通用值,一般不需要改;`volc_speaker` 和 `minimax_voice` 默认值均为空,**必须分别换成你自己训练/创建出来的音色 ID** 才能唱出你的角色的声音,否则插件会在运行时报错提示未配置。

## 已知限制

以下都是实测结论,不是理论推测:

- **豆包不听长度约束**。实测"唱两句就好"、"唱一小段副歌就行"、"只唱十秒钟"、"简单唱几句不用唱完整首"四种说法全部无效,输出恒定在 49~50 秒左右。所以插件的做法是让豆包唱完整首,再自己裁剪到目标长度。
- **给歌词提示也不行**。往请求里塞歌词只会被豆包当成检索关键词用,照样从头开始唱整首歌,不会只唱给的那几句。
- **豆包 S2S 有间歇性失败**,实测同一请求连挂 4 次、第 5 次才成功过,所以默认重试次数是 5。
- **阿里云 ASR 也有间歇性失败**,报错码是 `ASR_RESPONSE_HAVE_NO_WORDS`,默认重试次数是 4。
- 曲库外的歌豆包会回答"我还不会唱呢",插件能识别这种拒绝并告知用户换一首。
- **ASR 对歌名的识别有误差**,实测误识别率约 25%(例如《星晴》被识别成"心情"、《小幸运》被识别成"畅享幸运"),可能导致点歌失败或唱错歌。
- **后台失败原因无法回传给 LLM**。`event.send()` 只把消息转发给平台适配器发出去,不会写入对话历史,所以任务失败时是插件直接发一句中性文案给用户,角色本身并不知道发生了什么、也无法针对性地回应用户的追问。
- 单次完整流程耗时约 60-90 秒;默认全局并发上限 2,单用户冷却时间 120 秒。
- **query(点歌指令)越短越准**。指令太长时容易被 ASR 判断/裁剪逻辑吃掉前半句,导致定位错乱或唱错歌。

## 工作原理

一次点歌请求的完整链路:

```
① MiniMax 合成"请唱 XXX"这句点歌语音
       ↓
② ffmpeg 转成 PCM 16k 单声道(豆包要求的输入格式)
       ↓
③ 送进豆包实时语音 S2S,角色音色开始唱(约 50 秒)
       ↓
④ 输出 PCM 转 mp3
       ↓
⑤ 阿里云 DashScope ASR 识别唱出的内容,拿到词级时间戳
       ↓
⑥ 判定这次是不是唱成功了(拒绝关键词/时长/歌词匹配度)
       ↓
⑦ 按歌词定位片段起点,或按目标时长裁剪,加淡入淡出
       ↓
以语音消息发给用户
```

## 协议要点(给想改代码的人)

豆包实时语音对话(`/api/v3/realtime/dialogue`)是二进制帧协议,以下是实测跑通的关键约束,写在这里省得下次再踩一遍坑:

- `dialog.extra.model` 必须是 `"1.2.1.1"`(即 O2.0),目前只有这个版本支持唱歌。
- `dialog.extra.enable_music` 必须是 `true`。
- `asr.extra` / `tts.extra` 不能传 `null`,至少传空对象 `{}`;并且**绝不能传 `tts_2_0_model` 字段**——实测传了这个字段会导致输出 0 字节,而且不报错,非常隐蔽。
- `tts.speaker` 必须传**槽位 ID**(`S_xxx` 形式),传 `ICL_xxx` 形式会报 resource ID mismatch。
- 输入音频格式:裸 PCM,16kHz,单声道,`s16le`;按 640 字节一包发送,每发一包 `sleep 20ms`(见 `sing_trim.py` / `_sing_core.py` 里的 `FRAME_BYTES` / `FRAME_INTERVAL`)。
- VAD 用 `server_vad`;客户端发完音频**不需要**发 `EndASR`,继续接收 30~60 秒等服务端把 TTS 音频发完。
- 音频接收循环的退出条件是收到事件 `EVENT_TTS_ENDED`(359);判定"这一帧是音频数据"的条件是 `event == EVENT_TTS_RESPONSE` 或 `message_type == MSG_TYPE_AUDIO_ONLY_RESPONSE`,两者任一为真都要当音频处理。
- 调 ASR 时 `format` 参数必须放在请求体的 `parameters` 层,放到 `asr_options` 或 `content` 里会报 `format is empty`。
- 服务端返回的 `tts_type` 是 `"sing"` 不代表一定唱成功,也可能是朗读被标成了这个类型,不能单独作为成功判据,还要结合时长和识别文本一起判断(见 `_sing_core.py` 的 `validate_output`)。

## Windows 用户注意

- ffmpeg 和 ffprobe 需要安装并加入 PATH；也可以在插件配置中填写 `ffmpeg_path` / `ffprobe_path` 的完整路径，例如 `C:\ffmpeg\bin\ffmpeg.exe` 和 `C:\ffmpeg\bin\ffprobe.exe`。
- 数据目录由框架 API 自动解析，不需要手动配置。Windows 上通常位于 AstrBot 安装目录下的 `data\plugin_data\astrbot_plugin_elysia_sing`。
- 如果遇到 `NotImplementedError` 相关报错，通常表示当前事件循环不支持子进程。插件已内置降级路径；如果仍然失败，请反馈报错信息。

## selftest.py

`selftest.py` 保留在插件包里,用于独立验证链路代码是否能正确 import、纯逻辑(歌词匹配、成功判定)是否正确,**默认不发起任何网络请求**:

```bash
python3 selftest.py
```

如果想实际跑一遍完整链路(会真实消耗火山/MiniMax/阿里云的 API 配额),用 `--live` 加上一份配置文件和歌名:

```bash
python3 selftest.py --live CONFIG.json "小幸运" --lyrics "我最亲爱的"
```

`CONFIG.json` 的字段跟 `_conf_schema.json` 里的字段一一对应,需要自己准备一份包含真实 Key 的 JSON 文件,不要把这份文件提交到版本库或打进插件包里。

## License

MIT
