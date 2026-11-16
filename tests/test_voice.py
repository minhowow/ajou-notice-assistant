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
