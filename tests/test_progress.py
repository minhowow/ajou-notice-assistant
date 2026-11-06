"""스레드 턴의 실시간 상태 표시와 멈추기."""
import asyncio
import json
import sys
import time

import pytest

from ajou_assistant import bot, progress

HOME = "/home/assistant/Ajou"
CAL = "mcp__claude_ai_Google_Calendar__"


def tool(name, **arguments):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": name, "input": arguments}]}}


def test_steps_name_files_searches_and_calendar_work_but_not_results():
    assert progress.steps(tool("Read", file_path=f"{HOME}/CURRENT_PROFILE.md"), HOME) == ["📄 읽기 CURRENT_PROFILE.md"]
    assert progress.steps(tool("Grep", pattern="AIWS", path="."), HOME) == ["🔎 찾기 'AIWS'"]
    assert progress.steps(tool("Write", file_path=f"{HOME}/outputs/threads/1/draft.md"), HOME) == [
        "✏️ 쓰기 outputs/threads/1/draft.md"]
    assert progress.steps(tool(CAL + "create_event", summary="면접"), HOME) == ["📅 일정 만들기 '면접'"]
    assert progress.steps({"type": "system", "subtype": "permission_denied", "tool_name": "Bash"}, HOME) == [
        "⛔ 권한 없음: Bash"]
    said = {"type": "assistant", "message": {"content": [{"type": "text", "text": "프로필을 먼저 보겠습니다.\n둘째 줄"}]}}
    assert progress.steps(said, HOME) == ["💬 프로필을 먼저 보겠습니다."]
    # 도구 결과(파일 내용)와 최종 결과는 상태에 옮기지 않는다.
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "비밀 내용"}]}}
    assert progress.steps(result, HOME) == [] and progress.steps({"type": "result", "result": "답"}, HOME) == []
    assert progress.steps("문자열", HOME) == [] and progress.steps({"type": "assistant", "message": "x"}, HOME) == []


class FakeMessage:
    def __init__(self, content, view):
        self.edits = [(content, view)]

    async def edit(self, content, **extra):
        self.edits.append((content, extra.get("view", "그대로")))


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content, view=None, **extra):
        assert extra.get("silent") and extra.get("suppress_embeds")
        message = FakeMessage(content, view)
        self.sent.append(message)
        return message


def test_status_message_updates_then_collapses_to_one_line():
    async def scenario():
        channel = FakeChannel()
        status = progress.Progress(channel, HOME, edit_seconds=0.01)
        await status.start()
        for number in range(progress.KEEP_LINES + 3):
            status.note(tool("Read", file_path=f"{HOME}/file{number}.md"))
        await asyncio.sleep(0.05)
        await status.finish()
        return channel.sent[0]

    message = asyncio.run(scenario())
    middle = [content for content, _ in message.edits if content.startswith("⏳")][-1]
    assert "… 앞 단계 3개" in middle and f"file{progress.KEEP_LINES + 2}.md" in middle and "file0.md" not in middle
    last, view = message.edits[-1]
    assert last.startswith("✅ 끝 · ") and last.endswith(f"단계 {progress.KEEP_LINES + 3}개") and view is None


FAKE = """
import json, sys, time
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.md"}}]}}), flush=True)
mode = sys.argv[1]
if mode == "big":
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "가" * 300000}]}}), flush=True)
    print(json.dumps({"type": "result", "is_error": False, "result": "끝", "session_id": "s1", "total_cost_usd": 0.1}))
elif mode == "slow":
    time.sleep(30)
else:
    print("로그인이 필요합니다", file=sys.stderr)
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE)
    monkeypatch.setattr(bot, "HOME_DIR", tmp_path)
    return lambda mode: [sys.executable, str(script), mode]


def test_run_claude_streams_events_and_reads_lines_over_64kb(fake_claude):
    seen = []
    result = asyncio.run(bot.run_claude("질문", fake_claude("big"), {}, bot.Turn(seen.append)))
    assert result["result"] == "끝" and result["session_id"] == "s1"
    assert [event["type"] for event in seen] == ["system", "assistant", "user"]
    assert len(seen[2]["message"]["content"][0]["content"]) == 300000


def test_run_claude_without_result_reports_stderr(fake_claude):
    with pytest.raises(RuntimeError, match="로그인이 필요합니다"):
        asyncio.run(bot.run_claude("질문", fake_claude("none"), {}))


def test_stop_button_ends_the_running_turn(fake_claude):
    async def scenario():
        button = bot.stop_view(5).children[0]
        assert button.custom_id == "ajou:stop:5" and button.label == "멈추기"
        turn = bot.Turn()
        turn.on_event = lambda event: event["type"] == "assistant" and turn.stop()
        started = time.monotonic()
        with pytest.raises(bot.Stopped):
            await bot.run_claude("질문", fake_claude("slow"), {}, turn)
        assert not turn.stop()  # 끝난 턴은 다시 멈추지 않는다.
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 10


def test_thread_command_streams_events():
    command = bot.claude_command("outputs/threads/1", None)
    assert command[command.index("--output-format") + 1] == "stream-json" and "--verbose" in command
