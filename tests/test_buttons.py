"""알림 버튼과 일정 뒤 기록 요청 (11/4): 📌 핀 버튼, 📅 캘린더 넣기 버튼, 일정 주소, 기록 스레드."""
import asyncio
import base64
from datetime import date, datetime
from types import SimpleNamespace

import discord
import pytest

from ajou_assistant import followup, google_bridge, store
from ajou_assistant.notify import PIN_LABEL, build_components
from ajou_assistant.scrape import ListItem
from ajou_assistant.triage import Analysis, Deadline

KST = store.KST


def analysis(deadlines):
    return Analysis.model_validate({"relevance": "high", "category": "scholarship", "summary": "", "why_relevant": "",
                                    "eligibility": "likely", "eligibility_reason": "", "action": "", "unknowns": [],
                                    "deadlines": [d.model_dump() for d in deadlines], "from_others": []})


# ── 일정 주소 ────────────────────────────────────────────────

def test_event_link_points_at_the_event_not_the_day(monkeypatch):
    monkeypatch.setattr(google_bridge, "_ping", lambda: {"calendar": "me@school.ac.kr", "version": 2})
    link = google_bridge.event_link({"id": "abc123", "link": ""}, "2026-11-20")
    eid = link.split("eid=")[1]
    assert base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4)).decode() == "abc123 me@school.ac.kr"
    # 그날 화면 주소를 받았으면 일정 주소로 바꾼다.
    assert "eid=" in google_bridge.event_link({"id": "abc123", "link": "https://calendar.google.com/calendar/r/day/2026/11/20"})
    assert google_bridge.event_link({"id": "x", "link": "https://www.google.com/calendar/event?eid=zz"}).endswith("eid=zz")
    assert google_bridge.event_link({}, "2026-11-20").endswith("/r/day/2026/11/20")


def test_reminder_buttons_find_the_event_by_key(monkeypatch):
    from ajou_assistant import cli
    deadline = Deadline(label="마감", date="2026-11-20", time="17:00", kind="deadline")
    rows = [{"id": "e1", "key": "notice:7:deadline:2026-11-20", "link": "https://www.google.com/calendar/event?eid=e1",
             "start": "2026-11-20T17:00:00+09:00", "allDay": False}]
    monkeypatch.setattr(google_bridge, "list_events", lambda first, last: rows)
    links = cli.calendar_links_for(7, analysis([deadline]), date(2026, 11, 18))
    assert list(links.values()) == ["https://www.google.com/calendar/event?eid=e1"]


def test_calendar_entry_reuses_my_own_event_at_the_same_time(monkeypatch):
    from ajou_assistant import cli
    deadline = Deadline(label="마감", date="2026-11-20", time="17:00", kind="deadline")
    mine = {"id": "u1", "key": "", "link": "https://www.google.com/calendar/event?eid=u1", "start": "2026-11-20T17:00:00+09:00", "allDay": False}
    monkeypatch.setattr(google_bridge, "list_events", lambda first, last: [mine])
    monkeypatch.setattr(google_bridge, "add_event", lambda *args, **kwargs: pytest.fail("사용자 일정이 있는데 또 넣음"))
    assert cli.calendar_entry("공지", "https://a.kr/1", deadline, 7).endswith("eid=u1")
    added = []
    monkeypatch.setattr(google_bridge, "list_events", lambda first, last: [])
    monkeypatch.setattr(google_bridge, "add_event", lambda key, *args, **kwargs: added.append(key) or {"event": {"link": "L"}})
    assert cli.calendar_entry("공지", "https://a.kr/1", deadline, 7) == "L" and added == ["notice:7:deadline:2026-11-20"]


# ── 버튼 ──────────────────────────────────────────────────────

def test_components_offer_pin_and_one_tap_calendar():
    item = ListItem(9, "scholarship", "https://www.ajou.ac.kr/x.do", "제목", "", "", "2026-11-01")
    deadlines = [Deadline(label="마감", date="2026-11-20", time="17:00", kind="deadline"),
                 Deadline(label="설명회", date="2026-11-21", time="", kind="event")]
    links, actions = build_components(item, analysis(deadlines), date(2026, 11, 2), can_add=True)
    assert [b.get("custom_id") for b in links["components"][1:]] == ["ajou:cal:9:0", "ajou:cal:9:1"]
    assert {"label": PIN_LABEL, "custom_id": "ajou:pin:9"}.items() <= actions["components"][1].items()
    assert all(len(row["components"]) <= 5 for row in (links, actions))
    # 캘린더 연결이 없으면 예전처럼 '일정 추가' 화면 주소를 단다.
    links, _ = build_components(item, analysis(deadlines), date(2026, 11, 2))
    assert "TEMPLATE" in links["components"][1]["url"]


class Board:
    def __init__(self):
        self.posts, self.deleted = [], []

    async def send(self, text, **kwargs):
        self.posts.append(text)
        return SimpleNamespace(id=500 + len(self.posts))

    def get_partial_message(self, message_id):
        async def delete():
            self.deleted.append(message_id)
        return SimpleNamespace(delete=delete)

    async def pins(self, limit=50, oldest_first=True):
        for message in []:
            yield message


def assistant(monkeypatch, tmp_path, board):
    from ajou_assistant import bot
    monkeypatch.setattr(bot, "PINS_FILE", tmp_path / "pins.json")
    monkeypatch.setattr(bot, "sync_push", lambda *args: None)
    monkeypatch.setattr(bot, "pin_summary", lambda message: f"핀 {message.id}")
    helper = bot.Assistant.__new__(bot.Assistant)
    helper.pins_channel = 99
    helper.pin_lock = asyncio.Lock()
    helper.sessions = {}
    helper.get_channel = lambda channel_id: board
    return bot, helper


@pytest.fixture
def notices(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ACTIONS_FILE", tmp_path / "actions.jsonl")
    monkeypatch.setattr(store, "REPOSTS_FILE", tmp_path / "reposts.jsonl")


def test_pin_button_works_without_discord_pin_permission(notices, tmp_path, monkeypatch):
    board = Board()
    bot, helper = assistant(monkeypatch, tmp_path, board)

    async def refuse():
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    message = SimpleNamespace(id=555, pinned=False, channel=SimpleNamespace(id=10), pin=refuse)
    note = asyncio.run(helper.set_pin(message, 7, True))
    assert "권한" in note and board.posts == ["핀 555"] and store.item_actions()[7] == "track"
    assert bot.load_json(bot.PINS_FILE)["555"]["manual"] is True
    # Discord 고정 목록을 맞출 때 버튼으로 모은 것은 지우지 않는다.
    asyncio.run(helper.sync_pins(SimpleNamespace(id=10, pins=board.pins)))
    assert "555" in bot.load_json(bot.PINS_FILE) and board.deleted == []
    asyncio.run(helper.set_pin(message, 7, False))
    assert board.deleted == [501] and bot.load_json(bot.PINS_FILE) == {} and store.item_actions()[7] == "open"


def test_done_keeps_done_when_it_clears_the_pin(notices, tmp_path, monkeypatch):
    board = Board()
    _, helper = assistant(monkeypatch, tmp_path, board)

    async def refuse():
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    message = SimpleNamespace(id=556, pinned=False, channel=SimpleNamespace(id=10), pin=refuse)
    asyncio.run(helper.set_pin(message, 8, True))
    store.mark_item(8, "done")
    asyncio.run(helper.set_pin(message, 8, False, release=False))
    assert store.item_actions()[8] == "done" and board.deleted == [501]


# ── 일정 뒤 기록 요청 ─────────────────────────────────────────

def event(title, start, end, **extra):
    return {"id": title, "title": title, "start": start, "end": end, "allDay": False, "key": "", "status": "", **extra}


def test_followup_picks_talks_and_meetings_not_classes_or_tasks():
    now = datetime(2026, 11, 4, 21, 0, tzinfo=KST)
    rows = [
        event("릴레이 글로벌 [미국(시애틀)-Apple]", "2026-11-03T19:00:00+09:00", "2026-11-03T20:30:00+09:00", key="notice:1", status="accepted"),
        event("팔 325 시프", "2026-11-04T09:00:00+09:00", "2026-11-04T10:30:00+09:00"),
        event("이석원 교수님 면담", "2026-11-04T15:10:00+09:00", "2026-11-04T15:50:00+09:00"),
        event("[면담 후보] 이석원 교수님", "2026-11-04T12:10:00+09:00", "2026-11-04T13:00:00+09:00", key="hold:1", status="needsAction"),
        event("[마감] 서류 접수", "2026-11-04T17:00:00+09:00", "2026-11-04T17:30:00+09:00", key="notice:2", status="accepted"),
        event("AJESS 보드게임 나이트", "2026-11-04T18:00:00+09:00", "2026-11-04T20:00:00+09:00", key="thread:3", status="needsAction"),
        event("진로 특강", "2026-11-04T19:00:00+09:00", "2026-11-04T20:45:00+09:00", status="declined"),
        event("취업 특강", "2026-11-04T20:00:00+09:00", "2026-11-04T20:50:00+09:00"),  # 끝난 지 30분이 안 됐다.
        event("인턴 면접", "2026-11-02T10:00:00+09:00", "2026-11-02T11:00:00+09:00"),  # 36시간이 지났다.
        event("글로벌 취·창업 톡톡톡 Apple 시애틀 (Zoom)", "2026-11-03T19:00:00+09:00", "2026-11-03T20:30:00+09:00"),
    ]
    assert [row["title"] for row in followup.due(rows, now, set())] == [
        "릴레이 글로벌 [미국(시애틀)-Apple]", "글로벌 취·창업 톡톡톡 Apple 시애틀 (Zoom)", "이석원 교수님 면담"]
    asked = {"릴레이 글로벌 [미국(시애틀)-Apple]", "글로벌 취·창업 톡톡톡 Apple 시애틀 (Zoom)"}
    assert [row["title"] for row in followup.due(rows, now, asked)] == ["이석원 교수님 면담"]
    assert followup.quiet_hours(datetime(2026, 11, 4, 23, 30)) and not followup.quiet_hours(datetime(2026, 11, 4, 9, 0))
    assert followup.prompt(rows[0], now).startswith("✍️ 어제 19:00 「릴레이 글로벌")


def test_followup_skips_tentative_holds():
    """11/4 점검: '[면담 후보]'만 건너뛰어 '[면접 후보]'·'[미정·AIWS]' 같은 가안 일정이 빠져 있었다."""
    now = datetime(2026, 11, 10, 21, 0, tzinfo=KST)
    start, end = "2026-11-10T15:00:00+09:00", "2026-11-10T15:30:00+09:00"
    for title in ("[면접 후보] AIWS 버디", "[미정·AIWS] 버디 면접", "[면담 후보] 교수님", " [후보] 인터뷰"):
        assert not followup.worth(event(title, start, end)), title
        assert not followup.worth(event(title, start, end, key="thread:1", status="accepted")), title
    # 대괄호가 첫머리가 아니거나 다른 말이면 평소대로 고른다.
    assert followup.worth(event("AIWS 버디 면접", start, end))
    assert followup.worth(event("면접 [후보 확정 뒤]", start, end))
    assert followup.worth(event("[AIWS] 버디 면접", start, end))


def test_reflection_thread_keeps_words_verbatim(tmp_path, monkeypatch):
    from ajou_assistant import bot
    monkeypatch.setattr(followup, "REFLECTIONS_DIR", tmp_path / "records" / "reflections")
    monkeypatch.setattr(bot, "HOME_DIR", tmp_path)
    monkeypatch.setattr(bot, "THREAD_DIR", tmp_path / "outputs" / "threads")
    monkeypatch.setattr(bot, "SESSIONS_FILE", tmp_path / "threads.json")
    monkeypatch.setattr(bot, "sync_push", lambda *args: None)
    monkeypatch.setattr(store, "now", lambda: datetime(2026, 11, 4, 21, 5, tzinfo=KST))
    helper = bot.Assistant.__new__(bot.Assistant)
    helper.sessions = {"77": {"title": "✍️ Apple", "mode": "reflection",
                              "event": {"id": "e", "title": "릴레이 글로벌 [미국(시애틀)-Apple]",
                                        "start": "2026-11-03T19:00:00+09:00", "end": "2026-11-03T20:30:00+09:00"}}}
    sent = []

    async def send(text, **kwargs):
        sent.append(text)
    thread = SimpleNamespace(id=77, name="✍️ Apple", send=send)
    asyncio.run(helper.reflect(thread, SimpleNamespace(content="재미를 따라가라는 말이 남았다.", attachments=[])))
    asyncio.run(helper.reflect(thread, SimpleNamespace(content="메일로 질문 하나 보내 보기.", attachments=[])))
    path = tmp_path / "records" / "reflections" / "2026-11-03_릴레이_글로벌_미국_시애틀_Apple.md"
    text = path.read_text()
    assert text.startswith("# 릴레이 글로벌 [미국(시애틀)-Apple]") and "재미를 따라가라는 말이 남았다." in text and "메일로 질문" in text
    assert "이어서 써 주세요" in sent[0] and "이어서" not in sent[1]
    assert (tmp_path / "outputs" / "threads" / "77" / "log.md").read_text().count("· 나") == 2
    assert helper.sessions["77"]["last"].startswith("2026-11-04T21:05")
