"""
火山豆包实时语音对话 (BidirectionalStreaming / /api/v3/realtime/dialogue) 二进制协议
帧打包/解包 + 事件常量 + 鉴权头候选组

参考: byte0 = 0x11 (protocol version=1 高4bit | header_size=1 低4bit, header 固定4字节)
     byte1 = (message_type << 4) | flags
     byte2 = serialization<<4 | compression  (0x10 = JSON 无压缩, 0x00 = Raw 无压缩)
     byte3 = 0x00 (reserved)
"""
import gzip
import json
import struct
import uuid

# ---------------------------------------------------------------------------
# message_type
# ---------------------------------------------------------------------------
MSG_TYPE_FULL_CLIENT_REQUEST = 0x1
MSG_TYPE_AUDIO_ONLY_REQUEST = 0x2
MSG_TYPE_FULL_SERVER_RESPONSE = 0x9
MSG_TYPE_AUDIO_ONLY_RESPONSE = 0xB
MSG_TYPE_ERROR = 0xF

# flags
FLAG_WITH_EVENT = 0x4

# serialization (byte2 高4位)
SERIALIZATION_RAW = 0x0
SERIALIZATION_JSON = 0x1

# ---------------------------------------------------------------------------
# 事件号 - 客户端
# ---------------------------------------------------------------------------
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200  # 音频
EVENT_SAY_HELLO = 300
EVENT_CHAT_TTS_TEXT = 500
EVENT_CHAT_TEXT_QUERY = 501
EVENT_CLIENT_INTERRUPT = 515

# ---------------------------------------------------------------------------
# 事件号 - 服务端
# ---------------------------------------------------------------------------
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352  # 音频二进制
EVENT_TTS_ENDED = 359
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_ENDED = 559
EVENT_USAGE_RESPONSE = 154
EVENT_DIALOG_COMMON_ERROR = 599

SERVER_EVENT_NAMES = {
    50: "ConnectionStarted",
    51: "ConnectionFailed",
    150: "SessionStarted",
    152: "SessionFinished",
    153: "SessionFailed",
    154: "UsageResponse",
    450: "ASRInfo",
    451: "ASRResponse",
    459: "ASREnded",
    350: "TTSSentenceStart",
    351: "TTSSentenceEnd",
    352: "TTSResponse",
    359: "TTSEnded",
    550: "ChatResponse",
    559: "ChatEnded",
    599: "DialogCommonError",
}


def new_uuid() -> str:
    return str(uuid.uuid4())


def pack(event, payload, session_id=None, message_type=MSG_TYPE_FULL_CLIENT_REQUEST,
         serialization=SERIALIZATION_JSON << 4):
    """
    打包一帧发给服务端。

    event: int, 事件号
    payload: bytes, 已经序列化好的 payload (通常是 json.dumps(...).encode())
    session_id: str|None, StartConnection/FinishConnection 不带, 其余事件必须带
    message_type: 0x1 = full-client request (JSON), 0x2 = audio-only request (Raw)
    serialization: byte2 的值 (调用方直接传 0x10 或 0x00, 不要再左移)
    """
    header = bytes([0x11, (message_type << 4) | FLAG_WITH_EVENT, serialization, 0x00])
    body = struct.pack(">I", event)
    if session_id is not None:
        sid = session_id.encode("utf-8")
        body += struct.pack(">I", len(sid)) + sid
    body += struct.pack(">I", len(payload)) + payload
    return header + body


def pack_json(event, payload_dict, session_id=None, message_type=MSG_TYPE_FULL_CLIENT_REQUEST):
    """便捷封装: payload 是 dict, 序列化成 JSON, serialization 标 0x10"""
    payload = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    return pack(event, payload, session_id=session_id, message_type=message_type,
                serialization=0x10)


def pack_audio(event, pcm_bytes, session_id):
    """音频帧: message_type=0x2 (audio-only request), serialization=0x00 (Raw)"""
    return pack(event, pcm_bytes, session_id=session_id, message_type=MSG_TYPE_AUDIO_ONLY_REQUEST,
                serialization=0x00)


class ParsedFrame:
    def __init__(self):
        self.message_type = None
        self.flags = None
        self.serialization = None
        self.compression = None
        self.event = None
        self.session_id = None
        self.connect_id = None
        self.error_code = None
        self.payload = b""
        self.json = None  # 解析出的 JSON dict (若 serialization 是 JSON)

    def __repr__(self):
        ev_name = SERVER_EVENT_NAMES.get(self.event, "?")
        return (f"ParsedFrame(msg_type=0x{self.message_type:X}, event={self.event}({ev_name}), "
                f"session_id={self.session_id}, error_code={self.error_code}, "
                f"payload_len={len(self.payload)}, json={self.json})")


def unpack(frame: bytes) -> ParsedFrame:
    """
    解包服务端返回的一帧。
    frame: 完整的一帧字节 (调用方需要先按 TCP/WS 消息边界拿到完整帧;
           WebSocket 是消息边界天然对齐的,一条 ws message = 一帧,不需要自己拆包)
    """
    pf = ParsedFrame()
    max_payload = 10 * 1024 * 1024
    max_decompressed = 50 * 1024 * 1024
    if len(frame) < 4 or frame[0] >> 4 != 1 or (frame[0] & 0x0F) != 1:
        raise ValueError("invalid protocol frame header")

    byte1, byte2 = frame[1], frame[2]
    pf.message_type = byte1 >> 4
    pf.flags = byte1 & 0x0F
    pf.serialization = byte2 >> 4
    pf.compression = byte2 & 0x0F
    offset = 4

    def read_u32(label):
        nonlocal offset
        if offset + 4 > len(frame):
            raise ValueError(f"truncated {label}")
        value = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
        return value

    if pf.message_type == MSG_TYPE_ERROR:
        pf.error_code = read_u32("error code")
    if pf.flags & FLAG_WITH_EVENT:
        pf.event = read_u32("event")
        if pf.event not in (EVENT_START_CONNECTION, EVENT_FINISH_CONNECTION):
            sid_len = read_u32("session id size")
            if sid_len > max_payload or offset + sid_len > len(frame):
                raise ValueError("invalid session id size")
            pf.session_id = frame[offset:offset + sid_len].decode("utf-8", errors="replace")
            offset += sid_len

    payload_size = read_u32("payload size")
    if payload_size > max_payload or payload_size != len(frame) - offset:
        raise ValueError("invalid payload size")
    pf.payload = frame[offset:]
    if pf.compression == 0x1 and pf.payload:
        import io
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(pf.payload)) as stream:
                decoded = stream.read(max_decompressed + 1)
            if len(decoded) > max_decompressed:
                raise ValueError("decompressed payload too large")
            pf.payload = decoded
        except (OSError, EOFError) as exc:
            raise ValueError("invalid compressed payload") from exc

    if pf.serialization == SERIALIZATION_JSON and pf.payload:
        try:
            pf.json = json.loads(pf.payload.decode("utf-8"))
        except Exception:
            pf.json = None

    return pf


# ---------------------------------------------------------------------------
# 鉴权头候选组 (阶段0 逐个尝试)
# ---------------------------------------------------------------------------
def build_auth_header_candidates(cfg: dict):
    """
    返回一个 list, 每项是 (候选名, headers_dict)。
    cfg 需要包含: api_key, app_key, resource_id
    """
    api_key = cfg["api_key"]
    app_key = cfg.get("app_key", "PlgvMymc7f3tQnJ6")
    resource_id = cfg.get("resource_id", "volc.speech.dialog")

    candidates = []

    # 候选1: X-Api-Key + X-Api-Resource-Id + X-Api-App-Key + X-Api-Connect-Id
    candidates.append((
        "candidate1_apikey_resource_appkey",
        {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-App-Key": app_key,
            "X-Api-Connect-Id": new_uuid(),
        },
    ))

    # 候选2: 只 X-Api-Key + X-Api-Resource-Id + X-Api-Connect-Id
    candidates.append((
        "candidate2_apikey_resource_only",
        {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Connect-Id": new_uuid(),
        },
    ))

    # 候选3: X-Api-Access-Key + X-Api-Resource-Id + X-Api-App-Key + X-Api-Connect-Id
    candidates.append((
        "candidate3_accesskey_resource_appkey",
        {
            "X-Api-Access-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-App-Key": app_key,
            "X-Api-Connect-Id": new_uuid(),
        },
    ))

    return candidates
