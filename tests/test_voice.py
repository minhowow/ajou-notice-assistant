"""v0.6.5 음성 녹음: 패킷 풀기, 빈 시간 채우기, Ogg Opus 파일."""
import struct
from types import SimpleNamespace

import nacl.secret
from discord.oggparse import OggStream

from ajou_assistant import voice

KEY = bytes(range(32))
OWNER = 1


def packet(sequence, timestamp, opus, ssrc=77, nonce=1, extension=b""):
    """discord.py가 보낼 때와 같은 방식(aead_xchacha20_poly1305_rtpsize)으로 만든 받은 패킷."""
    first = 0x80 | (0x10 if extension else 0)
    header = struct.pack(">BBHII", first, 0x78, sequence, timestamp, ssrc)
    if extension:
        header += struct.pack(">HH", 0xBEDE, len(extension) // 4)
    box = nacl.secret.Aead(KEY)
    nonce_bytes = struct.pack(">I", nonce) + bytes(20)
    return header + box.encrypt(extension + opus, header, nonce_bytes).ciphertext + nonce_bytes[:4]


def test_rtp_payload_undoes_transport_encryption_and_skips_the_extension():
    found = voice.rtp_payload(packet(5, 4800, b"OPUS", extension=b"\x10\x22\x00\x00"), KEY)
    assert found == (77, 4800, b"OPUS")
    assert voice.rtp_payload(packet(5, 4800, b"OPUS"), bytes(32)) is None  # 다른 열쇠
    rtcp = bytearray(packet(5, 4800, b"OPUS"))
    rtcp[1] = 201
    assert voice.rtp_payload(bytes(rtcp), KEY) is None


def recorder(tmp_path, dave=None):
    state = SimpleNamespace(secret_key=list(KEY), dave_session=dave, dave_protocol_version=1 if dave else 0)
    return voice.Recorder(tmp_path / "a.ogg", OWNER, state)


def test_gaps_become_silence_and_late_or_other_voices_are_dropped(tmp_path):
    rec = recorder(tmp_path)
    rec.on_ws({"op": 5, "d": {"ssrc": 99, "user_id": "2"}})  # 다른 사람
    rec.on_packet(packet(1, 0, b"A"))
    rec.on_packet(packet(2, 960, b"B"))
    rec.on_packet(packet(3, 960 * 4, b"C"))   # 2프레임(40ms) 비었다
    rec.on_packet(packet(2, 960, b"B"))       # 같은 패킷
    rec.on_packet(packet(9, 960 * 10, b"X", ssrc=99))  # 다른 사람 목소리
    rec.close()
    assert rec.packets == 3 and rec.writer.seconds == 5 * 960 / 48000
    stream = OggStream(open(tmp_path / "a.ogg", "rb"))
    packets = list(stream.iter_packets())
    assert packets[0].startswith(b"OpusHead") and packets[1].startswith(b"OpusTags")
    assert packets[2:] == [b"A", b"B", voice.SILENCE, voice.SILENCE, b"C"]


def test_dave_layer_is_decrypted_for_the_owner(tmp_path):
    calls = []

    class Dave:
        ready = True

        def decrypt(self, user, media, data):
            calls.append((user, data))
            if data == b"bad":
                raise ValueError("no key")
            return b"plain-" + data

        def can_passthrough(self, user):
            return False
    rec = recorder(tmp_path, Dave())
    rec.on_packet(packet(1, 0, b"enc"))
    rec.on_packet(packet(2, 960, b"bad"))
    rec.on_packet(packet(3, 1920, voice.SILENCE))  # 무음 프레임은 암호화되지 않는다
    rec.close()
    assert calls == [(OWNER, b"enc"), (OWNER, b"bad")] and rec.failures == 1 and rec.packets == 2
    packets = list(OggStream(open(tmp_path / "a.ogg", "rb")).iter_packets())[2:]
    assert packets == [b"plain-enc", voice.SILENCE, voice.SILENCE]


def test_ogg_pages_have_valid_checksums_and_large_packets_span_segments(tmp_path):
    writer = voice.OggOpusWriter(tmp_path / "b.ogg")
    big = bytes(range(256)) * 3  # 768바이트: 255 255 255 3
    for _ in range(120):
        writer.write(big)
    writer.close()
    data = (tmp_path / "b.ogg").read_bytes()
    offset, pages = 0, 0
    while offset < len(data):
        assert data[offset:offset + 4] == b"OggS"
        segments = data[offset + 26]
        size = 27 + segments + sum(data[offset + 27:offset + 27 + segments])
        page = bytearray(data[offset:offset + size])
        stored = struct.unpack_from("<I", page, 22)[0]
        struct.pack_into("<I", page, 22, 0)
        assert voice.ogg_crc(bytes(page)) == stored
        offset, pages = offset + size, pages + 1
    assert pages >= 5 and data.rfind(b"OggS") >= 0
    assert list(OggStream(open(tmp_path / "b.ogg", "rb")).iter_packets())[2:] == [big] * 120


def test_packets_wait_for_keys_instead_of_saving_ciphertext(tmp_path):
    class Dave:
        ready = False

        def can_passthrough(self, user):
            return False
    rec = recorder(tmp_path, Dave())
    rec.on_packet(packet(1, 0, b"enc"))
    rec.close()
    assert rec.waiting == 1 and rec.packets == 0 and "열쇠를 기다린 패킷 1개" in voice.describe(rec)


def test_websocket_prepares_and_executes_dave_transitions():
    """discord.py 2.7은 키 전환을 davey에 알리지 않는다(11/16 패킷 112개를 풀지 못한 원인). 웹소켓이 대신 알린다."""
    import asyncio
    calls = []

    class Session:
        protocol_version = 1

        def process_welcome(self, data):
            calls.append(("welcome", data))

        def process_commit(self, data):
            calls.append(("commit", data))
            return True

        def prepare_transition(self, transition, version):
            calls.append(("prepare", transition, version))

        def execute_transition(self, transition):
            calls.append(("execute", transition))

    async def executed(transition):
        calls.append(("discord-execute", transition))
    state = SimpleNamespace(dave_session=Session(), dave_protocol_version=1, dave_pending_transitions={},
                            _execute_transition=executed)
    ws = voice.TransitionVoiceWebSocket.__new__(voice.TransitionVoiceWebSocket)
    ws._connection, ws.seq_ack = state, 0

    async def ready(transition):
        calls.append(("ready", transition))

    async def hook(*args):
        return None
    ws.send_transition_ready, ws._hook = ready, hook
    welcome = struct.pack(">HBH", 1, ws.MLS_WELCOME, 7) + b"W"
    asyncio.run(ws.received_binary_message(welcome))
    assert calls == [("welcome", b"W"), ("ready", 7), ("prepare", 7, 1)]
    calls.clear()
    asyncio.run(ws.received_message({"op": ws.DAVE_EXECUTE_TRANSITION, "d": {"transition_id": 7}}))
    assert calls == [("discord-execute", 7), ("execute", 7)]
    calls.clear()
    asyncio.run(ws.received_message({"op": ws.DAVE_PREPARE_TRANSITION, "d": {"transition_id": 0, "protocol_version": 1}}))
    assert ("prepare", 0, 1) in calls and ("execute", 0) not in calls
