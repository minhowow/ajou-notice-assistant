"""v0.6.1: 📥 던지기 길잡이(router.py)와 메일 확인(mail.py)."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from ajou_assistant import bot, guard, mail, router, store
from ajou_assistant.cli import last_slot
from ajou_assistant.notify import Tier

from tests.test_lifecycle import GUILD, helper  # noqa: F401  (fixture)

KST = store.KST
NOW = datetime(2026, 11, 15, 20, 0, tzinfo=KST)


def notice(number, title, relevance="high", message_id=None, channel="scholarship", at="2026-11-14T10:00:00+09:00"):
    return {"id": number, "title": title, "channel": channel, "message_id": message_id, "analyzed_at": at,
            "analysis": {"relevance": relevance, "deadlines": [{"label": "마감", "date": "2026-11-20"}]}}


def test_candidates_come_from_code_and_skip_closed_old_and_duplicated_topics():
    sessions = {
        "100": {"title": "OO장학금", "notice": 7, "last": "2026-11-15T19:00:00+09:00"},
        "101": {"title": "닫은 대화", "last": "2026-11-15T19:00:00+09:00", "done": "x"},
        "102": {"title": "옛 대화", "last": "2026-09-01T10:00:00+09:00"},
        "103": {"title": "📅 특강", "last": "2026-11-15T18:00:00+09:00", "event": {"id": "ev1"}},
    }
    notices = [notice(7, "OO장학금", message_id="555"), notice(8, "AI 공모전", message_id="556"),
               notice(9, "낮은 공지", relevance="low", message_id="557"), notice(10, "패스한 공지", message_id="558"),
               notice(11, "알림 없음", message_id=None)]
    cards = {"t1": {"channel": "900", "message": "901", "subject": "추천서 회신", "from": "교수님", "at": "2026-11-15T09:00:00+09:00"}}
    events = [{"id": "ev1", "title": "특강", "start": "2026-11-15T16:00:00+09:00"},
              {"id": "ev2", "title": "면담", "start": "2026-11-17T10:00:00+09:00", "location": "팔달관"},
              {"id": "ev3", "title": "불참", "start": "2026-11-16T10:00:00+09:00", "status": "declined"}]
    topics = router.candidates(sessions, notices, {10}, cards, events, {"scholarship": "800"}, NOW)
    keys = {topic.key for topic in topics}
    assert keys == {"thread:100", "thread:103", "card:800:556", "card:900:901", "event:ev2"}
    by_key = {topic.key: topic for topic in topics}
    many = [{"id": f"x{n}", "title": f"약속 {n}", "start": "2026-11-16T10:00:00+09:00"} for n in range(40)]
    many += [{"id": "k", "title": "[마감] 장학", "start": "2026-11-16T10:00:00+09:00", "key": "notice:1"},
             {"id": "d", "title": "종일", "start": "2026-11-16T00:00:00+09:00", "allDay": True}]
    crowded = router.candidates(sessions, notices, {10}, cards, many, {"scholarship": "800"}, NOW)
    assert sum(1 for topic in crowded if topic.kind == "event") == router.KIND_LIMITS["event"]
    assert {"thread:100", "card:800:556", "card:900:901"} <= {topic.key for topic in crowded}
    assert not {"event:k", "event:d"} & {topic.key for topic in crowded}
    assert by_key["card:800:556"].notice == "8" and by_key["card:800:556"].kind == "notice"
    assert by_key["card:900:901"].kind == "mail" and "교수님" in by_key["card:900:901"].label
    assert by_key["event:ev2"].event["location"] == "팔달관"


class FakeClient:
    def __init__(self, answer):
        self.answer, self.calls = answer, []
        self.messages = self

    def create(self, **call):
        self.calls.append(call)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.answer))], usage=usage,
                               model="claude-haiku-5-5")


def test_route_only_accepts_listed_numbers():
    topics = [router.Topic("thread:1", "thread", "장학", "스레드 · 장학", thread="1"),
              router.Topic("event:e", "event", "면담", "일정 · 면담")]
    client = FakeClient({"choice": 2, "confidence": "high", "alternatives": [1, 7], "title": ""})
    decision = router.route(client, "면담 때 물어볼 것: 졸업 요건", topics, NOW)
    assert decision.target.key == "event:e" and decision.confident and [t.key for t in decision.alternatives] == ["thread:1"]
    assert "<던진_글>" in client.calls[0]["messages"][0]["content"] and client.calls[0]["model"] == "claude-haiku-5-5"
    # 목록에 없는 번호는 받아들이지 않는다(던진 글 속 지시문이 할 수 있는 최악은 엉뚱한 곳)
    decision = router.route(FakeClient({"choice": 99, "confidence": "high", "alternatives": [], "title": ""}), "x", topics, NOW)
    assert decision.target is None and not decision.confident
    decision = router.route(FakeClient({"choice": 0, "confidence": "high", "alternatives": [], "title": "여름 계획"}), "x", topics, NOW)
    assert decision.target is None and decision.confident and decision.title == "여름 계획"
    decision = router.route(FakeClient({"choice": 0, "confidence": "high", "alternatives": [], "title": ""}), "\n  첫 줄 제목\n둘째", topics, NOW)
    assert decision.title == "첫 줄 제목"


def test_mail_answer_is_checked_field_by_field():
    result = '앞말 [{"id": "m1", "thread": "t1", "url": "https://mail.google.com/mail/?authuser=x#all/t1", "from": "교수님 <p@ajou.ac.kr>",' \
             ' "subject": "추천서", "importance": "urgent", "kind": "교수·교직원", "summary": "확인 부탁"},' \
             ' {"id": "m2", "thread": "t2", "url": "https://evil.example/x", "subject": "광고", "importance": "low", "kind": "스팸"},' \
             ' "문자열"] 끝'
    first, second = mail.parse(result)
    assert first.sender.startswith("교수님") and first.importance == "normal" and first.url.startswith("https://mail.google.com/")
    assert second.url == "" and second.kind == "기타" and second.importance == "low"


class FakeDiscord:
    dry_run = False
    channels = {"mail": "700", "system": "701"}

    def __init__(self):
        self.sent, self.opened = [], []

    def send(self, channel, payload, files=None, tier=Tier.NOTIFY):
        self.sent.append((channel, payload, tier))
        return f"m{len(self.sent)}"

    def fetch(self, channel, message_id):
        return {}

    def open_thread(self, channel, message_id, name):
        self.opened.append((channel, message_id))
        return message_id


def fake_mails(*rows):
    return [mail.Mail.model_validate(row) for row in rows]


def test_mail_check_first_run_is_silent_then_cards_and_same_thread_follow_ups(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mail.json")
    monkeypatch.setattr(store, "add_usage", lambda *args: None)
    monkeypatch.setattr("ajou_assistant.config.SESSIONS_FILE", tmp_path / "threads.json")
    inbox = [{"id": "m1", "thread": "t1", "subject": "옛 메일", "importance": "high"}]
    monkeypatch.setattr(mail, "check", lambda seen: ([m for m in fake_mails(*inbox) if m.id not in seen], 0.01))
    discord = FakeDiscord()
    assert mail.main(discord, None) == 0 and not discord.sent  # 처음에는 이미 있던 메일로만 둔다
    inbox += [{"id": "m2", "thread": "t2", "subject": "추천서 회신", "importance": "high", "summary": "서명본 [여기](https://bad.io)",
               "url": "https://mail.google.com/mail/#all/t2"},
              {"id": "m3", "thread": "t3", "subject": "광고", "importance": "low"}]
    assert mail.main(discord, None) == 0
    [(channel, payload, tier)] = discord.sent
    assert channel == "mail" and tier == Tier.NOTIFY and payload["embeds"][0]["title"] == "추천서 회신"
    assert "](https://bad.io)" not in payload["embeds"][0]["description"]
    assert "ajou:done:mail-m2" in json.dumps(payload["components"])
    state = json.loads((tmp_path / "mail.json").read_text())
    assert state["cards"]["t2"]["message"] == "m1" and set(state["seen"]) == {"m1", "m2", "m3"} and sum(state["low"].values()) == 1
    inbox.append({"id": "m4", "thread": "t2", "subject": "Re: 추천서 회신", "importance": "normal", "summary": "받았습니다"})
    assert mail.main(discord, None) == 0  # 같은 Gmail 스레드의 다음 메일은 처음 카드의 스레드로
    assert discord.opened == [("700", "m1")] and discord.sent[-1][0] == "m1" and discord.sent[-1][2] == Tier.QUIET
    assert mail.note_id(discord.sent[-1][1]["content"]) == "m4"


def test_mail_failures_alert_once_after_three(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mail.json")

    def broken(seen):
        raise RuntimeError("Claude 오류: not logged in")
    monkeypatch.setattr(mail, "check", broken)
    discord = FakeDiscord()
    for _ in range(4):
        assert mail.main(discord, None) == 1
    assert [channel for channel, _, _ in discord.sent] == ["system"]


def test_guard_lets_only_gmail_reading_through():
    assert guard.verdict("mcp__claude_ai_Gmail__search_threads", {"query": "in:inbox"}, set()) is None
    assert guard.verdict("mcp__claude_ai_Gmail__get_thread", {"threadId": "t"}, set()) is None
    for name in ("send_message", "create_draft", "forward", "trash_thread", "label_message", "reply"):
        assert guard.verdict(f"mcp__claude_ai_Gmail__{name}", {}, set()), name
    args, prompt = mail.command(["m1"])
    allowed = args[args.index("--allowedTools") + 1].split()
    assert allowed == mail.READ_TOOLS and "Read" in args[args.index("--disallowedTools") + 1].split() and "m1" in prompt


def test_mail_slots_every_quarter_hour_from_8_to_23():
    def at(text):
        return datetime.fromisoformat(text + ":00+09:00")
    assert last_slot("mail", at("2026-11-15T10:14")) == at("2026-11-15T10:00")
    assert last_slot("mail", at("2026-11-15T10:15")) == at("2026-11-15T10:15")
    assert last_slot("mail", at("2026-11-15T23:40")) == at("2026-11-15T23:00")
    assert last_slot("mail", at("2026-11-16T03:00")) == at("2026-11-15T23:00")
    assert last_slot("mail", at("2026-11-16T08:05")) == at("2026-11-16T08:00")


def test_thrown_message_goes_to_the_chosen_thread_and_asks_when_unsure(helper, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_lifecycle import add_thread
    monkeypatch.setattr(bot, "INBOX_FILE", tmp_path / "inbox.json")
    monkeypatch.setattr(store, "add_usage", lambda *args: None)
    add_thread(helper, 300, last="2026-11-15T19:00:00+09:00")
    topic = router.Topic("thread:300", "thread", "장학", "스레드 · 장학", thread="300")
    other = router.Topic("thread:301", "thread", "공모전", "스레드 · 공모전", thread="301")
    answers = []
    monkeypatch.setattr(router, "gather", lambda sessions, now: [topic, other])
    monkeypatch.setattr(router, "route", lambda client, text, topics, now: answers.pop(0))
    helper._api, helper.inbox_channel = object(), 50
    conversed, replies = [], []

    async def converse(target, message):
        conversed.append((target.id, message.id))
    helper.converse = converse

    class Thrown:
        def __init__(self, number, text):
            self.id, self.content, self.attachments, self.message_snapshots = number, text, [], []
            self.jump_url = f"https://discord.com/channels/{GUILD}/50/{number}"
            self.created_at = datetime(2026, 11, 15, 20, number % 60, tzinfo=KST)

        async def reply(self, text, view=None, **kwargs):
            replies.append((text, [child.custom_id for child in view.children] if view else []))
            return SimpleNamespace(id=self.id + 1000)

    answers.append(router.Decision(topic, True, [other], "", {}, "claude-haiku-5-5"))
    asyncio.run(helper.inbox_message(Thrown(1, "장학금 서류 사진 보냄")))
    assert conversed == [(300, 1)] and "보냈어요" in replies[-1][0]
    assert replies[-1][1] == ["ajou:route:1:1", "ajou:route:1:new"]  # 고른 곳은 빼고 다른 후보와 새 주제
    assert any(event[0] == "send" and "던진 글" in str(event[1]) for event in helper.events if event[0] == "send")

    answers.append(router.Decision(other, False, [topic], "", {}, "claude-haiku-5-5"))
    asyncio.run(helper.inbox_message(Thrown(30, "이건 어디?")))  # 90초가 지난 뒤, 애매함
    assert conversed == [(300, 1)] and replies[-1][0] == "어느 주제에 붙일까요?"
    assert replies[-1][1] == ["ajou:route:30:0", "ajou:route:30:1", "ajou:route:30:new"]
    saved = json.loads((tmp_path / "inbox.json").read_text())
    assert saved["30"]["options"][0]["key"] == "thread:301" and "thread" not in saved["30"]
