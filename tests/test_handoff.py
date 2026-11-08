"""스레드 넘겨받기 (v0.4.6): 잠금, 세션 파일 오가기, 원격 명령 따옴표, 훅 기록."""
import io
import json
import subprocess
import sys
import tarfile

import pytest

from ajou_assistant import handoff, sync


@pytest.fixture
def vm(tmp_path, monkeypatch):
    """봇 컨테이너 쪽: 상태 폴더, 스레드 대응표, 세션 파일."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(handoff, "LEASE_DIR", tmp_path / "state" / "leases")
    monkeypatch.setattr(handoff, "LOCK_DIR", tmp_path / "state" / "locks")
    monkeypatch.setattr(handoff, "SESSIONS_FILE", tmp_path / "state" / "threads.json")
    monkeypatch.setattr(handoff, "TAKE_WAIT_SECONDS", 0.2)
    pushed = []
    monkeypatch.setattr(sync, "sync_push", lambda message, *paths: pushed.append(paths))
    (tmp_path / "state").mkdir()
    handoff.SESSIONS_FILE.write_text(json.dumps({
        "10": {"title": "장학 서류", "session": "abc", "last": "2026-11-08T10:00:00+09:00"},
        "11": {"title": "세션 없음"},
        "12": {"title": "옛 대화", "session": "old", "last": "2026-11-01T10:00:00+09:00", "done": "x"}}))
    session = tmp_path / "home" / ".claude" / "projects" / "-home-assistant-Ajou" / "abc.jsonl"
    session.parent.mkdir(parents=True)
    session.write_bytes(b'{"type":"user"}\n')
    (session.with_suffix("") / "tool-results").mkdir(parents=True)
    (session.with_suffix("") / "tool-results" / "big.txt").write_bytes(b"x" * 10)
    return session, pushed


def take(key="10"):
    out = io.BytesIO()
    code = handoff.take(key, out)
    return code, (handoff.unpack(out.getvalue()) if code == 0 else {})


def test_list_shows_threads_with_a_session_newest_first(vm):
    assert [row["key"] for row in handoff.list_threads()] == ["10", "12"]
    assert handoff.list_threads()[1]["done"] is True


def test_take_hands_out_the_session_once(vm):
    session, pushed = vm
    code, files = take()
    lease = json.loads(files["lease.json"])
    assert code == 0 and files["session.jsonl"] == session.read_bytes() and files["session/tool-results/big.txt"] == b"x" * 10
    assert lease["session"] == "abc" and len(lease["token"]) == 16 and lease["path"] == str(session)
    assert handoff.lease_of("10")["token"] == lease["token"] and pushed == [("outputs/threads/10",)]
    assert take()[0] == handoff.EXIT_HELD  # 이미 쥐고 있으면 또 내주지 않는다.
    assert take("11")[0] == handoff.EXIT_NO_SESSION


def test_take_waits_for_a_running_discord_turn(vm):
    with handoff.held("10"):  # 봇이 턴을 도는 중
        assert take()[0] == handoff.EXIT_BUSY
    assert handoff.lease_of("10") is None


def test_give_writes_back_and_releases(vm):
    session, _ = vm
    _, files = take()
    token = json.loads(files["lease.json"])["token"]
    meta = json.dumps({"files": ["05_deadlines.md"], "home": "/Users/me/Ajou"}).encode()
    back = handoff.pack({"session.jsonl": b"longer\n", "session/tool-results/new.txt": b"y", "meta.json": meta})
    assert handoff.give("10", "wrong", back) == handoff.EXIT_RECLAIMED and session.read_bytes() == b'{"type":"user"}\n'
    assert handoff.give("10", token, back) == 0
    assert session.read_bytes() == b"longer\n" and (session.with_suffix("") / "tool-results" / "new.txt").read_bytes() == b"y"
    assert handoff.lease_of("10") is None
    result = handoff.pop_result("10")
    assert result["files"] == ["05_deadlines.md"] and result["home"] == "/Users/me/Ajou"
    assert handoff.pop_result("10") == {}  # 한 번 읽으면 지운다.


def test_notes_need_the_current_token(vm):
    _, files = take()
    token = json.loads(files["lease.json"])["token"]
    assert handoff.note("10", "wrong", {"text": "x"}) == handoff.EXIT_RECLAIMED
    assert handoff.note("10", token, {"text": "첫 줄 " * 50, "files": ["a.md"] * 20}) == 0
    rows, offset = handoff.read_notes("10", 0)
    assert len(rows[0]["text"]) == 120 and len(rows[0]["files"]) == 10 and handoff.read_notes("10", offset)[0] == []


def test_unpack_drops_paths_outside_the_bundle():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("../escape.txt", "/abs.txt", "session.jsonl"):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    assert list(handoff.unpack(buffer.getvalue())) == ["session.jsonl"]


def test_remote_quotes_the_command_for_one_or_two_shells(monkeypatch):
    monkeypatch.setenv("AJOU_LEASE_COMMAND", "echo lease")
    monkeypatch.setenv("AJOU_REMOTE", "sh -c {cmd}")
    assert handoff.remote("take", "10") == b"lease take 10\n"
    # ssh → su -c 처럼 셸을 두 번 거치는 경로: 바깥 셸이 큰따옴표를 벗기고 안쪽 셸이 명령을 실행한다.
    monkeypatch.setenv("AJOU_REMOTE", 'sh -c "sh -c {cmd}"')
    assert handoff.remote("give", "10", "abc") == b"lease give 10 abc\n"
    with pytest.raises(ValueError):
        handoff.remote("take", "10; rm -rf /")
    monkeypatch.setenv("AJOU_REMOTE", "sh -c {cmd}")
    monkeypatch.setenv("AJOU_LEASE_COMMAND", "sh -c 'echo 이미 열려 있습니다 >&2; exit 3' --")
    with pytest.raises(handoff.RemoteError) as error:
        handoff.remote("take", "10")
    assert error.value.code == handoff.EXIT_HELD and "이미 열려" in str(error.value)


def test_hook_records_files_and_turn_ends_only(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    hook = [sys.executable, "-I", str(handoff.HOOK_FILE)]
    write = {"tool_name": "Write", "tool_input": {"file_path": "/x/Ajou/05.md", "content": "비밀 내용"}, "tool_response": {}}
    subprocess.run([*hook, "file", str(outbox)], input=json.dumps(write).encode(), check=True)
    subprocess.run([*hook, "stop", str(outbox)], input=json.dumps({"last_assistant_message": "고쳤습니다.\n자세히…"}).encode(), check=True)
    assert subprocess.run([*hook, "stop", str(outbox)], input=b"not json").returncode == 0  # 무엇이 와도 막지 않는다.
    rows = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert rows == [{"file": "/x/Ajou/05.md"}, {"stop": "고쳤습니다.\n자세히…"}] and "비밀" not in outbox.read_text()


def test_shipper_sends_one_line_per_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff, "HOME_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(handoff, "remote", lambda *args, data=None, timeout=0: sent.append((args, json.loads(data))))
    outbox = tmp_path / "outbox.jsonl"
    rows = [{"file": str(tmp_path / "05.md")}, {"file": str(tmp_path / "05.md")}, {"stop": "\n마감을 고쳤습니다. 다음은…\n둘째 줄"},
            {"stop": "질문에 답함"}]
    outbox.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    shipper = handoff.Shipper("10", "tok", outbox)
    shipper.drain()
    assert sent == [(("note", "10", "tok"), {"text": "마감을 고쳤습니다. 다음은…", "files": ["05.md"]}),
                    (("note", "10", "tok"), {"text": "질문에 답함", "files": []})]
    assert shipper.stop() == ["05.md"]


def test_mac_session_uses_approval_mode_and_hides_secrets(tmp_path):
    settings = handoff.claude_settings(tmp_path / "outbox.jsonl")
    assert "Read(~/.config/ajou-assistant/**)" in settings["permissions"]["deny"]
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    assert handoff.project_dir("/Users/me/Ajou").name == "-Users-me-Ajou"
