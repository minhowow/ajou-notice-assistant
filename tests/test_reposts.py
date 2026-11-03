"""재공지 묶기 (11/3): 후보 고르기, AI 판단 검증, 묶음 단위 독촉, 스레드·답장 알림, 봇의 다음 턴 자료. 핀과 추적 (11/3)."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from ajou_assistant import repost, store
from ajou_assistant.notify import Tier

DEADLINE = {"label": "마감", "date": "2026-11-13", "time": "18:00", "kind": "deadline"}


def analysis(relevance="high", deadlines=(DEADLINE,), summary="장학생 모집"):
    return {"relevance": relevance, "category": "scholarship", "summary": summary, "why_relevant": "",
            "eligibility": "likely", "eligibility_reason": "", "action": "", "unknowns": [],
            "deadlines": list(deadlines), "from_others": []}


def row(article_id, title, analyzed_at, posted="2026-11-01", board="scholarship", **extra):
    return {"id": article_id, "board": board, "title": title, "url": f"https://www.ajou.ac.kr/{article_id}",
            "date": posted, "analyzed_at": analyzed_at, "analysis": extra.pop("result", analysis()), **extra}


@pytest.fixture
def notices(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(store, "REPOSTS_FILE", tmp_path / "reposts.jsonl")
    monkeypatch.setattr(store, "ACTIONS_FILE", tmp_path / "actions.jsonl")
    monkeypatch.setattr(store, "REMINDERS_FILE", tmp_path / "reminders.jsonl")
    monkeypatch.setattr(store, "now", lambda: datetime(2026, 11, 10, 9, 0, tzinfo=store.KST))

    def add(*rows):
        for item in rows:
            store.append_jsonl(tmp_path / "analysis" / "2026-11.jsonl", item)
    return add


def test_titles_match_reposts_but_not_other_rounds():
    assert repost.similarity("[재공지][글로벌교양학부] 제109회 TOPIK 대비 특강(~11/11(수) 18시까지)",
                             "[글로벌교양학부] 제109회 TOPIK 대비 특강(~11/11(수) 18시까지)") == 1.0
    assert repost.similarity("📢(마감임박)2026 공학교육 캡스톤디자인 경진대회 신청 접수",
                             "(접수중)2026 공학교육 캡스톤디자인 경진대회 안내") >= repost.MIN_RATIO
    assert repost.similarity("「2026 블록체인 밋업데이(BCMD)」 30회차 교육생 모집",
                             "「2026 블록체인 밋업데이(BCMD)」31회차 교육생 모집") == 0.0
    # 괄호 속 근무지는 글을 가르는 정보라 지우지 않는다.
    assert repost.similarity("신입사원모집(평택:IT 인프라)", "신입사원모집(이천:반도체 노광장비 Field Service Engineer)") < repost.MIN_RATIO


def test_candidates_skip_recruit_old_and_own_reposts(notices):
    rows = [row(1, "[교외장학] OO장학금 모집 공고", "2026-11-01T10:00"),
            row(2, "[교외장학] OO장학금 모집 공고", "2026-08-01T10:00", posted="2026-08-01"),
            row(3, "신입사원모집(평택:IT 인프라)", "2026-11-01T10:00", board="recruit")]
    found = repost.candidates(9, "scholarship", "[재공지][교외장학] OO장학금 모집 공고", "2026-11-05", rows)
    assert [item["id"] for item in found] == [1]
    assert repost.candidates(9, "recruit", "신입사원모집(평택:IT 인프라)", "2026-11-05", rows) == []
    store.add_repost(1, 9, "", "test")  # 9의 재공지인 1은 9의 후보가 될 수 없다(순환 방지).
    assert repost.candidates(9, "scholarship", "[재공지][교외장학] OO장학금 모집 공고", "2026-11-05", rows) == []


class FakeClient:
    def __init__(self, results):
        body = json.dumps({"results": results})
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=body)], usage=usage, model="claude-opus-5-5")
        self.messages = SimpleNamespace(create=lambda **kwargs: response)


def test_judge_only_accepts_offered_candidates():
    new = row(9, "재공지", "2026-11-05T10:00")
    case = [(new, [row(1, "원래", "2026-11-01T10:00")])]
    verdicts, _, _ = repost.judge(FakeClient([{"id": "9", "same_as": "1", "changes": " 마감 11/20으로 연장 "}]), case)
    assert verdicts == {"9": {"same_as": 1, "changes": "마감 11/20으로 연장"}}  # 원래 번호 형식(int)을 지킨다
    verdicts, _, _ = repost.judge(FakeClient([{"id": "9", "same_as": "777", "changes": ""}]), case)
    assert verdicts == {}


def test_groups_follow_chains_and_keep_latest_dates_with_highest_relevance(notices):
    extended = dict(DEADLINE, date="2026-11-20")
    notices(row(1, "원래", "2026-11-01T10:00"),
            row(2, "재공지", "2026-11-03T10:00", result=analysis("medium")),
            row(3, "연장", "2026-11-05T10:00", result=analysis("medium", (extended,))),
            row(4, "다른 공지", "2026-11-05T11:00"))
    store.add_repost(2, 1, "", "test")
    store.add_repost(3, 2, "마감 연장", "test")
    assert repost.root_of(3) == 1 and repost.group_ids(1) == {1, 2, 3}
    groups = {group["root"]: group for group in store.notice_groups()}
    assert set(groups) == {1, 4}
    assert groups[1]["members"] == {1, 2, 3}
    assert groups[1]["analysis"]["relevance"] == "high"
    assert groups[1]["analysis"]["deadlines"][0]["date"] == "2026-11-20"


class FakeDiscord:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send(self, channel, payload, files=None, tier=Tier.NOTIFY):
        self.sent.append((channel, payload, tier))
        return "m1"

    def pin(self, channel, message_id, on=True):
        self.sent.append((channel, {"pin": message_id, "on": on}, None))


def test_reminders_go_once_per_group_and_stop_when_any_member_is_closed(notices, monkeypatch):
    from ajou_assistant import cli
    monkeypatch.setattr(cli, "calendar_ready", lambda: False)
    monkeypatch.setattr(cli, "load_discord", lambda: {})
    notices(row(1, "OO장학금", "2026-11-01T10:00"), row(2, "OO장학금", "2026-11-01T10:01"))
    store.add_repost(2, 1, "", "test")
    fake = FakeDiscord()
    assert cli.send_reminders(fake, date(2026, 11, 10)) == 1  # D-3, 두 글이 아니라 한 번
    assert "ajou:done:1" in json.dumps(fake.sent[0][1]["components"])
    assert cli.send_reminders(fake, date(2026, 11, 10)) == 0
    store.mark_item(2, "done")  # 재공지 쪽에서 완료해도 묶음 전체가 멈춘다
    assert cli.send_reminders(fake, date(2026, 11, 12)) == 0


def test_repost_goes_to_thread_then_reply_then_silence(notices, tmp_path, monkeypatch):
    from ajou_assistant import cli
    from ajou_assistant.scrape import ListItem
    sessions = tmp_path / "threads.json"
    monkeypatch.setattr(cli, "SESSIONS_FILE", sessions)
    notices(row(1, "OO장학금", "2026-11-01T10:00", channel="scholarship", message_id="555"))
    item = ListItem(9, "scholarship", "", "[재공지] OO장학금", "", "", "2026-11-05", link="https://www.ajou.ac.kr/9")
    changed = {"parent": 1, "root": 1, "changes": "마감 11/20으로 연장"}

    sessions.write_text(json.dumps({"1234": {"notice": 1, "title": "OO"}}))
    fake = FakeDiscord()
    assert cli.deliver_repost(fake, item, changed) == (None, None)
    [(channel, payload, tier)] = fake.sent
    assert channel == "1234" and tier == Tier.NOTIFY and repost.note_id(payload["content"]) == "9"

    sessions.write_text("{}")
    fake = FakeDiscord()
    assert cli.deliver_repost(fake, item, changed) == ("scholarship", "m1")
    assert fake.sent[0][1]["message_reference"]["message_id"] == "555"

    fake = FakeDiscord()
    assert cli.deliver_repost(fake, item, {**changed, "changes": ""}) == (None, None) and not fake.sent
    store.mark_item(1, "skip")
    assert cli.deliver_repost(fake, item, changed) == (None, None) and not fake.sent


def test_repost_of_unalerted_notice_is_sent_normally(notices, tmp_path, monkeypatch):
    from ajou_assistant import cli
    from ajou_assistant.scrape import ListItem
    monkeypatch.setattr(cli, "SESSIONS_FILE", tmp_path / "없음.json")
    notices(row(1, "OO장학금", "2026-11-01T10:00", result=analysis("low"), channel=None, message_id=None))
    item = ListItem(9, "scholarship", "", "OO장학금", "", "", "2026-11-05", link="https://www.ajou.ac.kr/9")
    assert cli.deliver_repost(FakeDiscord(), item, {"parent": 1, "root": 1, "changes": "자격 완화"}) is None


def test_bot_keeps_thread_notes_for_the_next_turn(tmp_path, monkeypatch):
    import asyncio
    from ajou_assistant import bot
    monkeypatch.setattr(bot, "SESSIONS_FILE", tmp_path / "threads.json")
    monkeypatch.setattr(bot, "notice_row", lambda article: None)
    assistant = bot.Assistant.__new__(bot.Assistant)
    assistant.guild_id = 7
    assistant.sessions = {"1234": {"notice": 1}}
    text = repost.note("[재공지] OO장학금", "https://www.ajou.ac.kr/9", 9, "마감 11/20으로 연장")
    message = SimpleNamespace(content=text, channel=SimpleNamespace(id=1234), guild=SimpleNamespace(id=7))
    assistant.note_repost(message)
    assistant.note_repost(SimpleNamespace(content="평범한 답장", channel=SimpleNamespace(id=1234), guild=SimpleNamespace(id=7)))
    assert assistant.sessions["1234"]["notes"] == [{"id": "9", "text": text}]
    context = asyncio.run(assistant.repost_context(assistant.sessions["1234"], tmp_path))
    assert "마감 11/20으로 연장" in context and repost.NOTE_TAG not in context
    assert "notes" not in assistant.sessions["1234"]
    assert asyncio.run(assistant.repost_context(assistant.sessions["1234"], tmp_path)) == ""


def test_handle_links_repost_and_skips_full_alert(notices, tmp_path, monkeypatch):
    from ajou_assistant import cli
    from ajou_assistant.scrape import ListItem
    from ajou_assistant.triage import Analysis, TriageResult
    monkeypatch.setattr(cli, "SESSIONS_FILE", tmp_path / "threads.json")
    (tmp_path / "threads.json").write_text(json.dumps({"1234": {"notice": 1}}))
    notices(row(1, "OO장학금", "2026-11-01T10:00", channel="scholarship", message_id="555"))
    detail = SimpleNamespace(content_hash="h9", body_text="본문" * 100, image_urls=[])
    result = TriageResult(Analysis.model_validate(analysis()), {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, "claude-opus-5-5")
    monkeypatch.setattr(cli, "analyze", lambda *args, **kwargs: (detail, SimpleNamespace(failures=[], images=[]), [], [], result))
    monkeypatch.setattr(cli, "find_repost", lambda client, item, analysis: ({"parent": 1, "root": 1, "changes": ""}, 0.01))
    calendar = []
    monkeypatch.setattr(cli, "add_to_calendar", lambda item, analysis, today, group=None: calendar.append(group) or {})
    monkeypatch.setattr(cli, "deliver", lambda *args, **kwargs: pytest.fail("재공지에 전체 알림을 보냄"))
    fetcher = SimpleNamespace(detail=lambda item, board: detail)
    item = ListItem(9, "scholarship", "", "[재공지] OO장학금", "", "", "2026-11-05", link="https://www.ajou.ac.kr/9")
    fake = FakeDiscord()
    outcome = cli.handle(item, fetcher, None, fake, date(2026, 11, 5))
    assert outcome["repost_of"] == 1 and outcome["channel"] is None
    assert [channel for channel, _, _ in fake.sent] == ["1234"]
    assert store.load_reposts()[9]["parent"] == 1 and calendar == [1]
    assert {row["id"] for row in store.recent_analysis()} == {1, 9}


def test_reanalyze_keeps_the_original_discord_message(notices, monkeypatch):
    """재판단 기록이 message_id를 잃으면 공지 답장과 재공지 답장이 원래 알림을 못 찾는다."""
    from ajou_assistant import cli
    from ajou_assistant.triage import Analysis, TriageResult
    notices(row(1, "OO장학금", "2026-11-01T10:00", channel="scholarship", message_id="555"))
    result = TriageResult(Analysis.model_validate(analysis()), {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, "claude-opus-5-5")
    monkeypatch.setattr(cli, "analyze", lambda *args, **kwargs: (None, SimpleNamespace(failures=[]), [], [], result))
    monkeypatch.setattr(cli, "make_clients", lambda dry_run: (None, FakeDiscord()))
    monkeypatch.setattr(cli, "Fetcher", lambda: None)
    monkeypatch.setattr(cli, "preflight", lambda: None, raising=False)
    monkeypatch.setattr(cli, "sync_push", lambda *args: None)
    monkeypatch.setattr(cli, "record", lambda item, detail, result, extraction, notes, channel, message_id, *rest: store.append_analysis(
        {"id": item.article_id, "board": "scholarship", "title": item.title, "url": "", "analyzed_at": "2026-11-03T16:00",
         "channel": channel, "message_id": message_id, "analysis": result.analysis.model_dump()}))
    cli.cmd_reanalyze(SimpleNamespace(articles=["1"], dry_run=False))
    [latest] = store.recent_analysis()
    assert latest["message_id"] == "555" and latest["channel"] == "scholarship"


def test_tracked_notice_gets_reminders_even_when_medium(notices, monkeypatch):
    from ajou_assistant import cli
    monkeypatch.setattr(cli, "calendar_ready", lambda: False)
    monkeypatch.setattr(cli, "load_discord", lambda: {})
    contest = dict(DEADLINE, date="2026-11-30")
    notices(row(378230, "생성형 AI 학습 활용 사례 공모전", "2026-11-03T16:00", result=analysis("medium", (contest,))))
    fake = FakeDiscord()
    assert cli.send_reminders(fake, date(2026, 11, 27)) == 0
    store.mark_item(378230, "track")
    assert cli.send_reminders(fake, date(2026, 11, 27)) == 1 and "D-3" in fake.sent[0][1]["content"]
    assert [row["id"] for _, _, row in cli.upcoming_deadlines(date(2026, 11, 20))] == [378230]
    store.mark_item(378230, "done")
    assert cli.send_reminders(fake, date(2026, 11, 29)) == 0


class FakePins:
    """핀 목록을 바꿀 수 있는 채널과 #📌핀 채널."""

    def __init__(self, channel_id):
        self.id = channel_id
        self.pinned = []
        self.posts, self.deleted = [], []

    async def pins(self, limit=50, oldest_first=True):
        for message in list(self.pinned):
            yield message

    async def send(self, text, **kwargs):
        self.posts.append(text)
        return SimpleNamespace(id=900 + len(self.posts))

    def get_partial_message(self, message_id):
        async def delete():
            self.deleted.append(message_id)
        return SimpleNamespace(delete=delete)


def test_pin_is_tracking_and_unpin_releases_only_tracking(notices, tmp_path, monkeypatch):
    import asyncio
    from ajou_assistant import bot
    monkeypatch.setattr(bot, "PINS_FILE", tmp_path / "pins.json")
    pushed = []
    monkeypatch.setattr(bot, "sync_push", lambda *args: pushed.append(args))
    monkeypatch.setattr(bot, "pin_summary", lambda message: f"핀 {message.id}")
    notices(row(1, "OO장학금", "2026-11-01T10:00", channel="scholarship", message_id="555"),
            row(2, "다른 공지", "2026-11-01T10:00", channel="scholarship", message_id="556"))
    channel, board = FakePins(10), FakePins(99)
    thread = FakePins(1234)
    assistant = bot.Assistant.__new__(bot.Assistant)
    assistant.pins_channel = 99
    assistant.sessions = {"1234": {"notice": "2"}}
    assistant.pin_lock = asyncio.Lock()
    assistant.get_channel = lambda channel_id: board
    alert = SimpleNamespace(id=555, channel=channel)

    def sync(where):
        asyncio.run(assistant.sync_pins(where))

    channel.pinned = [alert]
    sync(channel)
    assert store.item_actions()[1] == "track" and board.posts == ["핀 555"] and len(pushed) == 1
    sync(channel)  # 같은 핀은 다시 표시하지 않는다.
    assert len(store.read_jsonl(store.ACTIONS_FILE)) == 1 and len(pushed) == 1
    channel.pinned = []
    sync(channel)
    assert store.item_actions()[1] == "open" and board.deleted == [901]
    # 완료한 공지의 핀을 풀어도 완료는 그대로다.
    channel.pinned = [alert]
    sync(channel)
    store.mark_item(1, "done")
    channel.pinned = []
    sync(channel)
    assert store.item_actions()[1] == "done"
    # 공지 스레드 안의 메시지를 핀해도 그 공지를 추적한다.
    thread.pinned = [SimpleNamespace(id=777, channel=thread)]
    sync(thread)
    assert store.item_actions()[2] == "track"


def test_unpin_releases_tracking_across_reposts(notices, tmp_path, monkeypatch):
    """CLI가 재공지 번호로 추적했어도, 처음 알림의 핀을 풀면 묶음 전체의 추적이 풀린다."""
    from ajou_assistant import bot
    notices(row(1, "OO장학금", "2026-11-01T10:00"), row(9, "[재공지] OO장학금", "2026-11-05T10:00"))
    store.add_repost(9, 1, "", "test")
    store.mark_item(9, "track")
    assert bot.track_pin(1, False) and store.item_actions()[9] == "open"
    assert not bot.track_pin(1, False)


def test_mark_pins_the_first_alert_of_the_group(notices, monkeypatch):
    from ajou_assistant import cli
    notices(row(1, "OO장학금", "2026-11-01T10:00", channel="scholarship", message_id="555"),
            row(9, "[재공지] OO장학금", "2026-11-05T10:00"))
    store.add_repost(9, 1, "", "test")
    fake = FakeDiscord()
    monkeypatch.setattr(cli, "make_clients", lambda dry_run: (None, fake))
    monkeypatch.setattr(cli, "sync_push", lambda *args: None)
    cli.cmd_mark(SimpleNamespace(article="9", action="track"))
    cli.cmd_mark(SimpleNamespace(article="9", action="done"))
    assert [payload for _, payload, _ in fake.sent] == [{"pin": "555", "on": True}, {"pin": "555", "on": False}]
    assert store.item_actions()[9] == "done"
