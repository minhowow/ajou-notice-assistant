"""v0.7.0 자동 진행: 규칙표로 단계를 정하고, 2·3단계는 카드 스레드에서 Claude 세션이 진행한다."""
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from ajou_assistant import autopilot, bot, mail, store
from ajou_assistant.notify import Tier
from ajou_assistant.scrape import ListItem
from ajou_assistant.triage import Analysis

from tests.test_lifecycle import helper  # noqa: F401  (fixture)

KST = store.KST
TODAY = date(2026, 11, 16)


def analysis(**fields):
    data = {"relevance": "high", "category": "scholarship", "summary": "장학", "why_relevant": "", "eligibility": "likely",
            "eligibility_reason": "", "action": "", "unknowns": [],
            "deadlines": [{"label": "마감", "date": "2026-11-30", "time": "", "kind": "deadline"}]}
    return Analysis.model_validate({**data, **fields})


def facts(board="scholarship", **fields):
    item = ListItem(1, board, "", "공지", "", "", "2026-11-16")
    return autopilot.notice_facts(item, analysis(**fields), TODAY)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "STATE_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(autopilot, "RULES_FILE", tmp_path / "없음.json")
    monkeypatch.setattr(store, "now", lambda: datetime(2026, 11, 16, 15, 0, tzinfo=KST))
    return tmp_path


def test_rule_table_sets_the_level_and_caps_win():
    assert autopilot.decide(facts())[0] == 3  # 자격 되는 장학
    assert autopilot.decide(facts(board="recruit", category="career"))[0] == 1  # 채용은 캘린더까지
    assert autopilot.decide(facts(relevance="medium", eligibility="likely"))[0] == 1
    assert autopilot.decide(facts(relevance="medium", eligibility="unknown"))[0] == 0
    assert autopilot.decide(facts(relevance="low"))[0] == 0
    asks = facts(category="academic", from_others=[{"document": "추천서", "who": "지도교수", "lead_days": 7}])
    assert autopilot.decide(asks)[0] == 2
    event = [mail.MailEvent(title="기말", date="2026-12-18")]
    exam = mail.Mail(id="m", thread="t", sender="<do-not-reply@blackboard.com>", relevance="medium", events=event)
    assert autopilot.decide(autopilot.mail_facts(exam, True))[0] == 1  # 내 수업(아주Bb) 메일
    # 11/16 선배만남의 날: 학교 발신이지만 나와 관련이 낮다 → 캘린더에 넣지 않는다
    career = mail.Mail(id="c", thread="t3", sender="직원 <x@ajou.ac.kr>", relevance="low", events=event)
    assert autopilot.decide(autopilot.mail_facts(career, True))[0] == 0
    assert autopilot.decide(autopilot.mail_facts(career.model_copy(update={"relevance": "high"}), True))[0] == 1
    outside = mail.Mail(id="o", thread="t4", sender="광고 <ad@example.com>", relevance="high", events=event)
    assert autopilot.decide(autopilot.mail_facts(outside, False))[0] == 0  # 학교 밖 발신은 캘린더도 하지 않는다
    professor = mail.Mail(id="p", thread="t2", kind="교수·교직원", action="서명본 회신", relevance="high")
    assert autopilot.decide(autopilot.mail_facts(professor, True))[0] == 2
    assert autopilot.decide(autopilot.mail_facts(professor.model_copy(update={"relevance": "none"}), True))[0] == 0


def test_claim_once_per_topic_with_a_daily_limit_and_shadow_mode(state):
    rules = {**autopilot.DEFAULT, "daily_limit": 2}
    assert autopilot.claim("notice:1", 3, rules) == ""
    assert autopilot.claim("notice:1", 3, rules) == "이미 진행함"
    assert autopilot.claim("notice:2", 2, rules) == ""
    assert autopilot.claim("notice:3", 2, rules).startswith("오늘 상한")
    assert autopilot.claim("notice:4", 1, rules) == "단계 낮음"
    assert autopilot.claim("notice:5", 3, {**rules, "shadow": True}) == "그림자 운영"
    assert "그림자" in autopilot.badge(3, {**rules, "shadow": True})
    steps = autopilot.plan(3, facts(from_others=[{"document": "추천서", "who": "지도교수", "lead_days": 14}]))
    assert steps[0] == autopilot.CALENDAR_STEP and "부탁 메일을 Gmail 초안으로 (지도교수: 추천서)" in steps[2] and "지원서" in steps[3]
    line = autopilot.task_line(3, "notice:1", "notice", steps)
    assert line.startswith("🤖 이 공지는 여기까지 해 둡니다:") and "끼어" not in line and "이어서 할 일" in line
    assert autopilot.parse_tag(line) == (3, "notice", "notice:1") and autopilot.parse_tag("그냥 글") is None
    assert autopilot.steps_from(line) == steps
    prompt = autopilot.task_prompt(3, "notice", steps)
    assert "공지를 보고" in prompt and autopilot.CALENDAR_STEP not in prompt and "지원서·신청서 양식" in prompt
    assert "제출과 발송은 하지 않는다" in autopilot.task_prompt(2, "mail")
    assert autopilot.plan(2, autopilot.mail_facts(mail.Mail(id="p", thread="t", action="회신", relevance="high"), True))[-1].startswith("이 메일에 보낼 답장을 Gmail 초안으로")
    assert "replyToMessageId" in autopilot.task_prompt(2, "mail")


def test_rules_file_overrides_the_defaults(state):
    (state / "rules.json").write_text(json.dumps({"_comment": "x", "daily_limit": 1, "rules": [
        {"name": "모든 공지 초안", "when": {"source": "notice"}, "level": 3}]}))
    autopilot.RULES_FILE = state / "rules.json"
    rules = autopilot.load_rules()
    assert rules["daily_limit"] == 1 and rules["quiet_hours"] == [23, 8]
    assert autopilot.decide(facts(relevance="low"), rules) == (3, ["모든 공지 초안"])


def test_notice_over_the_bar_gets_a_badge_and_a_task_line_in_its_card_thread(state, monkeypatch):
    from ajou_assistant import cli
    from tests.test_reposts import FakeDiscord
    monkeypatch.setattr(cli, "SESSIONS_FILE", state / "threads.json")
    monkeypatch.setattr(store, "recent_analysis", lambda *args, **kwargs: [])
    monkeypatch.setattr(store, "append_analysis", lambda row: None)
    detail = SimpleNamespace(content_hash="h", body_text="본문" * 100, image_urls=[], attachments=[])
    result = SimpleNamespace(analysis=analysis(), usage={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, model="claude-opus-5-5")
    monkeypatch.setattr(cli, "analyze", lambda *args, **kwargs: (detail, SimpleNamespace(failures=[], images=[]), [], [], result))
    monkeypatch.setattr(cli, "find_repost", lambda client, item, analysis: (None, 0.0))
    calendar = []
    monkeypatch.setattr(cli, "add_to_calendar", lambda item, analysis, today, group=None: calendar.append(group) or {})
    monkeypatch.setattr(cli, "load_discord", lambda: {})
    monkeypatch.setattr(cli, "calendar_ready", lambda: True)
    monkeypatch.setattr(cli, "upload_images", lambda originals: [])
    monkeypatch.setattr(cli, "record", lambda *args, **kwargs: None)
    fake = FakeDiscord()
    item = ListItem(42, "scholarship", "", "OO장학금", "", "", "2026-11-16", link="https://www.ajou.ac.kr/42")
    cli.handle(item, SimpleNamespace(detail=lambda item, board: detail), None, fake, TODAY)
    card, task = fake.sent[0], fake.sent[1]
    shown = json.dumps(card[1]["embeds"][0], ensure_ascii=False)
    assert "지원서·신청서 양식을 내 정보로 채운 초안" in shown and autopilot.CALENDAR_STEP in shown and calendar == [42]
    assert fake.opened == [(card[0], "m1", "OO장학금")]
    assert task[0] == "m1" and task[2] == Tier.QUIET and autopilot.parse_tag(task[1]["content"]) == (3, "notice", "notice:42")
    assert "- 지원서·신청서 양식을 내 정보로 채운 초안" in task[1]["content"]
    fake = FakeDiscord()  # 같은 공지가 다시 와도 한 번만
    cli.handle(item, SimpleNamespace(detail=lambda item, board: detail), None, fake, TODAY)
    assert len(fake.sent) == 1 and "이미 진행함" in json.dumps(fake.sent[0][1]["embeds"][0], ensure_ascii=False)


def test_bot_queues_the_task_and_runs_it_outside_quiet_hours(helper, state, monkeypatch):  # noqa: F811
    from tests.test_lifecycle import add_thread
    monkeypatch.setattr(bot, "AUTORUN_FILE", state / "autoruns.json")
    monkeypatch.setattr(bot, "notice_for_message", lambda message_id: {"id": 42, "title": "OO장학금"} if message_id == 400 else None)
    add_thread(helper, 400)
    helper.sessions.pop("400")
    ran = []

    async def converse(thread, message):
        ran.append((thread.id, message.content, message.auto))
    helper.converse = converse
    steps = autopilot.plan(3, facts())
    own = SimpleNamespace(content=autopilot.task_line(3, "notice:42", "notice", steps), guild=SimpleNamespace(id=helper.guild_id),
                          channel=SimpleNamespace(id=400))
    helper.queue_autorun(own)
    helper.queue_autorun(own)  # 같은 표시가 두 번 와도 한 번
    assert len(json.loads((state / "autoruns.json").read_text())["queue"]) == 1
    monkeypatch.setattr(store, "now", lambda: datetime(2026, 11, 16, 23, 30, tzinfo=KST))
    assert asyncio.run(helper.run_autorun()) is False and not ran  # 밤에는 아침까지 둔다
    monkeypatch.setattr(store, "now", lambda: datetime(2026, 11, 17, 8, 5, tzinfo=KST))
    assert asyncio.run(helper.run_autorun()) is True
    assert ran[0][0] == 400 and ran[0][1].startswith("[자동 진행]") and "지원서·신청서 양식" in ran[0][1] and ran[0][2] is True
    assert helper.sessions["400"]["notice"] == 42
    assert asyncio.run(helper.run_autorun()) is False


def test_idle_record_queues_a_followup_only_after_the_user_spoke(helper, state, monkeypatch):  # noqa: F811
    """11/16: '기록했습니다' 대신, 마무리 안 된 일 중 비서가 할 수 있는 것을 후속 정리로 이어 한다."""
    from ajou_assistant import record
    from tests.test_lifecycle import add_thread
    monkeypatch.setattr(bot, "AUTORUN_FILE", state / "autoruns.json")
    monkeypatch.setattr(bot, "sync_push", lambda *args: None)
    monkeypatch.setattr(record, "write_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(record, "apply_events", lambda *args, **kwargs: [])
    result = {"summary": "", "decisions": [], "facts": [], "events": [], "questions": [],
              "todos": [{"task": "지원 동기 초안 보완", "due": "", "owner": "assistant"}, {"task": "교수님께 메일", "due": "", "owner": "me"}]}
    monkeypatch.setattr(record, "extract", lambda *args: (result, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, "claude-opus-5-5"))
    helper._api = object()
    thread = add_thread(helper, 500)
    folder = state / "outputs" / "threads" / "500"
    record.log_turn(folder, "나", "지원 동기 같이 보자", datetime(2026, 11, 16, 14, 0, tzinfo=KST))
    flush = bot.Assistant.flush_thread.__get__(helper)
    assert asyncio.run(flush("500", thread)) is True
    [job] = json.loads((state / "autoruns.json").read_text())["queue"]
    assert job["thread"] == 500 and job["prompt"].startswith("[후속 정리]") and "지원 동기 초안 보완" in job["prompt"]
    assert not [event for event in helper.events if event[0] == "send"]  # '기록했습니다' 목록을 올리지 않는다
    record.log_turn(folder, "자동 진행", "[후속 정리] …", datetime(2026, 11, 16, 15, 0, tzinfo=KST))
    asyncio.run(flush("500", thread))  # 후속 정리 턴만 있었으면 다시 하지 않는다
    assert len(json.loads((state / "autoruns.json").read_text())["queue"]) == 1
    record.log_turn(folder, "나", "더 보자", datetime(2026, 11, 16, 16, 0, tzinfo=KST))
    asyncio.run(flush("500", thread, followup=False))  # 닫을 때는 하지 않는다
    assert len(json.loads((state / "autoruns.json").read_text())["queue"]) == 1
