"""스레드 정리 (11/6 사용자 결정, v0.4.5): 완료하면 바로 보관해 사이드바에서 빼고 원래 메시지에 ✅,
다시 쓰면 열린다. 보관된 스레드의 메시지·버튼이 버려지지 않게 하고, 세션 파일이 없으면 새로 시작한다."""
import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

from ajou_assistant import bot, cli, guard, progress, store

OWNER, GUILD, CHANNEL = 1, 2, 10


class FakeMessage:
    def __init__(self, text=None, view=None):
        self.content, self.view, self.edits = text, view, []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.content = kwargs.get("content", self.content)
        self.view = kwargs.get("view", self.view)


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeThread(discord.Thread):
    """isinstance(…, discord.Thread)를 통과하는 가짜 스레드. 보낸 것과 보관을 events에 순서대로 남긴다."""

    def __init__(self, thread_id, events, archived=False):
        self.id, self.parent_id, self.archived, self.name = thread_id, CHANNEL, archived, "스레드"
        self.events, self.sent = events, []

    async def send(self, content=None, **kwargs):
        self.events.append(("send", content))
        message = FakeMessage(content, kwargs.get("view"))
        self.sent.append(message)
        return message

    async def edit(self, **kwargs):
        self.events.append(("archive",) if kwargs.get("archived") else ("edit", kwargs))
        self.archived = kwargs.get("archived", self.archived)

    def typing(self):
        return Typing()


class Parent:
    def __init__(self, events):
        self.events = events

    def get_partial_message(self, message_id):
        events = self.events

        class Starter:
            async def add_reaction(self, emoji):
                events.append(("react", message_id, emoji))

            async def remove_reaction(self, emoji, member):
                events.append(("unreact", message_id, emoji))
        return Starter()


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(bot, "SESSIONS_FILE", tmp_path / "state" / "threads.json")
    monkeypatch.setattr(bot, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bot, "THREAD_DIR", tmp_path / "outputs" / "threads")
    monkeypatch.setattr(bot, "sync_push", lambda *args: None)
    monkeypatch.setattr(store, "ACTIONS_FILE", tmp_path / "actions.jsonl")
    monkeypatch.setattr(store, "REPOSTS_FILE", tmp_path / "reposts.jsonl")
    assistant = bot.Assistant.__new__(bot.Assistant)
    assistant._connection = SimpleNamespace(user=SimpleNamespace(id=999))
    assistant.owner_id, assistant.guild_id, assistant.managed = OWNER, GUILD, {CHANNEL}
    assistant.chat_channel = assistant.study_channel = assistant.english_channel = 0
    assistant.sessions, assistant.locks, assistant.pending, assistant.turns = {}, {}, {}, {}
    assistant.recent_thread, assistant.last_status = {}, {}
    assistant.gate = asyncio.Semaphore(2)
    assistant.events, assistant.threads = [], {}
    assistant.flushed = []

    def get_channel(channel_id):
        return Parent(assistant.events) if channel_id == CHANNEL else assistant.threads.get(channel_id)

    async def fetch_channel(channel_id):
        if channel_id in assistant.threads:
            return assistant.threads[channel_id]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")

    async def flush_thread(key, thread=None):
        assistant.events.append(("flush", key))
        assistant.flushed.append(key)
        return True
    assistant.get_channel, assistant.fetch_channel, assistant.flush_thread = get_channel, fetch_channel, flush_thread
    return assistant


def add_thread(helper, thread_id, archived=False, **state):
    thread = FakeThread(thread_id, helper.events, archived)
    helper.threads[thread_id] = thread
    helper.sessions[str(thread_id)] = {"title": "스레드", **state}
    return thread


def test_complete_records_then_says_one_line_then_marks_and_archives(helper):
    thread = add_thread(helper, 500)
    stopped = []
    status, closing = FakeMessage("✅ 끝", view="버튼"), SimpleNamespace(stop=lambda: stopped.append(True))
    helper.last_status[500] = (status, closing)
    helper.recent_thread[CHANNEL] = (0.0, 500)
    asyncio.run(helper.complete_thread(thread))
    kinds = [event[0] for event in helper.events]
    assert kinds == ["flush", "send", "react", "archive"]  # 보관 뒤에 쓰면 다시 열리므로 보관이 마지막이다.
    assert "다시 쓰면 열리고" in helper.events[1][1] and helper.events[2] == ("react", 500, "✅")
    assert helper.sessions["500"]["done"] and json.loads(bot.SESSIONS_FILE.read_text())["500"]["done"]
    assert status.edits == [{"view": None}] and 500 not in helper.last_status and stopped == [True]
    assert CHANNEL not in helper.recent_thread  # 90초 안에 이어 쓴 글이 닫은 스레드를 되살리지 않는다.


def test_already_archived_thread_closes_without_a_new_post(helper):
    thread = add_thread(helper, 501, archived=True)
    asyncio.run(helper.complete_thread(thread))
    assert [event[0] for event in helper.events] == ["flush", "react", "archive"]


def test_writing_again_reopens_and_unmarks(helper):
    thread = add_thread(helper, 502, done="2026-11-08T10:00:00")
    asyncio.run(helper.reopen(thread))
    assert "done" not in helper.sessions["502"] and helper.events == [("unreact", 502, "✅")]
    asyncio.run(helper.reopen(thread))  # 열린 스레드는 아무것도 하지 않는다.
    assert len(helper.events) == 1


def message(channel, text):
    return SimpleNamespace(author=SimpleNamespace(id=OWNER, bot=False), guild=SimpleNamespace(id=GUILD),
                           channel=channel, content=text, attachments=[], message_snapshots=[], id=7777)


def test_message_in_archived_thread_is_not_dropped(helper):
    """discord.py는 보관된 스레드를 캐시에서 빼서 메시지가 PartialMessageable로 온다. 예전에는 조용히 버렸다."""
    add_thread(helper, 503, archived=True)
    seen = []

    async def converse(thread, item):
        seen.append(thread.id)
    helper.converse = converse
    helper.sessions["503"]["done"] = "x"
    asyncio.run(helper.on_message(message(discord.PartialMessageable(state=None, id=503), "다시 볼게")))
    assert seen == [503] and "done" not in helper.sessions["503"]


def test_done_word_closes_the_thread(helper):
    add_thread(helper, 504)
    asyncio.run(helper.on_message(message(discord.PartialMessageable(state=None, id=504), " 완료 ")))
    assert helper.sessions["504"]["done"] and helper.threads[504].archived


class Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, **kwargs):
        self.sent.append(text)

    async def defer(self, **kwargs):
        self.sent.append("defer")


def interaction(custom_id, alert=None):
    return SimpleNamespace(data={"custom_id": custom_id}, user=SimpleNamespace(id=OWNER), guild_id=GUILD,
                           response=Response(), message=alert, followup=SimpleNamespace())


def test_alert_done_closes_the_notice_threads_but_not_others(helper):
    add_thread(helper, 600, notice=7)
    add_thread(helper, 601, notice=9)
    add_thread(helper, 602, notice=7, done="x", archived=True)

    async def set_pin(*args, **kwargs):
        return ""
    helper.set_pin = set_pin
    press = interaction("ajou:done:7", alert=SimpleNamespace(id=600))
    asyncio.run(helper.on_interaction(press))
    assert "스레드도 닫습니다" in press.response.sent[0] and store.item_actions()[7] == "done"
    assert helper.threads[600].archived and not helper.threads[601].archived
    assert ("react", 602, "✅") not in helper.events  # 이미 닫은 스레드는 건드리지 않는다.


def test_close_button_on_the_last_status_line(helper):
    add_thread(helper, 603)
    press = interaction("ajou:close:603")
    asyncio.run(helper.on_interaction(press))
    assert press.response.sent == ["defer"] and helper.threads[603].archived


def test_notice_button_finds_an_archived_thread_instead_of_making_another(helper):
    add_thread(helper, 604, notice=7, archived=True)
    sent = []

    async def followup_send(text, **kwargs):
        sent.append(text)

    async def refuse(*args, **kwargs):
        pytest.fail("이미 있는 스레드를 다시 만듦")
    helper.open_thread = refuse
    press = interaction("ajou:thread:7", alert=SimpleNamespace(id=604, thread=None))
    press.followup = SimpleNamespace(send=followup_send)
    asyncio.run(helper.on_interaction(press))
    assert sent == ["이미 열린 스레드가 있습니다: <#604>"]


def test_finished_threads_get_no_bot_posts(helper, tmp_path, monkeypatch):
    add_thread(helper, 605, done="x", last="2026-11-01T10:00:00+09:00")
    assert not helper.needs_record("605", helper.sessions["605"], store.now())
    helper.recent_thread[CHANNEL] = (100.0, 605)
    assert asyncio.run(helper.joined_thread(CHANNEL, 110.0)) is None
    # 공지 실행(scheduler)도 닫은 스레드에는 재공지 알림을 올리지 않는다.
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"605": {"notice": 7, "done": "x"}, "606": {"notice": 7}}))
    monkeypatch.setattr(cli, "SESSIONS_FILE", path)
    assert cli.notice_threads({7}) == ["606"]


def test_reflection_threads_hide_after_a_day(helper):
    made = []

    class Starter:
        async def create_thread(self, name, auto_archive_duration):
            made.append(auto_archive_duration)
            thread = FakeThread(700 + len(made), helper.events)

            async def add_user(user):
                return None
            thread.add_user = add_user
            return thread
    asyncio.run(helper.open_thread(Starter(), "✍️ 강연", None, mode="reflection"))
    asyncio.run(helper.open_thread(Starter(), "질문", None))
    assert made == [1440, 10080]


def test_missing_session_starts_fresh_and_points_at_the_log(helper, tmp_path, monkeypatch):
    thread = add_thread(helper, 800, session="gone", cost=3.0)
    calls = []

    async def run_turn(prompt, workdir, session, mode="chat", turn=None):
        calls.append((prompt, session))
        return {"result": "답", "session_id": f"new{len(calls)}", "total_cost_usd": 0.2}

    async def no_files(*args):
        return None
    monkeypatch.setattr(bot, "run_turn", run_turn)
    monkeypatch.setattr(bot, "plan_login", lambda: True)
    monkeypatch.setattr(store, "month_usage", lambda kind: 0.0)
    monkeypatch.setattr(store, "add_usage", lambda *args: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    helper.send_files = no_files
    asyncio.run(helper.converse(thread, message(thread, "이어서 하자")))
    prompt, session = calls[0]
    assert session is None and "log.md에 있으니 먼저 읽는다" in prompt and prompt.endswith("이어서 하자")
    assert helper.sessions["800"]["session"] == "new1"
    status, _ = helper.last_status[800]
    assert status.content.startswith("✅ 끝") and status.view.children[0].custom_id == "ajou:close:800"
    # 다음 턴이 시작되면 앞 상태 줄의 버튼은 뗀다. 세션 파일이 있으면 그대로 이어 간다.
    sessions = tmp_path / ".claude" / "projects" / "-x-Ajou"
    sessions.mkdir(parents=True)
    (sessions / "new1.jsonl").write_text("{}\n")
    asyncio.run(helper.converse(thread, message(thread, "하나 더")))
    assert calls[1] == ("하나 더", "new1") and status.edits[-1] == {"view": None}


def test_guard_blocks_files_that_act_as_rules():
    for path in ("outputs/threads/1/.gitattributes", "outputs/threads/1/a/.git/config", "/h/Ajou/outputs/threads/1/CLAUDE.md",
                 "outputs/threads/1/.claude/settings.json", "outputs/threads/1/.mcp.json"):
        assert guard.verdict("Write", {"file_path": path}, set()), path
    assert guard.verdict("Edit", {"file_path": "outputs/threads/1/.gitignore"}, set())
    assert guard.verdict("Write", {"file_path": "outputs/threads/1/gitnotes.md"}, set()) is None
    settings = bot.guard_settings()
    assert settings["cleanupPeriodDays"] >= 3650
    assert "Write" in settings["hooks"]["PreToolUse"][0]["matcher"]


def test_tool_search_has_a_plain_label():
    event = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "ToolSearch", "input": {}}]}}
    assert progress.steps(event, "/h") == ["🧰 도구 불러오기"]
