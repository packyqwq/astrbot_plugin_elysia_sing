# 更新日志

本文件记录本项目的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] - 2026-08-18

### 变更

- 依赖从 4 个平台（MiniMax TTS + 火山实时语音 + 阿里云 DashScope ASR + ffmpeg）收敛到**火山引擎单平台**，TTS/ASR/实时对话共用同一个 API Key
- 语音合成换用 seed-tts-2.0（`/api/v3/tts/unidirectional`）
- 语音识别换用火山流式识别（`/api/v3/sauc/bigmodel_nostream`）
- 配置面板必填项大幅精简，现在只需填 API Key 和音色 ID 两项
- 乐句切分改为基于识别结果的语义分句

### 新增

- `volc_tts_speaker` 配置项：指令语音的合成音色，有可用默认值 `zh_female_vv_uranus_bigtts`，一般不用改

### 修复

- 歌词精准匹配在匹配位置靠近音频开头时会被误丢弃，导致明明命中却降级成粗切
- 降级路径下语音识别被重复调用，每首歌多花一倍识别开销
- 输出文件扩展名为 `.wav` 但实际是 mp3 编码
- 片段定位失败时的提示语会误称“从开头截取”，现在会如实说明唱的是其他段落

### 安全

- 凭据脱敏：防止异常堆栈把 API Key 打进日志
- 加固二进制帧解析的边界校验，增加 gzip 解压大小上限

### 升级注意（从 1.x 升级的用户）

- MiniMax 与阿里云相关配置项已删除，升级后面板会少几项，原有值不再读取，可以不管
- 请确认面板里 `volc_tts_speaker` 有值（留空会用默认音色）
- `volc_speaker`（你自己训练的声音复刻音色，决定唱歌的声音）和 `volc_tts_speaker`（官方音色，只用于合成指令语音）**不通用**，填反会报 `resource ID is mismatched with speaker related resource`

## [1.0.2] - 2026-08-16

### 新增

- 首个发布版本
