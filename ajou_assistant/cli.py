"""실행 명령.

  ajou seed [--board 키]        현재 글을 본 것으로 표시 (AI 호출 없음)
  ajou backfill --days 14       최근 글을 한 번 분석 (옛 글까지 쪽을 넘겨 읽는다)
  ajou run                      새 글 수집·분석·알림 (주기 실행)
  ajou digest                   하루 요약 전송
  ajou check <게시판> <글번호>    글 하나를 분석만 해서 출력
  ajou reanalyze <글번호...>     새 정보로 다시 판단해 알림
  ajou mark <글번호> done|skip|open|track   마감 독촉을 끄거나 켠다. track은 관련도와 상관없이 계속 추적
  ajou ignore <글번호>           읽을 수 없는 글을 미처리 목록에서 뺀다
  ajou tier-test                알림 단계별 시험 메시지 전송
  ajou style                    Discord 채널 이름·아이콘 적용
  ajou google-check             학교 계정 연결 확인
  ajou calendar-add 제목 날짜 [시각] [--minutes 분] [--notes 메모]
  ajou link-reposts [--dry-run]  이미 처리한 공지에서 재공지를 찾아 처음 공지에 묶는다 (repost.py)
  ajou schedule                 run(매시 :07)·digest(21:30)·weekly(일 21:00)를 계속 돌린다 (도커용)

수집 원칙: 글은 완료로 기록되기 전까지 계속 다시 시도한다. 학교·AI·Discord 장애는 글 탓이
아니므로 시도 횟수에 넣지 않는다. 전송 뒤 기록 전에 꺼지면 다시 보내므로 중복은 있어도 누락은 없다.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import os
import subprocess
import sys
import socket
import traceback
from datetime import date, datetime, timedelta

import anthropic
import httpx

from . import english, repost, store
from .config import (
    BOARDS, HEARTBEAT_DIR, LIST_PAGE_SIZE, SESSIONS_FILE, STATE_DIR, Board, SetupError, load_discord, load_secrets, setting,
)
from .extract import Extraction, extract_file, prepare_image
from .notify import (
    SUPPRESS_EMBEDS, Discord, DiscordError, Tier, _clip, ask_dates, build_components, build_embed, calendar_key, route,
    upload_images,
)
from .scrape import Detail, Fetcher, ListItem, ScrapeError, is_public_url, is_transient
from .sync import sync_all, sync_pull, sync_push
from .triage import Analysis, TriageError, TriageResult, build_content, load_profile, prefilter, triage

BOARD_BY_KEY = {board.key: board for board in BOARDS}
RETRY_EVERY_RUN = 5   # 처음 다섯 번은 매 실행마다, 그 뒤로는 6시간마다 다시 시도한다
RETRY_SLOW_HOURS = 6
MAX_TRANSIENT = 6     # 첨부만 계속 실패하면 받은 만큼으로 먼저 알리고 하루 한 번 다시 읽는다
PARTIAL_RETRY_DAYS = 14
REMINDER_DAYS = (3, 1, 0)
DEADLINE_HORIZON = 14
DUPLICATE_MIN_BODY = 100
STANDBY_HOURS = float(setting("AJOU_STANDBY_HOURS", "6"))
PRICES = {  # 달러/백만 토큰 (입력, 출력, 캐시 읽기, 캐시 쓰기)
    "claude-opus-5-5": (4.0, 20.0, 0.20, 8.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 4.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.20),
}


def cost(usage: dict, model: str) -> float:
    prices = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (
        usage["input"] * prices[0] + usage["output"] * prices[1]
        + usage["cache_read"] * prices[2] + usage["cache_write"] * prices[3]
    ) / 1_000_000


def fault_of(error: BaseException) -> str:
    """실패의 원인을 가른다. school·ai·discord는 글 탓이 아니므로 시도 횟수에 넣지 않는다."""
    if isinstance(error, DiscordError):
        return "discord" if error.is_outage else "item"
    if isinstance(error, anthropic.APIConnectionError):
        return "ai"
    if isinstance(error, anthropic.APIStatusError):
        if error.status_code in (401, 403, 429) or error.status_code >= 500 or "credit" in str(error).lower():
            return "ai"
        return "item"
    return "school" if is_transient(error) else "item"


def retry_due(row: dict, moment: datetime) -> bool:
    if row.get("attempts", 0) < RETRY_EVERY_RUN:
        return True
    try:
        return moment - datetime.fromisoformat(row["at"]) >= timedelta(hours=RETRY_SLOW_HOURS)
    except (KeyError, ValueError):
        return True


def collect_lists(fetcher: Fetcher, since: str | None = None) -> tuple[dict[int | str, ListItem], list[str]]:
    """게시판을 읽는다. 0건은 '새 글 없음'이 아니라 수집 실패로 취급한다.

    오래 꺼져 있었으면 이미 본 글이 나올 때까지 쪽을 넘긴다. since를 주면 그 날짜까지 읽는다.
    """
    items: dict[int | str, ListItem] = {}
    failures: list[str] = []
    seen = store.load_seen()
    known = {row.get("board") for row in seen.values()}

    def needs_more(rows: list[ListItem], board: Board) -> bool:
        if since:
            return bool(rows[-1].date) and rows[-1].date >= since
        return board.key in known and all(row.article_id not in seen for row in rows[-5:])

    for board in BOARDS:
        try:
            rows = fetcher.list_page(board, 0, LIST_PAGE_SIZE)
            if not rows:
                raise ScrapeError("목록에서 글을 찾지 못함 (사이트 구조 변경 가능성)")
            page = 1
            while page < board.pages and needs_more(rows, board):
                more = fetcher.list_page(board, page * LIST_PAGE_SIZE, LIST_PAGE_SIZE)
                if not more:
                    break
                rows += more
                page += 1
            else:
                if page >= board.pages and needs_more(rows, board) and board.kind == "cms":
                    failures.append(f"{board.name}: {board.pages}쪽까지 읽어도 이전 기록에 닿지 못함")
            for row in rows:
                current = items.get(row.article_id)
                if current is None or (not current.attachments and row.attachments):
                    items[row.article_id] = row
        except (httpx.HTTPError, ScrapeError) as error:
            failures.append(f"{board.name}: {error}")
    return items, failures


def gather(fetcher: Fetcher, item: ListItem, detail: Detail, allow_partial: bool = False) -> tuple[Extraction, list[tuple[str, bytes]]]:
    """본문 이미지와 첨부를 내려받아 읽는다. 원본 이미지는 Discord 전송용으로 따로 돌려준다.

    학교 서버 장애로 일부만 받은 채 분석하면 완료로 처리되어 내용을 놓치므로, 일시 장애는 위로
    올려 글 전체를 나중에 다시 처리한다. 여러 번 실패한 뒤에만 받은 만큼으로 분석한다.
    """
    extraction = Extraction()
    originals: list[tuple[str, bytes]] = []
    external = [url for url in detail.skipped_images if is_public_url(url)]
    for index, url in enumerate([*detail.image_urls, *external], start=1):
        name = f"본문 이미지 {index}"
        try:
            data = fetcher.bytes(url)
        except (httpx.HTTPError, ScrapeError) as error:
            if is_transient(error) and not allow_partial:
                raise
            extraction.failures.append(f"{name}: 내려받기 실패 ({error})")
            continue
        prepared = prepare_image(name, data)
        extraction.merge(prepared)
        originals += prepared.images
    blocked = len(detail.skipped_images) - len(external)
    if blocked:
        extraction.truncated.append(f"외부 이미지 {blocked}장은 내부 주소라 내려받지 않음")
    attachments = {a.attach_no: a for a in [*item.attachments, *detail.attachments]}
    detail.attachments = list(attachments.values())
    for attachment in detail.attachments:
        try:
            data = fetcher.bytes(attachment.url)
            extraction.merge(extract_file(attachment.name, data))
        except (httpx.HTTPError, ScrapeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            if is_transient(error) and not allow_partial:
                raise
            extraction.failures.append(f"{attachment.name}: 읽기 실패 ({error})")
    extraction.cap_images()
    return extraction, originals


def analyze(fetcher: Fetcher, client: anthropic.Anthropic, item: ListItem, today: date, effort: str = "low", allow_partial: bool = False, detail: Detail | None = None):
    board = BOARD_BY_KEY[item.board_key]
    detail = detail or fetcher.detail(item, board)
    extraction, originals = gather(fetcher, item, detail, allow_partial)
    content, notes = build_content(item, detail, extraction, today)
    result = triage(client, content, effort, board.model)
    return detail, extraction, originals, notes, result


def record(item: ListItem, detail: Detail, result: TriageResult, extraction: Extraction, notes: list[str], channel: str | None, message_id: str | None, changed, backfill: bool) -> None:
    store.append_analysis({
        "id": item.article_id,
        "board": item.board_key,
        "title": item.title,
        "url": item.url,
        "date": item.date,
        "analyzed_at": store.now().isoformat(timespec="seconds"),
        "changed": bool(changed),
        "backfill": backfill,
        "channel": channel,
        "message_id": message_id,
        "model": result.model,
        "usage": result.usage,
        "images": len(extraction.images),
        "failures": extraction.failures,
        "notes": notes,
        "analysis": result.analysis.model_dump(),
    })


def deliver(discord: Discord, channel: str, tier: Tier, item: ListItem, detail: Detail, analysis: Analysis, notes, failures, today, changed, originals,
            calendar_links: dict[str, str] | None = None) -> str | None:
    account = load_discord().get("calendar_account", "")
    payload = {
        "embeds": [build_embed(item, detail, analysis, notes, failures, today, changed)],
        "components": build_components(item, analysis, today, account, calendar_links, can_add=calendar_ready()),
    }
    try:
        return discord.send(channel, payload, upload_images(originals), tier)
    except DiscordError as error:
        if error.is_outage:
            raise
    # 메시지 형식이 거절되면 제목과 링크만이라도 보낸다. 알림이 사라지는 것보다 낫다.
    return discord.send(channel, {"content": f"**{_clip(item.title, 200)}**\n<{item.url}>\n상세 전송에 실패했습니다. 원문을 확인하세요."}, tier=tier)


def notice_threads(members: set) -> list[str]:
    """이 공지 묶음으로 열린, 닫지 않은 Discord 스레드. 봇의 상태 파일을 읽기만 한다."""
    try:
        sessions = json.loads(SESSIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    wanted = {str(member) for member in members}
    # 닫은(완료한) 스레드에 쓰면 다시 열려 사이드바로 돌아온다. 재공지는 그 스레드 대신 처음 알림의 답장으로 간다.
    return [key for key, state in sessions.items() if str(state.get("notice", "")) in wanted and not state.get("done")]


def tell_threads(discord: Discord, root, text: str, tier: Tier) -> bool:
    """처음 공지의 스레드에 한 줄을 올린다. 봇이 이 줄을 다음 턴의 자료로 넘긴다(bot.note_repost)."""
    threads = notice_threads(repost.group_ids(root))
    for thread in threads:
        try:
            discord.send(thread, {"content": text, "flags": SUPPRESS_EMBEDS}, tier=tier)
        except DiscordError as error:
            if error.is_outage:
                raise
            print(f"스레드 알림 실패 {thread}: {error}")
    return bool(threads)


def find_repost(client, item: ListItem, analysis: Analysis) -> tuple[dict | None, float]:
    """이 공지가 이전 공지를 되풀이한 것이면 {"parent", "root", "changes"}. 판단이 실패하면 새 공지로 둔다."""
    earlier = repost.candidates(item.article_id, item.board_key, item.title, item.date, store.recent_analysis())
    if not earlier:
        return None, 0.0
    row = {"id": item.article_id, "title": item.title, "date": item.date, "analysis": analysis.model_dump()}
    try:
        verdicts, usage, model = repost.judge(client, [(row, earlier)])
    except Exception as error:  # 묶지 못하면 알림이 하나 더 갈 뿐 놓치지는 않는다.
        print(f"재공지 판단 실패, 새 공지로 처리 {item.article_id}: {type(error).__name__}: {str(error)[:120]}")
        return None, 0.0
    verdict = verdicts.get(str(item.article_id))
    spent = cost(usage, model)
    if verdict is None:
        return None, spent
    return {"parent": verdict["same_as"], "root": repost.root_of(verdict["same_as"]), "changes": verdict["changes"]}, spent


def first_alert(members: set) -> dict | None:
    """묶음에서 Discord로 알림을 보낸 가장 이른 분석."""
    rows = [row for row in store.recent_analysis() if row["id"] in members and row.get("message_id") and row.get("channel")]
    return min(rows, key=lambda row: str(row.get("analyzed_at", ""))) if rows else None


def deliver_repost(discord: Discord, item: ListItem, link: dict) -> tuple[str | None, str | None] | None:
    """재공지를 알린다. 스레드가 있으면 스레드에, 없고 중요한 차이가 있으면 처음 알림의 답장으로 한 줄.

    (채널, 메시지 ID)를 돌려준다. 처음 공지가 알림 없이 끝났으면 None을 돌려 평소대로 알리게 한다.
    """
    members = repost.group_ids(link["root"])
    text = repost.note(item.title, item.url, item.article_id, link["changes"])
    tier = Tier.NOTIFY if link["changes"] else Tier.QUIET
    if tell_threads(discord, link["root"], text, tier):
        return None, None
    first = first_alert(members)
    if first is None:
        return None
    if not link["changes"] or members & store.closed_items():
        return None, None
    payload = {"content": text, "flags": SUPPRESS_EMBEDS,
               "message_reference": {"message_id": first["message_id"], "fail_if_not_exists": False}}
    return first["channel"], discord.send(first["channel"], payload, tier=tier)


def handle(item, fetcher, client, discord, today, changed=False, backfill=False, allow_partial=False, hashes: dict | None = None) -> dict:
    board = BOARD_BY_KEY[item.board_key]
    detail = fetcher.detail(item, board)
    original = (hashes or {}).get(detail.content_hash)
    long_enough = len(detail.body_text) >= DUPLICATE_MIN_BODY or detail.image_urls
    if original is not None and original != item.article_id and long_enough and not changed:
        tell_threads(discord, repost.root_of(original), repost.note(item.title, item.url, item.article_id, ""), Tier.QUIET)
        return {"hash": detail.content_hash, "status": "done", "relevance": None, "cost": 0.0, "channel": None, "duplicate_of": original}
    detail, extraction, originals, notes, result = analyze(fetcher, client, item, today, allow_partial=allow_partial, detail=detail)
    spent = cost(result.usage, result.model)
    link = None
    if not changed and not backfill:  # 같은 글을 고친 것(changed)은 재공지가 아니다.
        link, extra = find_repost(client, item, result.analysis)
        spent += extra
    delivered = deliver_repost(discord, item, link) if link else None
    if delivered is not None:
        channel, message_id = delivered
        if not discord.dry_run:
            store.add_repost(item.article_id, link["parent"], link["changes"], "run")
            if not members_closed(link["root"]):  # 마감이 바뀌었으면 새 날짜만 더한다. 같은 날짜는 묶음 번호로 한 번만.
                add_to_calendar(item, result.analysis, today, link["root"])
    else:
        if link and not discord.dry_run:  # 처음 공지가 알림 없이 끝났던 글. 묶음은 남기고 평소대로 알린다.
            store.add_repost(item.article_id, link["parent"], link["changes"], "run")
        channel, tier = route(result.analysis, today)
        if backfill:  # 옛 글은 아직 신청할 수 있는 것만 조용히 올린다.
            tier = Tier.QUIET
            if not any(deadline.date >= today.isoformat() for deadline in result.analysis.deadlines):
                channel = None
        message_id = None
        # 캘린더에 먼저 넣어야 알림 버튼이 템플릿 화면 대신 실제 일정으로 간다.
        root = link["root"] if link else item.article_id
        calendar_links = add_to_calendar(item, result.analysis, today, root) if not discord.dry_run else {}
        if channel:
            message_id = deliver(discord, channel, tier, item, detail, result.analysis, notes, extraction.failures, today, changed, originals, calendar_links)
    if not discord.dry_run:  # 시험 실행이 처리 기록을 남기면 서버와 기록이 갈라진다.
        record(item, detail, result, extraction, notes, channel, message_id, changed, backfill)
    outcome = {"hash": detail.content_hash, "status": "done", "relevance": result.analysis.relevance,
               "cost": spent, "channel": channel, "partial": len(extraction.failures)}
    if link:
        outcome["repost_of"] = link["root"]
    return outcome


def members_closed(root) -> bool:
    return bool(repost.group_ids(root) & store.closed_items())


def alert_state(key: str, failed: bool, message: str, discord: Discord) -> None:
    """같은 장애를 매시간 반복해서 알리지 않고, 시작과 복구만 알린다. 알림 실패가 수집을 막지 않는다."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"alert-{key}"
    try:
        if failed and not path.exists():
            discord.text("system", f"🔴 {message}")
            path.touch()
        elif not failed and path.exists():
            discord.text("system", f"🟢 복구됨: {key}")
            path.unlink()
    except DiscordError as error:
        print(f"장애 알림 실패: {error}")


def preflight() -> None:
    """AI를 부르는 명령은 시작 전에 프로필과 Discord 설정을 확인한다. 글마다 같은 설정 오류가 반복되지 않게 한다."""
    load_profile()
    load_discord()
    load_secrets()["ANTHROPIC_API_KEY"]


def make_clients(dry_run: bool) -> tuple[anthropic.Anthropic, Discord]:
    secrets = load_secrets()
    client = anthropic.Anthropic(api_key=secrets["ANTHROPIC_API_KEY"], timeout=180, max_retries=3)
    return client, Discord(secrets["DISCORD_BOT_TOKEN"], load_discord(), dry_run)


def primary_is_alive() -> bool:
    """예비 호스트용. 다른 호스트가 최근에 돌았으면 맡기고, 끊겼으면 이어받는다."""
    host = socket.gethostname().split(".")[0]
    newest = None
    for path in HEARTBEAT_DIR.glob("*.json"):
        if path.stem == host:
            continue
        last_run = store.read_heartbeat(path).get("last_run")
        if not last_run:
            continue
        moment = datetime.fromisoformat(last_run)
        newest = moment if newest is None or moment > newest else newest
    return newest is not None and store.now() - newest < timedelta(hours=STANDBY_HOURS)


def cmd_seed(args) -> int:
    fetcher = Fetcher()
    items, failures = collect_lists(fetcher)
    seen = store.load_seen()
    added = 0
    for item in items.values():
        if args.board and item.board_key != args.board:
            continue
        if item.article_id not in seen:
            store.append_seen({"id": item.article_id, "board": item.board_key, "title": item.title, "link": item.link,
                               "date": item.date, "sig": item.signature, "status": "seeded"})
            added += 1
    print(f"표시 {added}건, 실패 {failures}")
    return 1 if failures else 0


ASK_REMINDER_DAYS = (2, 0)


def send_ask_reminders(discord: Discord, row: dict, analysis: Analysis, today: date, sent: set) -> int:
    """남에게 부탁할 서류는 '부탁할 날' 이틀 전과 당일에 알린다. 이미 지났으면 한 번만 바로 알린다."""
    count = 0
    for prep, ask in ask_dates(analysis, today):
        left = (ask - today).days
        if left not in ASK_REMINDER_DAYS and left >= 0:
            continue
        key = f"{row['id']}:ask:{prep.document}:{left if left >= 0 else 'late'}"
        if key in sent or (left < 0 and any(item.startswith(f"{row['id']}:ask:{prep.document}:") for item in sent)):
            continue
        when = "이미 늦었습니다. 오늘 바로" if left < 0 else "오늘" if left == 0 else f"{ask.month}/{ask.day}까지"
        discord.send("urgent", {
            "content": f"🤝 **부탁 시작** {row['title'][:120]}\n{prep.document}: {prep.who}께 {when} 부탁하세요 "
                       f"(받기까지 약 {prep.lead_days}일)\n<{row['url']}>",
        }, tier=Tier.PING)
        if not discord.dry_run:
            store.mark_reminded(key)
            sent.add(key)
        count += 1
    return count


def send_reminders(discord: Discord, today: date) -> int:
    """관련도 높은 공지의 마감을 D-3, D-1, 당일 아침에 강한 알림으로 다시 알린다. 완료·패스하면 멈춘다."""
    if not 9 <= store.now().hour < 22:
        return 0
    closed = store.closed_items()
    tracked = store.tracked_items()
    sent = store.reminded()
    account = load_discord().get("calendar_account", "")
    google_ready = calendar_ready()
    count = 0
    for group in store.notice_groups():  # 재공지는 처음 공지 번호로 한 번만 독촉한다.
        analysis = Analysis.model_validate(group["analysis"])
        if group["members"] & tracked:  # 사용자가 추적하라고 한 공지는 판단과 상관없이 높음으로 다룬다.
            analysis = analysis.model_copy(update={"relevance": "high", "eligibility": "likely"})
        if group["members"] & closed or analysis.eligibility == "unlikely":
            continue
        row = {**group, "id": group["root"]}
        if analysis.relevance == "high" or (analysis.relevance == "medium" and analysis.eligibility == "likely"):
            count += send_ask_reminders(discord, row, analysis, today, sent)
        if analysis.relevance != "high":
            continue
        for deadline in analysis.deadlines:
            try:
                left = (date.fromisoformat(deadline.date) - today).days
            except ValueError:
                continue
            key = f"{row['id']}:{deadline.date}:{left}"
            if deadline.kind != "deadline" or left not in REMINDER_DAYS or key in sent:
                continue
            label = "오늘 마감" if left == 0 else f"D-{left}"
            item = ListItem(row["id"], row["board"], "", row["title"], "", "", row.get("date", ""), link=row["url"])
            in_calendar = calendar_links_for(group["root"], analysis, today) if google_ready else {}
            discord.send("urgent", {
                "content": f"⏰ **{label}** {row['title'][:120]}\n{deadline.label}: {deadline.date} {deadline.time}".strip(),
                "components": build_components(item, analysis, today, account, in_calendar, can_add=google_ready),
            }, tier=Tier.PING)
            if not discord.dry_run:
                store.mark_reminded(key)
            count += 1
    return count


def cmd_run(args) -> int:
    preflight()
    moment = store.now()
    today = moment.date()
    client, discord = make_clients(args.dry_run)
    if args.standby:
        sync_error = sync_all(f"sync: {socket.gethostname().split('.')[0]} {store.now():%m-%d %H:%M}")
        alert_state("git-sync", bool(sync_error), f"예비 호스트 동기화 실패, 직접 확인 필요: {sync_error}", discord)
        alive = primary_is_alive()
        alert_state("primary-down", not alive, f"주 호스트 기록이 {STANDBY_HOURS:g}시간 넘게 끊겨 이 컴퓨터가 이어받습니다.", discord)
        if alive:
            print("주 호스트가 동작 중이라 대기합니다.")
            return 0
    if not args.dry_run and not args.standby:
        pull_error = sync_pull()
        alert_state("git-pull", bool(pull_error), f"git pull 실패: {pull_error}", discord)
    fetcher = Fetcher()
    items, failures = collect_lists(fetcher)
    alert_state("collect", bool(failures), "공지 수집 실패: " + "; ".join(failures), discord)
    seen = store.load_seen()
    hashes = {row["hash"]: key for key, row in seen.items() if row.get("hash") and row.get("status") == "done"}
    new, changed, retry = [], [], []
    for item in items.values():
        row = seen.get(item.article_id)
        if row is None:
            new.append(item)
        elif row.get("status") == "pending":
            if retry_due(row, moment):
                retry.append(item)
        elif row.get("sig") != item.signature:
            changed.append(item)
    for article_id, row in seen.items():
        # 목록에서 밀려난 미처리 글도 포기하지 않는다.
        if row.get("status") == "pending" and article_id not in items and retry_due(row, moment):
            board = BOARD_BY_KEY[row["board"]]
            retry.append(ListItem(article_id, board.key, board.url, row["title"], "", "", row.get("date", ""), link=row.get("link", "")))
    spent = 0.0
    done = 0
    ai_errors: list[str] = []
    school_down = 0
    outage: dict[str, str] = {}
    for item, is_changed in [*((i, False) for i in new + retry), *((i, True) for i in changed)]:
        previous = seen.get(item.article_id, {})
        signature = item.signature if item.article_id in items else previous.get("sig", item.signature)
        base = {"id": item.article_id, "board": item.board_key, "title": item.title, "link": item.link,
                "date": item.date, "sig": signature}
        transient_tries = previous.get("transient", 0)
        try:
            outcome = handle(item, fetcher, client, discord, today, changed=is_changed,
                             allow_partial=transient_tries >= MAX_TRANSIENT, hashes=hashes)
        except Exception as error:  # 글 하나의 문제로 나머지 글까지 멈추지 않는다.
            fault = fault_of(error)
            detail = f"{type(error).__name__}: {str(error)[:160]}"
            print(f"실패[{fault}] {str(item.title)[:40]}: {detail}")
            if fault in ("ai", "discord"):
                outage[fault] = detail
                break
            if fault == "school":
                school_down += 1
                if not args.dry_run:
                    store.append_seen({**base, "status": "pending", "attempts": previous.get("attempts", 0),
                                       "transient": transient_tries + 1})
                if school_down >= 3:
                    break
                continue
            attempts = previous.get("attempts", 0) + 1
            ai_errors.append(f"{item.title[:40]}: {detail}")
            if not args.dry_run:
                store.append_seen({**base, "status": "pending", "attempts": attempts, "error": detail})
                if attempts in (1, RETRY_EVERY_RUN):
                    state = "다시 시도합니다" if attempts == 1 else f"{RETRY_SLOW_HOURS}시간마다 계속 시도합니다. 원문을 직접 확인하세요"
                    try:
                        discord.text("general", f"⚠️ 공지 처리 실패, {state}: [{item.title}](<{item.url}>)", tier=Tier.PING)
                    except DiscordError:
                        pass
            continue
        school_down = 0
        spent += outcome["cost"]
        done += 1
        if outcome.get("hash"):
            hashes.setdefault(outcome["hash"], item.article_id)
        if not args.dry_run:
            store.append_seen({**base, "hash": outcome["hash"], "status": "done", "relevance": outcome["relevance"],
                               "attempts": 0, "transient": 0, "partial": outcome.get("partial", 0),
                               **({"duplicate_of": outcome["duplicate_of"]} if "duplicate_of" in outcome else {})})
    alert_state("school", school_down >= 3, "학교 사이트 응답 불안정: 남은 공지는 다음 실행에서 처리합니다.", discord)
    alert_state("analysis", bool(ai_errors), "공지 처리 실패: " + "; ".join(ai_errors[:3]), discord)
    alert_state("ai", "ai" in outage, f"AI 호출 장애, 복구되면 이어서 처리합니다: {outage.get('ai', '')}", discord)
    alert_state("discord", "discord" in outage, f"Discord 전송 장애: {outage.get('discord', '')}", discord)
    reminders = 0
    if "discord" not in outage:
        try:
            reminders = send_reminders(discord, today)
        except DiscordError as error:
            print(f"마감 알림 실패: {error}")
    if spent:  # 시험 실행도 실제로 돈이 나가므로 비용은 남긴다.
        store.add_usage("triage-dry" if args.dry_run else "triage", spent)
    if not args.dry_run:
        store.write_heartbeat({"new": done, "failures": len(failures), "ai_errors": len(ai_errors)})
        message = f"auto: 공지 {done}건 처리"
        push_error = sync_all(message) if args.standby else sync_push(message)
        alert_state("git-push", bool(push_error), f"git push 실패: {push_error}", discord)
    print(f"새 글 {len(new)}, 수정 {len(changed)}, 재시도 {len(retry)}, 처리 {done}, 오류 {len(ai_errors)}, 장애 {sorted(outage)}, 독촉 {reminders}, 비용 ${spent:.3f}")
    return 1 if failures or ai_errors or outage else 0


def cmd_backfill(args) -> int:
    preflight()
    today = store.now().date()
    cutoff = (today - timedelta(days=args.days)).isoformat()
    client, discord = make_clients(args.dry_run)
    fetcher = Fetcher()
    items, failures = collect_lists(fetcher, since=cutoff)
    seen = store.load_seen()
    targets = [
        item for item in items.values()
        if item.date >= cutoff and seen.get(item.article_id, {}).get("status") in (None, "seeded")
        and (not args.board or item.board_key == args.board)
    ]
    targets.sort(key=lambda item: str(item.article_id))
    if args.prefilter:
        kept: set[int] = set()
        for start in range(0, len(targets), 300):
            part = targets[start:start + 300]
            titles = [f"[{item.board_key} {item.date}] {item.title}" for item in part]
            kept |= {start + index for index in prefilter(client, titles, today)}
        for index, item in enumerate(targets):
            if index not in kept and not args.dry_run and not args.count:
                store.append_seen({"id": item.article_id, "board": item.board_key, "title": item.title, "link": item.link,
                                   "date": item.date, "sig": item.signature, "status": "seeded", "prefiltered": True})
        print(f"제목 거르기: {len(targets)}건 중 {len(kept)}건 남김")
        targets = [item for index, item in enumerate(targets) if index in kept]
    if args.limit:
        targets = targets[: args.limit]
    print(f"대상 {len(targets)}건, 실패 {failures}")
    if args.count:
        return 0
    spent = 0.0
    hashes = {row["hash"]: key for key, row in seen.items() if row.get("hash") and row.get("status") == "done"}
    for item in targets:
        base = {"id": item.article_id, "board": item.board_key, "title": item.title, "link": item.link,
                "date": item.date, "sig": item.signature}
        try:
            outcome = handle(item, fetcher, client, discord, today, backfill=True, hashes=hashes)
        except Exception as error:
            print(f"실패 {item.article_id} {item.title[:40]}: {type(error).__name__}: {str(error)[:160]}")
            if fault_of(error) in ("ai", "discord"):
                print("장애로 중단합니다. 남은 글은 다시 실행하면 이어서 처리합니다.")
                break
            if not args.dry_run:
                store.append_seen({**base, "status": "pending", "attempts": 1, "error": str(error)[:160]})
            continue
        spent += outcome["cost"]
        print(f"{str(outcome['relevance']):<7}{str(outcome['channel']):<14}${outcome['cost']:.3f} {item.title[:50]}")
        if not args.dry_run:
            store.append_seen({**base, "hash": outcome["hash"], "status": "done", "relevance": outcome["relevance"],
                               "attempts": 0, "partial": outcome.get("partial", 0)})
    if spent:
        store.add_usage("backfill-dry" if args.dry_run else "backfill", spent)
    print(f"총 비용 ${spent:.3f}")
    return 0


def cmd_reanalyze(args) -> int:
    """프로필이 바뀌었을 때 지난 공지를 다시 판단한다. 판단이 바뀐 것만 알린다."""
    preflight()
    today = store.now().date()
    client, discord = make_clients(args.dry_run)
    fetcher = Fetcher()
    rows = {row["id"]: row for row in store.recent_analysis()}
    spent = 0.0
    for raw in args.articles:
        key = int(raw) if raw.isdigit() else raw
        row = rows.get(key)
        if row is None:
            print(f"기록 없음: {key}")
            continue
        board = BOARD_BY_KEY[row["board"]]
        item = ListItem(key, row["board"], board.url, row["title"], "", "", row.get("date", ""),
                        link=row["url"] if board.kind != "cms" else "")
        detail, extraction, originals, notes, result = analyze(fetcher, client, item, today, detail=None)
        spent += cost(result.usage, result.model)
        before, after = row["analysis"], result.analysis
        moved = (before["relevance"], before["eligibility"]) != (after.relevance, after.eligibility)
        print(f"{key} {before['relevance']}/{before['eligibility']} → {after.relevance}/{after.eligibility} {row['title'][:50]}")
        channel, tier = route(after, today)
        message_id = None
        if moved and channel:
            message_id = deliver(discord, channel, tier, item, detail, after, notes, extraction.failures, today, "[재판단] ", originals)
        if not message_id:  # 다시 보내지 않았으면 처음 알림을 그대로 가리킨다. 답장·스레드·재공지 답장이 이 번호로 공지를 찾는다.
            channel, message_id = row.get("channel"), row.get("message_id")
        if not args.dry_run:
            record(item, detail, result, extraction, notes, channel, message_id, "reanalyze", False)
    if spent:
        store.add_usage("reanalyze-dry" if args.dry_run else "reanalyze", spent)
    if spent and not args.dry_run:
        sync_push(f"auto: 공지 {len(args.articles)}건 재판단")
    print(f"비용 ${spent:.3f}")
    return 0


def cmd_mark(args) -> int:
    key = int(args.article) if args.article.isdigit() else args.article
    store.mark_item(key, args.action)
    sync_push(f"auto: {key} {args.action}")
    print(f"{key}: {args.action}")
    pin_alert(key, args.action == "track")
    return 0


def pin_alert(key, on: bool) -> None:
    """추적은 핀이다. 추적하면 묶음의 처음 알림을 핀해 #📌핀에 모으고, 다른 표시로 바꾸면 핀을 푼다."""
    alert = first_alert(repost.group_ids(repost.root_of(key)))
    if alert is None:
        return
    try:
        _, discord = make_clients(False)
        discord.pin(alert["channel"], alert["message_id"], on)
        print("알림 핀" if on else "알림 핀 해제")
    except (DiscordError, KeyError, OSError) as error:  # 표시는 이미 저장됐다. 핀은 덤이다.
        print(f"핀 {'실패' if on else '해제 실패'}: {error}")


def cmd_ignore(args) -> int:
    key = int(args.article) if args.article.isdigit() else args.article
    row = store.load_seen().get(key)
    if row is None:
        print("기록 없음")
        return 1
    store.append_seen({**{k: v for k, v in row.items() if k != "at"}, "status": "done", "ignored": True})
    sync_push(f"auto: {key} 처리 제외")
    print(f"처리 제외: {row['title']}")
    return 0


def cmd_tier_test(args) -> int:
    _, discord = make_clients(args.dry_run)
    discord.text("system", "시험 1/3: 🔴 강한 알림입니다. 휴대폰 푸시와 빨간 배지가 떠야 합니다.", tier=Tier.PING)
    discord.text("system", "시험 2/3: 🟠 일반 알림입니다. 휴대폰 푸시는 뜨고 빨간 배지는 없습니다.", tier=Tier.NOTIFY)
    discord.text("system", "시험 3/3: ⚪ 조용한 기록입니다. 휴대폰 푸시 없이 채널에만 남아야 합니다.", tier=Tier.QUIET)
    print("전송 완료")
    return 0


def cmd_google_check(args) -> int:
    from . import google_bridge
    info = google_bridge.call("ping")
    version = info.get("version", 1)
    print(f"연결됨: {info['account']}, 캘린더 {info['calendar']}, 스크립트 {version}판")
    if version < 2:
        print("참석 응답(예/아니요/미정) 기능을 쓰려면 setup/update-google.sh 를 실행하세요.")
    return 0


def cmd_calendar_add(args) -> int:
    from . import google_bridge
    key = f"manual:{args.date}:{args.title}"[:120]
    result = google_bridge.add_event(key, args.title, args.date, args.time, args.notes, args.minutes)
    print("등록" if result["created"] else "이미 있음", result["event"]["start"], result["event"]["title"])
    return 0


CALENDAR_PREFIX = {"deadline": "마감", "event": "일정", "start": "접수 시작"}


def calendar_dates(analysis: Analysis, today: date) -> list:
    """캘린더에 넣을 날짜. 중요하고 자격이 없지 않은 공지의 남은 날짜만, 공지당 4개까지 고른다.

    high는 마감·일정·접수 시작을, 자격이 확실한 medium은 마감만 넣는다. 회차가 많은 특강은
    앞 회차만 들어가므로, 나머지는 사용자가 대화로 말하면 기록 단계가 넣는다.
    참석 여부는 사용자가 캘린더에서 예/아니요/미정으로 고른다.
    """
    if analysis.eligibility == "unlikely":
        return []
    if analysis.relevance == "high":
        kinds = {"deadline", "event", "start"}
    elif analysis.relevance == "medium" and analysis.eligibility == "likely":
        kinds = {"deadline"}
    else:
        return []
    chosen = [deadline for deadline in analysis.deadlines if deadline.kind in kinds and deadline.date >= today.isoformat()]
    return sorted(chosen, key=lambda deadline: (deadline.date, deadline.time))[:4]


def already_in_calendar(day: str, clock: str, existing: list[dict]) -> dict | None:
    """같은 날 같은 시각에 시작하는 일정이 있으면 그 일정. 사용자가 직접 넣은 일정과 겹치지 않게 한다."""
    from .google_bridge import same_time
    return next(iter(same_time(existing, day, clock)), None)


def cmd_calendar_dedupe(args) -> int:
    """비서가 넣은 일정이 사용자 일정과 같은 시각에 겹치면 비서 쪽을 지운다. 사용자 일정은 건드리지 않는다."""
    from . import google_bridge
    today = store.now().date()
    rows = google_bridge.list_events(today.isoformat(), (today + timedelta(days=args.days)).isoformat())
    removed = 0
    for row in rows:
        start = google_bridge.local_start(row)
        if not row.get("key") or start is None:
            continue
        owners = [other for other in google_bridge.same_time(rows, *start) if not other.get("key")]
        if not owners:
            continue
        print(f"중복 {start[0]} {start[1]} '{row['title'][:40]}' (사용자 일정 '{owners[0]['title'][:30]}')")
        if not args.dry_run:
            google_bridge.remove_event(row["id"])
            removed += 1
    print(f"지운 일정 {removed}건")
    return 0


def cmd_records_apply(args) -> int:
    """기록 단계에서 뽑은 일정을 다시 반영한다. 2판 스크립트로 올린 뒤 참석 응답을 채우는 데 쓴다."""
    from . import record
    today = store.now().date()
    for path in sorted(record.RECORD_DATA_DIR.glob("*.jsonl")):
        for row in store.read_jsonl(path):
            if row.get("at", "") < args.since:
                continue
            for line in record.apply_events(row, row["thread"], today):
                print(line)
    return 0


def calendar_ready() -> bool:
    """자동으로 일정을 넣는 조건: 연결돼 있고 참석 응답을 넣을 수 있는 2판 스크립트."""
    from . import google_bridge
    try:
        return google_bridge.configured() and google_bridge.version() >= 2
    except (google_bridge.BridgeError, httpx.HTTPError) as error:
        print(f"캘린더 연결 확인 실패: {error}")
        return False


def calendar_entry(title: str, url: str, deadline, group) -> str:
    """공지 날짜 하나를 캘린더에 넣고 그 일정 주소를 돌려준다.

    일정 key는 묶음의 처음 공지 번호(group)로 만든다. 재공지가 같은 날짜를 또 넣지 않는다.
    같은 시각에 사용자가 직접 넣은 일정이 있으면 새로 만들지 않고 그 일정을 가리킨다.
    """
    from . import google_bridge
    key = f"notice:{group}:{deadline.kind}:{deadline.date}"
    minutes = 60 if deadline.kind == "event" else 30
    reminders = google_bridge.EVENT_REMINDERS if deadline.kind == "event" else google_bridge.DEADLINE_REMINDERS
    same = already_in_calendar(deadline.date, deadline.time, google_bridge.list_events(deadline.date, deadline.date))
    if same and same.get("key") != key:
        return google_bridge.event_link(same, deadline.date)
    result = google_bridge.add_event(key, f"[{CALENDAR_PREFIX[deadline.kind]}] {deadline.label}"[:80],
                                     deadline.date, deadline.time, f"{title}\n{url}\n\n아주대 비서가 공지에서 넣은 일정입니다.",
                                     minutes=minutes, reminders=reminders)
    return result["event"]["link"]


def calendar_links_for(group, analysis: Analysis, today: date) -> dict[str, str]:
    """이미 캘린더에 있는 공지 날짜의 {날짜 key: 일정 주소}. 독촉 알림의 ✅ 버튼이 그날 화면이 아니라 일정을 가리키게 한다."""
    from . import google_bridge
    links: dict[str, str] = {}
    for deadline in analysis.deadlines:
        if deadline.date < today.isoformat():
            continue
        try:
            rows = google_bridge.list_events(deadline.date, deadline.date)
        except (google_bridge.BridgeError, httpx.HTTPError, ValueError) as error:
            print(f"캘린더 확인 실패 {group}: {error}")
            return links
        found = google_bridge.find_event(rows, f"notice:{group}:{deadline.kind}:{deadline.date}") \
            or already_in_calendar(deadline.date, deadline.time, rows)
        if found:
            links[calendar_key(deadline)] = google_bridge.event_link(found, deadline.date)
    return links


def add_to_calendar(item: ListItem, analysis: Analysis, today: date, group=None) -> dict[str, str]:
    """공지 날짜를 학교 캘린더에 넣고 {날짜 key: 일정 주소}를 돌려준다. 실패해도 알림은 막지 않는다."""
    from . import google_bridge
    links: dict[str, str] = {}
    # 1판 스크립트는 참석 응답을 못 넣는다. 응답 없는 일정이 쌓이지 않게 2판부터 자동으로 넣는다.
    if not calendar_ready():
        return links
    for deadline in calendar_dates(analysis, today):
        try:
            links[calendar_key(deadline)] = calendar_entry(item.title, item.url, deadline, item.article_id if group is None else group)
        except (google_bridge.BridgeError, httpx.HTTPError, ValueError, KeyError) as error:
            print(f"캘린더 등록 실패 {item.article_id}: {error}")
    return links


def cmd_calendar_sync(args) -> int:
    """Google 연결 전에 처리했거나 재점검으로 올린 공지 중 아직 남은 날짜를 캘린더에 넣는다."""
    today = store.now().date()
    closed = store.closed_items()
    titles: set[str] = set()
    added = 0
    for group in sorted(store.notice_groups(), key=lambda row: str(row.get("analyzed_at", "")), reverse=True):
        if group["members"] & closed:
            continue
        row = {**group, "id": group["root"]}
        analysis = Analysis.model_validate(row["analysis"])
        # 밀린 것을 한꺼번에 넣을 때는 확실한 것만 넣는다. 같은 글이 다른 번호로 다시 올라온 것은 한 번만.
        title = re.sub(r"\[(재공지|수정|홍보|당일 공지)\]|\(.*?수정.*?\)|\s+", "", row["title"])
        if analysis.relevance != "high" or analysis.eligibility != "likely" or title in titles:
            continue
        if not calendar_dates(analysis, today):
            continue
        titles.add(title)
        item = ListItem(row["id"], row["board"], "", row["title"], "", "", row.get("date", ""), link=row["url"])
        links = {} if args.dry_run else add_to_calendar(item, analysis, today)
        print(f"{row['id']} {row['title'][:50]} → {len(links) if not args.dry_run else len(calendar_dates(analysis, today))}건")
        added += len(links)
    print(f"캘린더 일정 {added}건 확인 (이미 있는 것은 새로 만들지 않음)")
    return 0


def cmd_style(args) -> int:
    from .style import apply_style
    return apply_style()


def recheck(fetcher: Fetcher, seen: dict, items: dict) -> list[ListItem]:
    """제목이 그대로인 채 본문만 고친 공지를 잡는다. 최신 글만 하루 한 번 비교한다."""
    changed: list[ListItem] = []
    newest = [item for item in items.values() if BOARD_BY_KEY[item.board_key].kind == "cms"]
    newest = sorted(newest, key=lambda item: item.article_id, reverse=True)[:40]
    for item in newest:
        row = seen.get(item.article_id)
        if not row or row.get("status") != "done" or not row.get("hash"):
            continue
        try:
            detail = fetcher.detail(item, BOARD_BY_KEY[item.board_key])
        except (httpx.HTTPError, ScrapeError):
            continue
        if detail.content_hash != row["hash"]:
            changed.append(item)
    return changed


def retry_partials(fetcher: Fetcher, client, discord: Discord, seen: dict, today: date, dry_run: bool) -> int:
    """첨부를 일부만 읽고 알린 글을 하루 한 번 다시 읽는다. 더 읽히면 다시 판단해 알린다."""
    fixed = 0
    cutoff = (today - timedelta(days=PARTIAL_RETRY_DAYS)).isoformat()
    for key, row in seen.items():
        if not row.get("partial") or row.get("status") != "done" or row.get("date", "") < cutoff:
            continue
        board = BOARD_BY_KEY[row["board"]]
        item = ListItem(key, board.key, board.url, row["title"], "", "", row.get("date", ""), link=row.get("link", ""))
        try:
            detail = fetcher.detail(item, board)
            extraction, originals = gather(fetcher, item, detail)
            if len(extraction.failures) >= row["partial"]:
                continue
            outcome = handle(item, fetcher, client, discord, today, changed="[내용 보완] ")
        except Exception as error:
            print(f"재확인 실패 {key}: {type(error).__name__}: {str(error)[:120]}")
            continue
        fixed += 1
        if not dry_run:
            base = {k: v for k, v in row.items() if k != "at"}
            store.append_seen({**base, "hash": outcome["hash"], "relevance": outcome["relevance"], "partial": outcome.get("partial", 0)})
    return fixed


def upcoming_deadlines(today: date) -> list[tuple[int, dict, dict]]:
    closed = store.closed_items()
    tracked = store.tracked_items()
    rows = []
    for row in store.notice_groups():
        analysis = row["analysis"]
        if row["members"] & closed:
            continue
        if not row["members"] & tracked and (analysis["relevance"] not in ("high", "medium") or analysis["eligibility"] == "unlikely"):
            continue
        for deadline in analysis["deadlines"]:
            try:
                left = (date.fromisoformat(deadline["date"]) - today).days
            except ValueError:
                continue
            if deadline["kind"] == "deadline" and 0 <= left <= DEADLINE_HORIZON:
                rows.append((left, deadline, row))
    rows.sort(key=lambda entry: entry[0])
    return rows


def cmd_digest(args) -> int:
    today = store.now().date()
    client, discord = make_clients(args.dry_run)
    if args.standby:
        sync_all(f"sync: {socket.gethostname().split('.')[0]} {store.now():%m-%d %H:%M}")
        if primary_is_alive():
            print("주 호스트가 동작 중이라 대기합니다.")
            return 0
    fetcher = Fetcher()
    items, failures = collect_lists(fetcher)
    seen = store.load_seen()
    for item in recheck(fetcher, seen, items):
        try:
            outcome = handle(item, fetcher, client, discord, today, changed=True)
        except Exception as error:
            print(f"수정 재확인 실패 {item.article_id}: {type(error).__name__}: {str(error)[:120]}")
            continue
        if not args.dry_run:
            store.append_seen({"id": item.article_id, "board": item.board_key, "title": item.title, "date": item.date,
                               "sig": item.signature, "hash": outcome["hash"], "status": "done",
                               "relevance": outcome["relevance"], "attempts": 0, "partial": outcome.get("partial", 0)})
    retry_partials(fetcher, client, discord, seen, today, args.dry_run)
    seen = store.load_seen()
    day = today.isoformat()
    rows = [row for row in store.load_analysis(day[:7]) if row["analyzed_at"].startswith(day) and not row.get("backfill")]
    links = store.load_reposts()
    repeats = [row for row in rows if row["id"] in links]
    rows = [row for row in rows if row["id"] not in links]
    groups = {"high": [], "medium": [], "low": [], "none": []}
    for row in rows:
        groups[row["analysis"]["relevance"]].append(row)
    labels = {"high": "🔴 높음", "medium": "🟠 보통", "low": "⚪ 낮음", "none": "⚫ 무관"}
    lines = [f"**{day} 공지 요약** · 새 글 {len(rows)}건" + (f" · 수집 실패 {len(failures)}곳" if failures else "")]
    deadlines = upcoming_deadlines(today)
    if deadlines:
        lines.append(f"\n**⏳ {DEADLINE_HORIZON}일 안의 마감** {len(deadlines)}건")
        lines += [f"- D-{left} {deadline['date']} {deadline['time']} [{row['title'][:60]}](<{row['url']}>)".replace("  ", " ")
                  for left, deadline, row in deadlines]
    tracked = store.tracked_items()
    following = [group for group in store.notice_groups() if group["members"] & tracked and not group["members"] & store.closed_items()]
    if following:
        lines.append(f"\n**📍 추적 중** {len(following)}건")
        for group in following:
            ahead = sorted(d for d in group["analysis"]["deadlines"] if d["kind"] == "deadline" and d["date"] >= day)
            when = f"D-{(date.fromisoformat(ahead[0]['date']) - today).days} {ahead[0]['date']}" if ahead else "마감 지남"
            lines.append(f"- {when} [{group['title'][:60]}](<{group['url']}>)")
    pending = [(key, row) for key, row in seen.items() if row.get("status") == "pending"]
    if pending:
        lines.append(f"\n**⚠️ 아직 처리하지 못한 글** {len(pending)}건 (계속 다시 시도 중)")
        lines += [f"- {row['title'][:70]}" for _, row in pending[:10]]
    partial = [row for row in seen.values() if row.get("partial") and row.get("status") == "done" and row.get("date", "") >= (today - timedelta(days=PARTIAL_RETRY_DAYS)).isoformat()]
    if partial:
        lines.append(f"\n**📎 첨부를 다 읽지 못한 글** {len(partial)}건 (매일 다시 확인)")
        lines += [f"- {row['title'][:70]}" for row in partial[:10]]
    if not rows:
        lines.append("\n오늘은 새 글이 없었습니다. 수집은 정상입니다." if not failures else "\n수집에 문제가 있어 새 글을 확인하지 못했을 수 있습니다.")
    for key in ("high", "medium", "low", "none"):
        if not groups[key]:
            continue
        lines.append(f"\n**{labels[key]}** {len(groups[key])}건")
        titled = [row for row in groups[key] if key in ("high", "medium") or row["board"] != "recruit"]
        lines += [f"- [{row['title'][:70]}](<{row['url']}>)" for row in titled]
        if len(titled) < len(groups[key]):
            lines.append(f"- 채용 공고 {len(groups[key]) - len(titled)}건 (제목 생략)")
    if repeats:
        lines.append(f"\n**🔁 다시 올라온 공지** {len(repeats)}건 (처음 알림에 묶음)")
        lines += [f"- [{row['title'][:60]}](<{row['url']}>)" + (f": {links[row['id']]['changes'][:80]}" if links[row["id"]].get("changes") else "")
                  for row in repeats]
    english_line = english.summary_line(today)
    if english_line:
        lines.append(f"\n{english_line}")
    lines.append(f"\n이번 달 AI 비용 약 ${store.month_usage():.2f}")
    important = groups["high"] or groups["medium"] or deadlines or pending
    discord.text("digest", "\n".join(lines), tier=Tier.NOTIFY if important else Tier.QUIET)
    if not args.dry_run:
        store.write_heartbeat({"new": 0, "digest": day})
        message = f"auto: {day} 일일 요약"
        push_error = sync_all(message) if args.standby else sync_push(message)
        alert_state("git-push", bool(push_error), f"git push 실패: {push_error}", discord)
    print(f"요약 {len(rows)}건")
    return 0


def cmd_check(args) -> int:
    preflight()
    today = store.now().date()
    secrets = load_secrets()
    client = anthropic.Anthropic(api_key=secrets["ANTHROPIC_API_KEY"], timeout=180, max_retries=3)
    fetcher = Fetcher()
    board: Board = BOARD_BY_KEY[args.board]
    items, _ = collect_lists(fetcher)
    item = items.get(args.article) or ListItem(args.article, board.key, board.url, args.title or "(제목 미상)", "", "", today.isoformat())
    detail, extraction, _, notes, result = analyze(fetcher, client, item, today, args.effort)
    print(json.dumps(result.analysis.model_dump(), ensure_ascii=False, indent=1))
    print("이미지", len(extraction.images), "실패", extraction.failures, "메모", notes)
    print("사용량", result.usage, "모델", result.model, f"비용 ${cost(result.usage, result.model):.4f}")
    return 0


def cmd_weekly(args) -> int:
    """일요일 밤 주간 정리. 기록이 없으면 AI를 부르지 않고 짧게 묻는다."""
    from . import weekly

    moment = store.now()
    today = moment.date()
    client, discord = make_clients(args.dry_run)
    if not args.dry_run:
        pull_error = sync_pull()
        alert_state("git-pull", bool(pull_error), f"git pull 실패: {pull_error}", discord)
    rows = weekly.week_rows(today)
    if not rows:
        discord.text("records", f"**🗂 주간 정리 {weekly.week_label(today)}**\n이번 주에는 스레드 대화 기록이 없습니다. "
                                "한 일이나 결정이 있었다면 `#💬대화`에 한두 줄 남겨 주세요.", tier=Tier.QUIET)
        return 0
    result, usage, model = weekly.build(client, rows, today)
    spent = cost(usage, model)
    store.add_usage("weekly-dry" if args.dry_run else "weekly", spent)
    text = weekly.render(result, today, len(rows), moment)
    if args.dry_run:
        print(text)
    else:
        path = weekly.write(text, today)
        push_error = sync_push(f"auto: 주간 정리 {weekly.week_label(today)}", path, str(store.USAGE_DIR))
        alert_state("git-push", bool(push_error), f"git push 실패: {push_error}", discord)
    discord.text("records", weekly.discord_text(result, today), tier=Tier.NOTIFY)
    print(f"주간 정리 기록 {len(rows)}건, 비용 ${spent:.3f}")
    return 0


# 컨테이너 안의 시계. systemd 타이머와 같은 시각에 같은 명령을 돈다. 꺼져 있던 동안 놓친 차례는
# 켜질 때 한 번만 돈다(systemd의 Persistent=true와 같다). 각 명령은 따로 프로세스로 띄워, 한 번의
# 실패나 메모리 문제가 시계를 멈추지 않게 한다. 명령끼리는 run.lock으로 겹치지 않는다.
SCHEDULE_FILE = STATE_DIR / "schedule.json"
SCHEDULE_JOBS = ("run", "digest", "weekly")


def last_slot(job: str, moment: datetime) -> datetime:
    """job이 가장 최근에 돌았어야 할 시각."""
    if job == "run":
        slot = moment.replace(minute=7, second=0, microsecond=0)
        return slot if moment >= slot else slot - timedelta(hours=1)
    if job == "digest":
        slot = moment.replace(hour=21, minute=30, second=0, microsecond=0)
        return slot if moment >= slot else slot - timedelta(days=1)
    slot = (moment - timedelta(days=(moment.weekday() - 6) % 7)).replace(hour=21, minute=0, second=0, microsecond=0)
    return slot if moment >= slot else slot - timedelta(days=7)


def due_jobs(moment: datetime, done: dict[str, str]) -> list[str]:
    return [job for job in SCHEDULE_JOBS if datetime.fromisoformat(done[job]) < last_slot(job, moment)]


def cmd_schedule(args) -> int:
    import time
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        done = json.loads(SCHEDULE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        done = {}
    start = store.now().isoformat(timespec="seconds")
    # 처음 켤 때는 지금 시각을 마지막 실행으로 둔다. systemd에서 옮겨 온 날 같은 요약을 두 번 보내지 않게.
    done = {job: done.get(job, start) for job in SCHEDULE_JOBS}
    SCHEDULE_FILE.write_text(json.dumps(done))
    print(f"시계 시작: {start}", flush=True)
    while True:
        for job in due_jobs(store.now(), done):
            done[job] = store.now().isoformat(timespec="seconds")
            SCHEDULE_FILE.write_text(json.dumps(done))
            print(f"{done[job]} {job} 시작", flush=True)
            result = subprocess.run([sys.executable, "-m", "ajou_assistant.cli", job])
            print(f"{store.now():%H:%M:%S} {job} 끝 ({result.returncode})", flush=True)
        time.sleep(30)


LINK_BATCH = 40


def cmd_link_reposts(args) -> int:
    """이미 처리한 공지에서 재공지를 찾아 묶는다. 알림은 보내지 않는다. 이후의 독촉·요약·캘린더가 묶음을 따른다."""
    client, _ = make_clients(True)
    if not args.dry_run:
        sync_pull()
    rows = sorted(store.recent_analysis(), key=store.posted_order)  # 먼저 올라온 글이 처음 공지가 된다.
    linked = store.load_reposts()
    cases = []
    for index, row in enumerate(rows):
        if row["id"] in linked:
            continue
        earlier = repost.candidates(row["id"], row["board"], row["title"], row.get("date", ""), rows[:index])
        if earlier:
            cases.append((row, earlier))
    spent, count = 0.0, 0
    for start in range(0, len(cases), LINK_BATCH):
        batch = cases[start:start + LINK_BATCH]
        verdicts, usage, model = repost.judge(client, batch)
        spent += cost(usage, model)
        for row, earlier in batch:
            verdict = verdicts.get(str(row["id"]))
            if verdict is None:
                print(f"별개 {row['id']} {row['title'][:50]}")
                continue
            count += 1
            print(f"묶음 {row['id']} → {verdict['same_as']} {row['title'][:50]}" + (f" ({verdict['changes'][:60]})" if verdict["changes"] else ""))
            if not args.dry_run:
                store.add_repost(row["id"], verdict["same_as"], verdict["changes"], "link-reposts")
    store.add_usage("repost-dry" if args.dry_run else "repost", spent)
    if not args.dry_run:
        sync_push(f"auto: 재공지 {count}건 묶음", str(store.REPOSTS_FILE), str(store.USAGE_DIR))
    print(f"후보 {len(cases)}건 중 {count}건 묶음, 비용 ${spent:.3f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="ajou")
    parser.add_argument("--dry-run", action="store_true", help="Discord와 git에 쓰지 않는다")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed")
    seed.add_argument("--board", default="")
    seed.set_defaults(func=cmd_seed)
    run = commands.add_parser("run")
    run.add_argument("--standby", action="store_true", help="다른 호스트가 살아 있으면 대기")
    run.set_defaults(func=cmd_run)
    digest = commands.add_parser("digest")
    digest.add_argument("--standby", action="store_true", help="다른 호스트가 살아 있으면 대기")
    digest.set_defaults(func=cmd_digest)
    backfill = commands.add_parser("backfill")
    backfill.add_argument("--days", type=int, default=14)
    backfill.add_argument("--limit", type=int, default=0)
    backfill.add_argument("--board", default="")
    backfill.add_argument("--count", action="store_true", help="대상 수만 세고 끝낸다")
    backfill.add_argument("--prefilter", action="store_true", help="제목으로 먼저 넓게 거른다")
    backfill.set_defaults(func=cmd_backfill)
    reanalyze = commands.add_parser("reanalyze")
    reanalyze.add_argument("articles", nargs="+")
    reanalyze.set_defaults(func=cmd_reanalyze)
    mark = commands.add_parser("mark")
    mark.add_argument("article")
    mark.add_argument("action", choices=["done", "skip", "open", "track"])
    mark.set_defaults(func=cmd_mark)
    ignore = commands.add_parser("ignore")
    ignore.add_argument("article")
    ignore.set_defaults(func=cmd_ignore)
    commands.add_parser("tier-test").set_defaults(func=cmd_tier_test)
    commands.add_parser("style").set_defaults(func=cmd_style)
    commands.add_parser("google-check").set_defaults(func=cmd_google_check)
    calendar_add = commands.add_parser("calendar-add")
    calendar_add.add_argument("title")
    calendar_add.add_argument("date")
    calendar_add.add_argument("time", nargs="?", default="")
    calendar_add.add_argument("--minutes", type=int, default=60)
    calendar_add.add_argument("--notes", default="")
    calendar_add.set_defaults(func=cmd_calendar_add)
    calendar_sync = commands.add_parser("calendar-sync")
    calendar_sync.add_argument("--dry-run", action="store_true")
    calendar_sync.set_defaults(func=cmd_calendar_sync)
    dedupe = commands.add_parser("calendar-dedupe")
    dedupe.add_argument("--days", type=int, default=120)
    dedupe.add_argument("--dry-run", action="store_true")
    dedupe.set_defaults(func=cmd_calendar_dedupe)
    records_apply = commands.add_parser("records-apply")
    records_apply.add_argument("--since", default="", help="이 날짜(YYYY-MM-DD) 이후 기록만 다시 반영. 기본은 전체")
    records_apply.set_defaults(func=cmd_records_apply)
    commands.add_parser("weekly").set_defaults(func=cmd_weekly)
    commands.add_parser("schedule").set_defaults(func=cmd_schedule)
    link_reposts = commands.add_parser("link-reposts")
    link_reposts.add_argument("--dry-run", action="store_true")
    link_reposts.set_defaults(func=cmd_link_reposts)
    check = commands.add_parser("check")
    check.add_argument("board", choices=sorted(BOARD_BY_KEY))
    check.add_argument("article", type=int)
    check.add_argument("--title", default="")
    check.add_argument("--effort", default="low")
    check.set_defaults(func=cmd_check)
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.func is cmd_schedule:  # 시계는 계속 돌며 자식 명령이 잠금을 잡아야 하므로 잠그지 않는다.
        return cmd_schedule(args)
    with (STATE_DIR / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("이미 실행 중입니다.")
            return 0
        try:
            return args.func(args)
        except SetupError as error:
            print(f"설정 오류: {error}", file=sys.stderr)
            return 2
        except Exception:
            traceback.print_exc()
            return 2


if __name__ == "__main__":
    sys.exit(main())
