import io
import json
import struct
import zipfile
import zlib
from datetime import date

import pytest
from PIL import Image

from ajou_assistant import store
from ajou_assistant.config import Board
from ajou_assistant.extract import (
    Extraction, _hwp_para_text, extract_file, prepare_image,
)
from ajou_assistant.notify import (
    Discord, DiscordError, Tier, build_components, build_embed, calendar_url, fit_embed, format_deadlines, route, split_message,
)
from ajou_assistant.scrape import Detail, ListItem, is_public_url, parse_list, parse_programs, parse_recruits, parse_view, site_domain
from ajou_assistant.triage import Analysis, Deadline, build_content

BOARD = Board("scholarship", "장학", "https://www.ajou.ac.kr/kr/ajou/notice_scholarship.do")

LIST_HTML = """
<table><tr class=" b-top-box">
<td class="b-num-box num-notice"><span>공지</span></td><td>교외장학</td>
<td class="b-td-left"><div class="b-title-box">
<a href="?mode=view&amp;articleNo=378223&amp;article.offset=0&amp;articleLimit=10" title="[국가근로] 신청 안내 자세히 보기"><span class="b-notice">[공지]</span>[국가근로] 신청 안내</a>
<div class="b-m-con"><span class="b-cate">교외장학</span><span class="b-writer">학생지원팀</span><span class="b-date">26.10.29</span></div>
</div></td>
<td class="b-no-right"><ul><li><a href="?mode=download&amp;articleNo=378223&amp;attachNo=335758" title="첨부파일다운로드">붙임1.pdf</a></li></ul></td>
<td>학생지원팀</td><td>2026-10-29</td></tr></table>"""

VIEW_HTML = """
<div class="b-file-box"><ul><li><a href="?mode=file-download&amp;articleNo=1&amp;attachNo=7">붙임.hwp</a></li></ul></div>
<div class="b-content-box"><div class="fr-view"><p>본문 <b>내용</b></p><script>x()</script>
<img src="/_attach/ajou/editor-image/a.png"><img src="https://upload.wikimedia.org/x.png"></div></div>"""


def analysis(**overrides) -> Analysis:
    values = dict(relevance="high", category="scholarship", summary="요약", why_relevant="이유",
                  eligibility="unknown", eligibility_reason="모름", action="확인", deadlines=[], unknowns=[])
    return Analysis(**{**values, **overrides})


def test_parse_list():
    [item] = parse_list(LIST_HTML, BOARD)
    assert item.article_id == 378223
    assert item.title == "[국가근로] 신청 안내"
    assert item.date == "2026-10-29"
    assert item.department == "학생지원팀"
    assert item.attachments[0].name == "붙임1.pdf"
    assert item.attachments[0].url.endswith("?mode=download&articleNo=378223&attachNo=335758")


def test_list_signature_changes_with_attachment():
    [item] = parse_list(LIST_HTML, BOARD)
    before = item.signature
    item.attachments.pop()
    assert item.signature != before


def test_parse_view_keeps_only_school_images():
    detail = parse_view(VIEW_HTML, BOARD, 1)
    assert detail.body_text == "본문 내용"
    assert detail.image_urls == ["https://www.ajou.ac.kr/_attach/ajou/editor-image/a.png"]
    assert detail.skipped_images == ["https://upload.wikimedia.org/x.png"]
    assert detail.attachments[0].attach_no == "7"


def test_hwp_control_characters_are_skipped():
    inline = [ord("\x0b")] + [0] * 7
    raw = struct.pack(f"<{3 + 8 + 2}H", ord("표"), ord("안"), 13, *inline, ord("끝"), 13)
    assert _hwp_para_text(raw).split() == ["표안", "끝"]


def test_tall_poster_is_split_and_small_icon_dropped():
    buffer = io.BytesIO()
    Image.new("RGB", (800, 5000), "white").save(buffer, "PNG")
    tiles = prepare_image("poster", buffer.getvalue()).images
    assert len(tiles) >= 3
    for _, data in tiles:
        width, height = Image.open(io.BytesIO(data)).size
        assert max(width, height) <= 1568
    small = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(small, "PNG")
    assert prepare_image("icon", small.getvalue()).images == []


def test_corrupt_image_reports_failure():
    out = prepare_image("broken", b"not an image")
    assert out.images == [] and out.failures


def test_zip_extracts_in_memory_and_ignores_paths():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../evil/안내.txt", "내부 문구")
        archive.writestr("x.exe", b"MZ")
    out = extract_file("묶음.zip", buffer.getvalue())
    assert [text for _, text in out.texts] == ["내부 문구"]
    assert any("지원하지 않는" in failure for failure in out.failures)


def test_nested_zip_is_not_expanded():
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("a.txt", "x")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("inner.zip", inner.getvalue())
    out = extract_file("o.zip", outer.getvalue())
    assert out.texts == [] and out.failures


def test_unknown_format_is_reported_not_dropped():
    assert extract_file("자료.ppt", b"x").failures


def test_route_urgent_when_high_and_deadline_soon():
    soon = analysis(deadlines=[Deadline(label="마감", date="2026-11-05", time="", kind="deadline")])
    assert route(soon, date(2026, 11, 2)) == ("urgent", Tier.PING)


def test_route_medium_is_quiet_without_deadline_and_low_is_digest_only():
    assert route(analysis(relevance="medium", category="international"), date(2026, 11, 2)) == ("international", Tier.QUIET)
    assert route(analysis(relevance="low"), date(2026, 11, 2)) == (None, Tier.DIGEST)
    assert route(analysis(relevance="none"), date(2026, 11, 2)) == (None, Tier.DIGEST)


def test_route_medium_with_open_deadline_notifies():
    later = analysis(relevance="medium", category="career", deadlines=[Deadline(label="마감", date="2026-11-20", time="", kind="deadline")])
    assert route(later, date(2026, 11, 2)) == ("career", Tier.NOTIFY)


def test_route_past_deadline_is_not_urgent():
    past = analysis(deadlines=[Deadline(label="마감", date="2026-10-01", time="", kind="deadline")])
    assert route(past, date(2026, 11, 2)) == ("scholarship", Tier.NOTIFY)


def test_bad_date_from_model_does_not_crash():
    bad = analysis(deadlines=[Deadline(label="마감", date="곧", time="", kind="deadline")])
    assert route(bad, date(2026, 11, 2)) == ("scholarship", Tier.NOTIFY)
    assert "곧" in format_deadlines(bad, date(2026, 11, 2))


def test_embed_stays_within_discord_limits_and_shows_failures():
    item = ListItem(1, "general", BOARD.url, "제" * 400, "", "", "2026-11-02")
    detail = Detail("", [], [], [])
    embed = build_embed(item, detail, analysis(summary="가" * 5000, why_relevant="나" * 2000),
                        ["일부만 분석"], ["붙임.doc: 지원하지 않는 형식"], date(2026, 11, 2), True)
    assert len(embed["title"]) <= 256 and embed["title"].startswith("[수정됨]")
    assert len(embed["description"]) <= 4096
    assert all(len(field["value"]) <= 1024 for field in embed["fields"])
    assert any("다 읽지 못함" in field["name"] for field in embed["fields"])
    assert embed["url"] == item.url


def test_split_message_respects_limit():
    chunks = split_message("\n".join(["가" * 500] * 10))
    assert len(chunks) >= 2 and all(len(chunk) <= 1900 for chunk in chunks)


def test_content_marks_body_and_reports_truncation():
    item = ListItem(1, "general", BOARD.url, "제목", "", "", "2026-11-02")
    extraction = Extraction(texts=[("긴.pdf", "가" * 70000)], images=[("이미지", b"\xff\xd8")])
    content, notes = build_content(item, Detail("본문", [], [], []), extraction, date(2026, 11, 2))
    assert content[1]["type"] == "image"
    assert "<notice_body>" in content[-1]["text"]
    assert notes and "앞" in notes[0]


def test_seen_uses_latest_row_regardless_of_file_order(tmp_path):
    path = tmp_path / "seen.jsonl"
    path.write_text(
        json.dumps({"id": 1, "status": "done", "at": "2026-11-02T10:00:00"}) + "\n"
        + json.dumps({"id": 1, "status": "pending", "attempts": 1, "at": "2026-11-02T09:00:00"}) + "\n"
    )
    assert store.load_seen(path)[1]["status"] == "done"


def test_hwp_surrogate_pairs_merge_and_lone_surrogate_is_dropped():
    pair = "𠀀".encode("utf-16-le")
    lone = struct.pack("<H", 0xD840)
    assert _hwp_para_text(pair + "가".encode("utf-16-le") + lone) == "𠀀가"


def test_content_survives_broken_unicode():
    item = ListItem(1, "general", BOARD.url, "제목", "", "", "2026-11-02")
    content, _ = build_content(item, Detail("가\ud840나", [], [], []), Extraction(), date(2026, 11, 2))
    content[-1]["text"].encode("utf-8")


def test_transient_classification():
    import httpx
    from ajou_assistant.scrape import is_transient
    request = httpx.Request("GET", "https://example.com")
    assert is_transient(httpx.ReadTimeout("x", request=request))
    assert is_transient(httpx.HTTPStatusError("x", request=request, response=httpx.Response(502, request=request)))
    assert not is_transient(httpx.HTTPStatusError("x", request=request, response=httpx.Response(404, request=request)))
    assert not is_transient(ValueError("x"))


PROGRAM_HTML = """
<div class="box"><span><img src="x.jpg"></span><div class="info-box">
<div class="prod-cate">강의/특강/행사</div>
<div id="ctl01_Title_txt" class="prod1">서류 합격 특강 &amp; 컨설팅</div>
<div id="ctl01_eduArea" class="prod2"><span>교육기간</span><span class="text-000 bold">2026-11-04~2026-11-13</span></div>
</div></div>
<div class="hover-box" onclick="javascript:goView('abcDEF123')" style="cursor: pointer;"></div>"""

RECRUIT_HTML = """<table><tbody>
<tr id="hcTr1"><td>회사A</td><td><div onclick=openw('https://example.com/jobs/1')>2027년 공개채용</div></td><td>2026-11-11</td></tr>
<tr id="hcTr2"><td>회사B</td><td><div onclick=openw('https://example.com/jobs/1')>2027년 공개채용</div></td><td>2026-11-11</td></tr>
</tbody></table>"""


def test_parse_programs():
    board = Board("program", "비교과", "https://job.ajou.ac.kr/x", kind="program")
    [item] = parse_programs(PROGRAM_HTML, board)
    [again] = parse_programs(PROGRAM_HTML.replace("abcDEF123", "zzzOTHER9"), board)
    assert item.article_id == again.article_id and item.article_id.startswith("p:")
    assert item.title == "서류 합격 특강 & 컨설팅"
    assert item.date == "2026-11-04"
    assert item.url.endswith("programView.aspx?pgdx=abcDEF123")


def test_parse_recruits_merges_same_posting():
    [item] = parse_recruits(RECRUIT_HTML, Board("recruit", "채용", "https://job.ajou.ac.kr/y", kind="recruit"))
    assert item.department == "회사A, 회사B"
    assert item.url == "https://example.com/jobs/1"
    assert item.date == "2026-11-11"


def test_torn_line_does_not_break_loading(tmp_path):
    path = tmp_path / "seen.jsonl"
    store.append_seen({"id": 1, "status": "done"}, path)
    with path.open("a") as handle:
        handle.write('{"id": 2, "sta')  # 쓰는 중에 전원이 끊긴 줄
    store.append_seen({"id": 3, "status": "done"}, path)
    seen = store.load_seen(path)
    assert set(seen) == {1, 3}


def test_public_url_blocks_private_hosts():
    assert not is_public_url("http://192.168.0.1/a.png")
    assert not is_public_url("http://127.0.0.1/a.png")
    assert not is_public_url("file:///etc/passwd")


def test_fit_embed_stays_within_discord_total_limit():
    embed = {"title": "t" * 256, "description": "d" * 4000, "footer": {"text": "f"},
             "fields": [{"name": "n", "value": "v" * 1024} for _ in range(4)]}
    fitted = fit_embed(embed)
    total = len(fitted["title"]) + len(fitted["description"]) + 1 + sum(len(f["name"]) + len(f["value"]) for f in fitted["fields"])
    assert total <= 5900


def test_calendar_url_encodes_time_and_all_day():
    timed = calendar_url("마감", "2026-11-04", "17:00", "https://a.kr/1")
    assert "20261104T170000%2F20261104T180000" in timed
    allday = calendar_url("행사", "2026-11-30", "", "https://a.kr/1")
    assert "20261130%2F20261201" in allday
    assert calendar_url("x", "미정", "", "https://a.kr/1") is None


def test_components_fit_discord_limits():
    item = ListItem(378179, "scholarship", "https://www.ajou.ac.kr/x.do", "제목", "", "", "2026-11-01")
    deadlines = [Deadline(label=f"일정{i}", date="2026-11-20", time="", kind="deadline") for i in range(9)]
    rows = build_components(item, analysis(deadlines=deadlines), date(2026, 11, 2))
    assert all(len(row["components"]) <= 5 for row in rows)
    assert all(len(button.get("url", "")) <= 512 for row in rows for button in row["components"])


def test_ping_mentions_only_owner_and_quiet_is_silent():
    discord = Discord("t", {"channels": {"system": "1"}, "owner_id": "42"}, dry_run=True)
    sent = []
    discord.client = None
    import builtins
    original = builtins.print
    builtins.print = lambda *args: sent.append(" ".join(map(str, args)))
    try:
        discord.send("system", {"content": "@everyone 공지 문구"}, tier=Tier.PING)
        discord.send("system", {"content": "조용히"}, tier=Tier.QUIET)
    finally:
        builtins.print = original
    assert '"users": ["42"]' in sent[0] and '"parse": []' in sent[0] and "<@42>" in sent[0]
    assert '"flags": 4096' in sent[1]


def test_fault_classification():
    import anthropic
    import httpx
    from ajou_assistant.cli import fault_of
    from ajou_assistant.scrape import ScrapeError
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status(code, body):
        return anthropic.APIStatusError("x " + body, response=httpx.Response(code, request=request), body=None)

    assert fault_of(anthropic.APIConnectionError(request=request)) == "ai"
    assert fault_of(status(529, "overloaded")) == "ai"
    assert fault_of(status(400, "Your credit balance is too low")) == "ai"
    assert fault_of(status(400, "invalid image")) == "item"
    assert fault_of(DiscordError(503, "x")) == "discord"
    assert fault_of(DiscordError(400, "x")) == "item"
    assert fault_of(httpx.ConnectError("x")) == "school"
    assert fault_of(ScrapeError("x")) == "item"


def test_retry_never_gives_up_but_slows_down():
    from datetime import datetime, timedelta, timezone
    from ajou_assistant.cli import retry_due
    moment = datetime(2026, 11, 2, 12, tzinfo=timezone(timedelta(hours=9)))
    assert retry_due({"attempts": 4, "at": moment.isoformat()}, moment)
    assert not retry_due({"attempts": 50, "at": (moment - timedelta(hours=1)).isoformat()}, moment)
    assert retry_due({"attempts": 50, "at": (moment - timedelta(hours=7)).isoformat()}, moment)


def test_bot_tools_exclude_shell_and_repo_secrets():
    from ajou_assistant.bot import claude_command
    command = claude_command("outputs/threads/1", None)
    allowed = command[command.index("--allowedTools") + 1]
    denied = command[command.index("--disallowedTools") + 1]
    assert "Bash" not in allowed and "Bash" in denied and "WebFetch" in denied
    assert "Read(./.git/**)" in denied
    assert "Write(outputs/threads/1/**)" in allowed


def test_bridge_times_for_timed_and_all_day_events():
    from ajou_assistant.google_bridge import event_times
    start, end, all_day = event_times("2026-11-04", "17:00", 30)
    assert (start, end, all_day) == ("2026-11-04T17:00:00+09:00", "2026-11-04T17:30:00+09:00", False)
    start, end, all_day = event_times("2026-11-30", "", 30)
    assert all_day and start.startswith("2026-11-30T00:00") and end.startswith("2026-12-01")


def test_bot_replies_use_arguments_discord_accepts():
    """send(flags=...)처럼 discord.py가 받지 않는 인자는 답장을 조용히 삼킨다 (11/2 장애)."""
    import ast
    import inspect
    from pathlib import Path
    from discord.abc import Messageable
    from ajou_assistant import bot
    accepted = set(inspect.signature(Messageable.send).parameters)
    assert set(bot.quiet(silent=True)) <= accepted
    tree = ast.parse(Path(bot.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "send":
            names = {keyword.arg for keyword in node.keywords if keyword.arg}
            assert names <= accepted | {"ephemeral"}, names


def test_calendar_dates_pick_important_open_dates():
    from ajou_assistant.cli import calendar_dates
    today = date(2026, 11, 2)
    dates = [Deadline(label="접수", date="2026-11-10", time="", kind="start"),
             Deadline(label="마감", date="2026-11-20", time="17:00", kind="deadline"),
             Deadline(label="설명회", date="2026-11-12", time="19:00", kind="event"),
             Deadline(label="지난 마감", date="2026-10-20", time="", kind="deadline")]
    assert [d.label for d in calendar_dates(analysis(deadlines=dates), today)] == ["접수", "설명회", "마감"]
    medium = analysis(relevance="medium", eligibility="likely", deadlines=dates)
    assert [d.label for d in calendar_dates(medium, today)] == ["마감"]
    series = [Deadline(label=f"{n}회", date=f"2026-11-{10 + n}", time="19:00", kind="event") for n in range(8)]
    assert [d.label for d in calendar_dates(analysis(deadlines=series[::-1]), today)] == ["0회", "1회", "2회", "3회"]
    assert calendar_dates(analysis(relevance="medium", deadlines=dates), today) == []
    assert calendar_dates(analysis(eligibility="unlikely", deadlines=dates), today) == []


def test_components_link_to_events_already_in_calendar():
    from ajou_assistant.notify import calendar_key
    item = ListItem(1, "scholarship", "https://www.ajou.ac.kr/x.do", "제목", "", "", "2026-11-01")
    deadline = Deadline(label="마감", date="2026-11-20", time="17:00", kind="deadline")
    rows = build_components(item, analysis(deadlines=[deadline]), date(2026, 11, 2), "",
                            {calendar_key(deadline): "https://www.google.com/calendar/event?eid=abc"})
    buttons = rows[0]["components"]
    assert buttons[1]["label"].startswith("✅") and buttons[1]["url"].endswith("eid=abc")
    assert not any("TEMPLATE" in button.get("url", "") for button in buttons)


def test_record_log_keeps_raw_turns_and_tracks_offset(tmp_path):
    from ajou_assistant import record
    record.log_turn(tmp_path, "나", "특강 11/3, 10, 17 신청했어")
    text, end = record.unrecorded(tmp_path, 0)
    assert "· 나" in text and "17 신청" in text
    record.log_turn(tmp_path, "비서", "기록했습니다")
    rest, newer = record.unrecorded(tmp_path, end)
    assert "17 신청" not in rest and "기록했습니다" in rest and newer > end
    assert record.unrecorded(tmp_path / "없음", 0) == ("", 0)


def test_record_applies_notice_decisions_and_writes_month_file(tmp_path, monkeypatch):
    from datetime import datetime
    from ajou_assistant import record
    monkeypatch.setattr(store, "ACTIONS_FILE", tmp_path / "actions.jsonl")
    monkeypatch.setattr(record, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(record, "RECORD_DATA_DIR", tmp_path / "records" / "data")
    result = {
        "summary": "연속 특강 3회 신청",
        "decisions": [{"about": "연속 특강", "decision": "3회 신청", "notice_id": "377587", "status": "applied"},
                      {"about": "토익", "decision": "보류", "notice_id": "", "status": "undecided"}],
        "facts": [{"area": "국제교류", "fact": "동아리 행사에서 진행 담당을 맡음"}],
        "todos": [{"task": "줌 링크 확인", "due": "2026-11-03"}],
        "events": [], "questions": [],
    }
    lines = record.apply_notice_decisions(result)
    assert 377587 in store.closed_items() and len(lines) == 1
    path = record.write_record(result, "1", "특강 신청", "[log.md](x)", datetime(2026, 11, 2, 21, 0), lines)
    written = path.read_text()
    assert "연속 특강 → 3회 신청 (공지 377587)" in written and "기한 2026-11-03" in written
    assert (tmp_path / "records" / "data" / "2026-11.jsonl").exists()
    assert "기록했습니다" in record.discord_summary(result, lines)
    empty = {key: [] for key in ("decisions", "facts", "todos", "events", "questions")}
    assert record.discord_summary({"summary": "잡담", **empty}, []) == ""


def test_record_schema_requires_every_field():
    from ajou_assistant.record import SCHEMA
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    for name in ("decisions", "facts", "todos", "events"):
        item = SCHEMA["properties"][name]["items"]
        assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False


def test_existing_event_at_same_time_is_reused():
    from ajou_assistant.cli import already_in_calendar
    rows = [{"start": "2026-11-10T10:00:00.000Z", "allDay": False, "title": "온라인 특강"},
            {"start": "2026-11-10", "allDay": True, "title": "종일"}]
    assert already_in_calendar("2026-11-10", "19:00", rows)["title"] == "온라인 특강"
    assert already_in_calendar("2026-11-10", "20:00", rows) is None
    assert already_in_calendar("2026-11-10", "", rows) is None


def test_local_start_reads_utc_and_kst_alike():
    from ajou_assistant.google_bridge import local_start
    assert local_start({"start": "2026-11-03T10:00:00.000Z"}) == ("2026-11-03", "19:00")
    assert local_start({"start": "2026-11-03T19:00:00+09:00"}) == ("2026-11-03", "19:00")
    assert local_start({"start": "2026-11-03", "allDay": True}) is None


def test_forwarded_messages_are_read():
    from types import SimpleNamespace
    from ajou_assistant.bot import message_text, thread_name
    forwarded = SimpleNamespace(content="", message_snapshots=[SimpleNamespace(content="모임 목요일 19시로 확정")])
    assert "모임 목요일 19시" in message_text(forwarded)
    both = SimpleNamespace(content="이거 기록해 줘", message_snapshots=[SimpleNamespace(content="원문")])
    assert message_text(both).startswith("이거 기록해 줘") and "[전달된 메시지]" in message_text(both)
    assert thread_name("\n\n  [문자] 면접 안내\n본문") == "[문자] 면접 안내"


def test_school_images_follow_the_board_domain_not_a_fixed_school():
    assert site_domain("https://www.ajou.ac.kr/kr/ajou/notice.do") == "ajou.ac.kr"
    assert site_domain("https://oia.ajou.ac.kr/oia/notice/notice.do") == "ajou.ac.kr"
    assert site_domain("https://cs.example.edu/board") == "example.edu"
    other = Board("notice", "공지", "https://www.example-univ.ac.kr/board/notice.do")
    detail = parse_view(VIEW_HTML.replace("/_attach/ajou/editor-image/a.png", "https://cdn.example-univ.ac.kr/a.png"), other, 1)
    assert detail.image_urls == ["https://cdn.example-univ.ac.kr/a.png"]
    assert detail.skipped_images == ["https://upload.wikimedia.org/x.png"]
