"""음성 채널 녹음 (v0.6.5, 11/16 사용자 요청: 페스티벌 전에).

사용자(소유자)가 서버의 음성 채널에 들어가면 봇이 따라 들어가 소유자의 목소리를 원본 그대로 저장하고, 나가면 멈춘다.
받아쓰기는 나중에 정한다(11/16 사용자). 혼자 쓰므로 동의 절차는 두지 않는다(같은 날 사용자).

discord.py(2.7)는 음성을 보내기만 하고 받지 않는다. 받는 확장(discord-ext-voice-recv)은 2025-06 뒤로 멈춰 Discord 음성
종단 간 암호화(DAVE)를 풀지 못한다. 그래서 여기서 직접 받는다.

1. discord.py의 음성 UDP 소켓에 듣기 함수를 붙인다(`VoiceConnectionState.add_socket_listener`).
2. 전송 암호화(aead_xchacha20_poly1305_rtpsize)를 푼다. discord.py가 보낼 때 쓰는 방식의 반대다.
3. DAVE가 켜져 있으면 davey로 소유자 몫을 푼다. 음성 웹소켓의 말하기 신호(op 5)로 SSRC와 사용자를 잇는다.
   discord.py 2.7은 DAVE 키 전환(prepare_transition·execute_transition)을 부르지 않는다. 보내기만 하는 봇은 그래도
   되지만, 남의 목소리를 풀 열쇠는 전환을 준비할 때 생긴다(davey docs/USAGE.md). 11/16 첫 시험에서 패킷 112개를
   풀지 못한 원인이다. 그래서 음성 웹소켓을 이어받아 커밋·웰컴(op 29·30)과 전환 준비·실행(op 21·22) 뒤에 부른다.
4. Opus 패킷을 다시 압축하지 않고 Ogg Opus 파일에 그대로 넣는다. 말이 없는 동안 Discord는 패킷을 보내지 않으므로
   빈 시간은 무음 프레임으로 채워 실제 시간과 길이를 맞춘다.

파일은 서버 상태 폴더(`voice/`)에 두고 git에는 올리지 않는다(1시간 약 30MB). 매일 백업 묶음에서도 빼고, AWS가 켜져
있으면 녹음이 끝날 때 한 번 S3 `backup/voice/`에 올린다.
"""
from __future__ import annotations

import re
import struct
import threading
from datetime import datetime
from pathlib import Path

import discord
import nacl.exceptions
import nacl.secret
from discord.gateway import DiscordVoiceWebSocket
from discord.utils import MISSING
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

from .config import STATE_DIR

VOICE_DIR = STATE_DIR / "voice"
RATE = 48000
SAMPLES = 960  # 20ms
SILENCE = b"\xf8\xff\xfe"  # Discord가 쓰는 Opus 무음 프레임
MAX_GAP_FRAMES = 50 * 60 * 30  # 30분 넘게 비어도 무음은 30분까지만 채운다
OPUS_PAYLOAD = 0x78
SPEAKING = 5
UPLOAD_LIMIT = 10 * 1024 * 1024  # Discord에 바로 올리는 크기 한도


def _crc_table() -> list[int]:
    table = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = ((value << 1) ^ 0x04C11DB7) if value & 0x80000000 else value << 1
        table.append(value & 0xFFFFFFFF)
    return table


CRC_TABLE = _crc_table()


def ogg_crc(data: bytes) -> int:
    """Ogg 페이지 검사합(다항식 0x04C11DB7, 비트를 뒤집지 않음). zlib.crc32와 다르다."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


class OggOpusWriter:
    """Opus 패킷을 다시 압축하지 않고 Ogg Opus 파일로 쓴다. 대부분의 재생기와 받아쓰기 도구가 바로 읽는다."""

    PACKETS_PER_PAGE = 50  # 1초

    def __init__(self, path: Path, channels: int = 2, serial: int = 0x414A4F55) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = path.open("wb")
        self.serial, self.sequence, self.granule = serial, 0, 0
        self.pending: list[bytes] = []
        self.closed = False
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, channels, 0, RATE, 0, 0)
        vendor = b"ajou-assistant"
        tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
        self._page([head], 0, 0x02)
        self._page([tags], 0, 0x00)

    def _page(self, packets: list[bytes], granule: int, flags: int) -> None:
        lacing = bytearray()
        for packet in packets:
            lacing += bytes([255] * (len(packet) // 255) + [len(packet) % 255])
        header = struct.pack("<4sBBqIII", b"OggS", 0, flags, granule, self.serial, self.sequence, 0)
        page = bytearray(header + bytes([len(lacing)]) + lacing + b"".join(packets))
        struct.pack_into("<I", page, 22, ogg_crc(bytes(page)))
        self.file.write(page)
        self.sequence += 1

    def write(self, packet: bytes, samples: int = SAMPLES) -> None:
        if self.closed:
            return
        self.granule += samples
        self.pending.append(packet)
        lacing = sum(len(item) // 255 + 1 for item in self.pending)
        if len(self.pending) >= self.PACKETS_PER_PAGE or lacing > 230:
            self._page(self.pending, self.granule, 0)
            self.pending = []

    def close(self) -> None:
        if self.closed:
            return
        self._page(self.pending, self.granule, 0x04)  # 마지막 페이지(EOS). 패킷이 없어도 된다.
        self.pending = []
        self.file.close()
        self.closed = True

    @property
    def seconds(self) -> float:
        return self.granule / RATE


def rtp_payload(packet: bytes, key: bytes) -> tuple[int, int, bytes] | None:
    """받은 음성 패킷에서 (SSRC, RTP 시각, Opus)를 꺼낸다. 음성이 아니거나 풀리지 않으면 None.

    aead_xchacha20_poly1305_rtpsize: RTP 헤더(+CSRC, 확장 머리 4바이트)는 그대로 두고 인증 데이터로 쓰며, 나머지는
    암호문이고 맨 끝 4바이트가 nonce 앞부분이다. 풀린 글의 앞에는 확장 본문이 붙어 있다.
    """
    if len(packet) < 12 + 16 + 4 or 200 <= packet[1] <= 204 or packet[1] & 0x7F != OPUS_PAYLOAD:
        return None
    count, extended = packet[0] & 0x0F, packet[0] & 0x10
    _, timestamp, ssrc = struct.unpack_from(">HII", packet, 2)
    header = 12 + 4 * count
    words = 0
    if extended:
        if len(packet) < header + 4:
            return None
        words = struct.unpack_from(">H", packet, header + 2)[0]
        header += 4
    try:
        plain = nacl.secret.Aead(key).decrypt(bytes(packet[header:-4]), bytes(packet[:header]), bytes(packet[-4:]) + bytes(20))
    except (nacl.exceptions.CryptoError, ValueError):
        return None
    return ssrc, timestamp, plain[words * 4:]


class Recorder:
    """소유자 한 사람의 목소리를 받는다. 소켓 읽기 스레드에서 불린다."""

    def __init__(self, path: Path, owner_id: int, state: VoiceConnectionState) -> None:
        self.writer = OggOpusWriter(path)
        self.owner_id, self.state = owner_id, state
        self.users: dict[int, int] = {}  # SSRC → 사용자
        self.lock = threading.Lock()
        self.last: int | None = None
        self.packets = self.failures = self.waiting = 0
        self.first_error = ""
        self.started = datetime.now()

    def on_ws(self, message: dict) -> None:
        if message.get("op") == SPEAKING:
            data = message.get("d") or {}
            if data.get("ssrc") is not None and data.get("user_id") is not None:
                self.users[int(data["ssrc"])] = int(data["user_id"])

    def on_packet(self, data: bytes) -> None:
        key = self.state.secret_key
        if not isinstance(key, (list, bytes, bytearray)):
            return
        parsed = rtp_payload(data, bytes(key))
        if parsed is None:
            return
        ssrc, timestamp, opus = parsed
        user = self.users.get(ssrc, self.owner_id)  # 혼자 쓰는 채널. 말하기 신호가 늦게 와도 소유자로 본다.
        if user != self.owner_id:
            return
        dave = self.state.dave_session
        if dave is not None and opus != SILENCE:
            import davey
            encrypted = self.state.dave_protocol_version > 0
            if dave.ready and (encrypted or dave.can_passthrough(user)):
                try:
                    opus = dave.decrypt(user, davey.MediaType.audio, opus)
                except Exception as error:
                    self.failures += 1
                    self.first_error = self.first_error or f"{type(error).__name__}: {str(error)[:160]}"
                    return
            elif encrypted:  # 아직 열쇠가 없다(전환 전). 암호문을 파일에 넣지 않는다.
                self.waiting += 1
                return
        self.append(timestamp, opus)

    def append(self, timestamp: int, opus: bytes) -> None:
        with self.lock:
            if self.writer.closed:
                return
            if self.last is not None:
                delta = (timestamp - self.last) & 0xFFFFFFFF
                if delta == 0 or delta > 0x7FFFFFFF:  # 같은 패킷이거나 늦게 온 패킷
                    return
                for _ in range(min(delta // SAMPLES - 1, MAX_GAP_FRAMES)):
                    self.writer.write(SILENCE)
            self.last = timestamp
            self.writer.write(opus)
            self.packets += 1

    def close(self) -> None:
        with self.lock:
            self.writer.close()


class TransitionVoiceWebSocket(DiscordVoiceWebSocket):
    """DAVE 키 전환을 davey에 알리는 음성 웹소켓. discord.py가 하는 일을 먼저 하고 그 뒤에 덧붙인다."""

    async def received_message(self, msg) -> None:
        await super().received_message(msg)
        session = self._connection.dave_session
        op, data = msg.get("op"), msg.get("d") or {}
        if session is None or op not in (self.DAVE_PREPARE_TRANSITION, self.DAVE_EXECUTE_TRANSITION):
            return
        try:
            if op == self.DAVE_PREPARE_TRANSITION:
                session.prepare_transition(int(data["transition_id"]), int(data["protocol_version"]))
            elif int(data["transition_id"]) != 0:  # 0번은 prepare_transition이 바로 실행한다
                session.execute_transition(int(data["transition_id"]))
        except Exception as error:
            print(f"DAVE 전환 실패 op {op}: {type(error).__name__}: {str(error)[:150]}", flush=True)

    async def received_binary_message(self, msg: bytes) -> None:
        await super().received_binary_message(msg)
        session = self._connection.dave_session
        if session is None or len(msg) < 5 or msg[2] not in (self.MLS_ANNOUNCE_COMMIT_TRANSITION, self.MLS_WELCOME):
            return
        transition = struct.unpack_from(">H", msg, 3)[0]
        try:
            session.prepare_transition(transition, session.protocol_version)
        except Exception as error:  # 커밋이 무시됐거나 세션을 다시 만든 경우
            print(f"DAVE 전환 준비 실패 {transition}: {type(error).__name__}: {str(error)[:150]}", flush=True)


class RecordingConnectionState(VoiceConnectionState):
    """음성 웹소켓만 TransitionVoiceWebSocket으로 바꾼다. 나머지는 discord.py 그대로다(2.7.x의 _connect_websocket)."""

    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        seq_ack = self.ws.seq_ack if self.ws is not MISSING else -1
        ws = await TransitionVoiceWebSocket.from_connection_state(self, resume=resume, hook=self.hook, seq_ack=seq_ack)
        self.state = ConnectionFlowState.websocket_connected
        return ws


class RecordingVoiceClient(discord.VoiceClient):
    """음성 웹소켓 신호를 녹음기에 넘기는 VoiceClient. 말하기 신호(op 5)로 SSRC와 사용자를 잇는다."""

    recorder: Recorder | None = None

    def create_connection_state(self) -> VoiceConnectionState:
        return RecordingConnectionState(self, hook=self._forward)

    async def _forward(self, ws, message: dict) -> None:
        if self.recorder is not None:
            self.recorder.on_ws(message)


def new_path(channel_name: str, moment: datetime) -> Path:
    slug = re.sub(r"[^\w가-힣]+", "_", channel_name).strip("_")[:30] or "음성"
    return VOICE_DIR / f"{moment:%Y-%m-%d_%H%M%S}_{slug}.ogg"


def describe(recorder: Recorder) -> str:
    seconds = int(recorder.writer.seconds)
    size = recorder.writer.path.stat().st_size if recorder.writer.path.exists() else 0
    length = f"{seconds // 3600}시간 {seconds % 3600 // 60}분" if seconds >= 3600 else f"{seconds // 60}분 {seconds % 60}초"
    extra = (f" · 풀지 못한 패킷 {recorder.failures}개" if recorder.failures else "") + (
        f" · 열쇠를 기다린 패킷 {recorder.waiting}개" if recorder.waiting else "")
    return f"{length} · {size / 1024 / 1024:.1f}MB · 받은 패킷 {recorder.packets}개" + extra


def overlapping(events: list[dict], start: datetime, end: datetime) -> list[dict]:
    """녹음하는 동안 걸쳐 있던 일정(종일 일정 빼고). 녹음이 무엇에 관한 것인지 시간으로 짐작한다."""
    found = []
    for row in events:
        if row.get("allDay"):
            continue
        try:
            first = datetime.fromisoformat(str(row.get("start")).replace("Z", "+00:00"))
            last = datetime.fromisoformat(str(row.get("end")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if first <= end and last >= start:
            found.append(row)
    return found


def upload(path: Path) -> str | None:
    """AWS 백업이 켜져 있으면 S3 backup/voice/에 한 번 올린다. 실패해도 녹음 파일은 서버에 남는다."""
    from . import cloud
    from .config import setting
    bucket = setting("AJOU_AWS_BACKUP_BUCKET")
    if not cloud.enabled() or not bucket:
        return None
    try:
        key = f"backup/voice/{path.name}"
        cloud.client("s3").upload_file(str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "audio/ogg"})
        return key
    except Exception as error:
        print(f"녹음 S3 업로드 실패 {path.name}: {type(error).__name__}: {str(error)[:150]}", flush=True)
        return None
