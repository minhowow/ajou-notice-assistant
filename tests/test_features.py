"""11/3 구조 개편: 커넥터 검사, 실행 방식, 영어 복습, 남에게 부탁할 서류, 주간 정리."""
import json
from datetime import date, datetime

from ajou_assistant import english, guard, store, weekly
from ajou_assistant.triage import Analysis

OWNERS = {"me@ajou.ac.kr", "me@gmail.com"}
CAL = "mcp__claude_ai_Google_Calendar__"
NOTION = "mcp__claude_ai_Notion__"


def test_guard_blocks_delete_and_outside_attendees():
    assert guard.verdict(CAL + "delete_event", {"event_id": "x"}, OWNERS)
    invite = {"summary": "회의", "attendees": [{"email": "me@ajou.ac.kr"}, {"email": "Someone@Example.com"}]}
    reason = guard.verdict(CAL + "create_event", invite, OWNERS)
    assert reason and "someone@example.com" in reason
    assert guard.verdict(CAL + "update_event", {"event": {"guests": "other@x.org"}}, OWNERS)


def test_guard_allows_own_events_and_emails_in_description():
    event = {"summary": "장학 마감", "description": "문의 iss@ajou.ac.kr", "attendees": ["me@gmail.com"]}
    assert guard.verdict(CAL + "create_event", event, OWNERS) is None
    assert guard.verdict(CAL + "list_events", {}, OWNERS) is None
    assert guard.verdict(CAL + "respond_to_event", {"response": "accepted"}, OWNERS) is None


def test_guard_keeps_notion_read_only_and_other_connectors_out():
    assert guard.verdict(NOTION + "notion-search", {"query": "ACC"}, OWNERS) is None
    assert guard.verdict(NOTION + "notion-update-page", {}, OWNERS)
    assert guard.verdict(NOTION + "notion-delete-pages", {}, OWNERS)
    assert guard.verdict("mcp__claude_ai_Gmail__send", {}, OWNERS)
    assert guard.verdict("Write", {}, OWNERS) is None
    assert all(not name.endswith("delete_event") for name in guard.tool_names())


def test_plan_mode_adds_connectors_and_hook_but_not_api_key(monkeypatch):
    from ajou_assistant import bot
    monkeypatch.setattr(bot, "load_secrets", lambda: {"ANTHROPIC_API_KEY": "k", "AJOU_OWNER_EMAILS": "me@gmail.com"})
    monkeypatch.setattr(bot, "load_discord", lambda: {"calendar_account": "me@ajou.ac.kr"})
    plan = bot.claude_command("outputs/threads/1", None, True, "study")
    allowed = plan[plan.index("--allowedTools") + 1]
    assert CAL + "create_event" in allowed and CAL + "delete_event" not in allowed
    assert "Write(study/**)" in allowed and "Gmail" not in allowed
    hooks = json.loads(plan[plan.index("--settings") + 1])["hooks"]["PreToolUse"]
    assert "guard.py" in hooks[0]["hooks"][0]["command"]
    api = bot.claude_command("outputs/threads/1", None, False, "chat")
    assert "mcp__" not in api[api.index("--allowedTools") + 1]
    # API 키로 돌 때도 파일 쓰기 검사와 세션 보관 기간은 같다.
    assert json.loads(api[api.index("--settings") + 1]) == json.loads(plan[plan.index("--settings") + 1])
    environment = bot.turn_environment(True)
    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["AJOU_OWNER_EMAILS"] == "me@ajou.ac.kr,me@gmail.com"
    assert int(environment["MCP_TIMEOUT"]) >= 30000 and "MCP_TIMEOUT" not in bot.turn_environment(False)
    assert bot.turn_environment(False)["ANTHROPIC_API_KEY"] == "k"


def test_plan_failure_falls_back_only_for_plan_problems():
    from ajou_assistant.bot import plan_failed
    assert plan_failed({"is_error": True, "result": "Claude AI usage limit reached"})
    assert plan_failed({"is_error": True, "result": "Invalid bearer token (401)"})
    assert not plan_failed({"is_error": False, "result": "limit을 설명하면"})
    assert not plan_failed({"is_error": True, "result": "시간 초과"})


def test_plan_turns_are_not_counted_as_api_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "USAGE_DIR", tmp_path)
    store.add_usage("triage", 1.0)
    store.add_usage("thread-plan", 5.0)
    store.add_usage("thread", 0.5)
    assert store.month_usage() == 1.5
    assert store.month_usage("thread") == 0.5


def test_english_spacing_and_reset():
    row = {"word": "abate", "added": "2026-11-03"}
    assert english.next_due(row, []) == (date(2026, 11, 4), 0)
    known = [{"at": f"2026-11-{day:02d}T08:30:00", "known": True} for day in (4, 7)]
    assert english.next_due(row, known) == (date(2026, 11, 14), 2)
    missed = known + [{"at": "2026-11-14T08:30:00", "known": False}]
    assert english.next_due(row, missed) == (date(2026, 11, 15), 0)
    five = [{"at": f"2026-11-{day:02d}T08:30:00", "known": True} for day in (4, 7, 14, 28)] + [{"at": "2026-12-28T08:30:00", "known": True}]
    due, step = english.next_due(row, five)
    assert step == 5 and (due - date(2026, 12, 28)).days == english.MASTERED_DAYS


def test_english_words_dedupe_and_quiz(tmp_path, monkeypatch):
    monkeypatch.setattr(english, "WORDS_FILE", tmp_path / "words.jsonl")
    monkeypatch.setattr(english, "REVIEWS_FILE", tmp_path / "reviews.jsonl")
    found = [{"word": "Abate", "pos": "v.", "meaning": "약해지다", "example": "The storm abated.", "example_ko": "폭풍이 약해졌다.", "collocations": []},
             {"word": "  ", "pos": "", "meaning": "", "example": "", "example_ko": "", "collocations": []}]
    new, again = english.add_words(found, date(2026, 11, 3), "test")
    assert [row["word"] for row in new] == ["Abate"] and not again
    new, again = english.add_words([{**found[0], "word": "abate"}], date(2026, 11, 4), "test")
    assert not new and len(again) == 1
    rows, backlog = english.due_words(date(2026, 11, 4))
    assert backlog == 1 and "||" in english.quiz_text(rows, backlog)
    english.record_review(["abate"], True, datetime(2026, 11, 4, 8, 40))
    assert english.due_words(date(2026, 11, 4)) == ([], 0)
    assert "단어 1개" in english.summary_line(date(2026, 11, 5))


def test_old_analysis_rows_still_load_and_asks_count_back_from_deadline():
    from ajou_assistant.notify import ask_dates, format_asks
    base = {"relevance": "high", "category": "scholarship", "summary": "", "why_relevant": "", "eligibility": "likely",
            "eligibility_reason": "", "action": "", "unknowns": [],
            "deadlines": [{"label": "마감", "date": "2026-11-13", "time": "18:00", "kind": "deadline"}]}
    assert Analysis.model_validate(base).from_others == []
    analysis = Analysis.model_validate({**base, "from_others": [{"document": "추천서", "who": "지도교수", "lead_days": 14}]})
    [(prep, ask)] = ask_dates(analysis, date(2026, 11, 3))
    assert ask == date(2026, 10, 30)
    assert "지금 바로" in format_asks(analysis, date(2026, 11, 3))
    assert "D-2" in format_asks(analysis, date(2026, 10, 28))


def test_ask_reminders_fire_once_when_late():
    from ajou_assistant.cli import send_ask_reminders
    analysis = Analysis.model_validate({
        "relevance": "high", "category": "scholarship", "summary": "", "why_relevant": "", "eligibility": "likely",
        "eligibility_reason": "", "action": "", "unknowns": [],
        "deadlines": [{"label": "마감", "date": "2026-11-13", "time": "", "kind": "deadline"}],
        "from_others": [{"document": "추천서", "who": "지도교수", "lead_days": 14}]})

    class Fake:
        dry_run = True
        sent: list = []

        def send(self, channel, payload, tier=None):
            self.sent.append((channel, payload["content"]))

    fake = Fake()
    row = {"id": 1, "title": "장학", "url": "https://example.ac.kr"}
    seen = {"1:ask:추천서:late"}
    assert send_ask_reminders(fake, row, analysis, date(2026, 11, 3), seen) == 0
    assert send_ask_reminders(fake, row, analysis, date(2026, 11, 3), set()) == 1
    assert "지도교수" in fake.sent[0][1]


def test_weekly_reads_across_month_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly, "DATA_DIR", tmp_path)
    store.append_jsonl(tmp_path / "2026-10.jsonl", {"at": "2026-10-28T10:00:00+09:00", "title": "옛 기록"})
    store.append_jsonl(tmp_path / "2026-10.jsonl", {"at": "2026-10-30T10:00:00+09:00", "title": "지난 금요일"})
    store.append_jsonl(tmp_path / "2026-11.jsonl", {"at": "2026-11-03T10:00:00+09:00", "title": "화요일"})
    rows = weekly.week_rows(date(2026, 11, 5))
    assert [row["title"] for row in rows] == ["지난 금요일", "화요일"]
    result = {"summary": "요약", "areas": [{"area": "영어", "done": ["단어 20개"], "next": ["복습"]}],
              "questions": ["물을 것"], "material": []}
    text = weekly.render(result, date(2026, 11, 8), 2, datetime(2026, 11, 8, 21, 0))
    assert "2026-W45" in text and "- [ ] 복습" in text
    assert set(weekly.SCHEMA["required"]) == set(weekly.SCHEMA["properties"])


def test_interactions_answer_before_slow_work():
    """버튼 응답 전에 git push를 하면 3초를 넘겨 '상호작용 실패'가 뜬다 (11/3 장애)."""
    import inspect
    from ajou_assistant.bot import Assistant
    for method in (Assistant.on_interaction, Assistant.answer_quiz):
        source = inspect.getsource(method)
        for respond in ("interaction.response.send_message", "interaction.response.edit_message"):
            if respond in source and "sync_push" in source:
                assert source.index(respond) < source.index("sync_push"), method.__name__
