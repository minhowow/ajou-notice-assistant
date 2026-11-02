"""Discord 전송. 문구는 공지·AI에서 왔으므로 멘션을 전부 끄고, 링크는 코드가 만든 것만 쓴다.

알림은 네 단계다. 단계는 코드가 정하고, 사용자 멘션도 코드가 붙인다.
  PING    강한 알림: 사용자 멘션. 휴대폰 푸시와 빨간 배지
  NOTIFY  일반 알림: 멘션 없는 메시지. 서버 알림이 '모든 메시지'일 때 푸시
  QUIET   조용한 기록: 채널에 쌓이지만 푸시 없음
  DIGEST  요약에만: 채널에 올리지 않고 21:30 요약에 제목만 남김
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from enum import Enum
from urllib.parse import urlencode

import httpx

from .scrape import Detail, ListItem
from .triage import Analysis

API = "https://discord.com/api/v10"
SUPPRESS_EMBEDS = 4
SUPPRESS_NOTIFICATIONS = 4096
MAX_UPLOAD = 9 * 1024 * 1024
EMBED_LIMIT = 5900
COLORS = {"high": 0xE03131, "medium": 0xF08C00, "low": 0x868E96, "none": 0x868E96}
RELEVANCE = {"high": "높음", "medium": "보통", "low": "낮음", "none": "무관"}
ELIGIBILITY = {"likely": "가능성 높음", "unlikely": "해당 안 될 가능성 높음", "unknown": "확인 필요"}
CHANNEL_BY_CATEGORY = {"scholarship": "scholarship", "international": "international", "career": "career", "activity": "career"}


class Tier(str, Enum):
    PING = "ping"
    NOTIFY = "notify"
    QUIET = "quiet"
    DIGEST = "digest"


class DiscordError(RuntimeError):
    """전송 실패. 400대는 메시지 문제, 나머지는 Discord 장애로 본다."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Discord {status}: {detail}")
        self.status = status

    @property
    def is_outage(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500


class Discord:
    def __init__(self, token: str, config: dict, dry_run: bool = False) -> None:
        self.channels = config["channels"]
        self.owner = config.get("owner_id")
        self.dry_run = dry_run
        self.client = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (ajou-assistant, 0.2)"},
            timeout=30,
        )

    def send(self, channel: str, payload: dict, files: list[tuple[str, bytes]] | None = None, tier: Tier = Tier.NOTIFY) -> str | None:
        """보낸 메시지 ID를 돌려준다."""
        payload = dict(payload)
        mentions: dict = {"parse": []}
        if tier == Tier.PING and self.owner:
            payload["content"] = f"<@{self.owner}> {payload.get('content', '')}".strip()
            mentions["users"] = [self.owner]
        if tier == Tier.QUIET:
            payload["flags"] = payload.get("flags", 0) | SUPPRESS_NOTIFICATIONS
        payload["allowed_mentions"] = mentions
        if self.dry_run:
            print(f"[dry-run #{channel} {tier.value}]", json.dumps(payload, ensure_ascii=False)[:1500])
            return None
        path = f"/channels/{self.channels[channel]}/messages"
        for _ in range(5):
            try:
                if files:
                    parts = {f"files[{i}]": (name, data) for i, (name, data) in enumerate(files)}
                    response = self.client.post(path, data={"payload_json": json.dumps(payload)}, files=parts)
                else:
                    response = self.client.post(path, json=payload)
            except httpx.HTTPError as error:
                raise DiscordError(0, str(error)[:200]) from error
            if response.status_code == 429:
                time.sleep(float(response.json().get("retry_after", 2)) + 0.2)
                continue
            if response.status_code >= 400:
                raise DiscordError(response.status_code, response.text[:200])
            return response.json().get("id")
        raise DiscordError(429, "재시도 초과")

    def text(self, channel: str, message: str, tier: Tier = Tier.NOTIFY) -> None:
        for chunk in split_message(message):
            self.send(channel, {"content": chunk}, tier=tier)


def split_message(message: str, limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in message.split("\n"):
        while len(line) > limit:
            line = line[:limit - 1] + "…"
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_deadlines(analysis: Analysis, today: date) -> str:
    lines = []
    for item in analysis.deadlines:
        suffix = ""
        try:
            days = (date.fromisoformat(item.date) - today).days
            suffix = f" (D-{days})" if days >= 0 else " (지남)"
        except ValueError:
            pass
        when = f"{item.date} {item.time}".strip()
        lines.append(f"{item.label}: {when}{suffix}")
    return "\n".join(lines)


def _days_left(item, today: date) -> int | None:
    try:
        return (date.fromisoformat(item.date) - today).days
    except ValueError:
        return None


def route(analysis: Analysis, today: date) -> tuple[str | None, Tier]:
    """(채널, 알림 단계). 낮은 공지는 채널로 보내지 않고 일일요약에만 남긴다."""
    if analysis.relevance in ("low", "none"):
        return None, Tier.DIGEST
    left = [_days_left(item, today) for item in analysis.deadlines if item.kind == "deadline"]
    left = [days for days in left if days is not None and days >= 0]
    channel = CHANNEL_BY_CATEGORY.get(analysis.category, "general")
    if analysis.relevance == "high":
        if any(days <= 7 for days in left):
            return "urgent", Tier.PING
        return channel, Tier.NOTIFY
    return channel, Tier.NOTIFY if left else Tier.QUIET


def calendar_url(title: str, day: str, clock: str, url: str, account: str = "", minutes: int = 60) -> str | None:
    """구글 캘린더 '일정 추가' 화면을 여는 주소. 사용자가 저장을 눌러야 등록된다."""
    try:
        if clock:
            start = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M")
            end = start + timedelta(minutes=minutes)
            dates = f"{start:%Y%m%dT%H%M00}/{end:%Y%m%dT%H%M00}"
        else:
            first = date.fromisoformat(day)
            dates = f"{first:%Y%m%d}/{first + timedelta(days=1):%Y%m%d}"
    except ValueError:
        return None
    params = {"action": "TEMPLATE", "text": title[:50], "dates": dates, "ctz": "Asia/Seoul", "details": url}
    if account:
        params["authuser"] = account
    link = "https://calendar.google.com/calendar/render?" + urlencode(params)
    return link if len(link) <= 512 else None


def calendar_key(deadline) -> str:
    return f"{deadline.kind}:{deadline.date}:{deadline.label}"


def build_components(item: ListItem, analysis: Analysis, today: date, account: str = "",
                     calendar_links: dict[str, str] | None = None) -> list[dict]:
    """원문·캘린더는 주소 버튼, 대화·완료·패스는 봇이 받는 버튼이다.

    비서가 이미 캘린더에 넣은 날짜는 그 일정으로 가는 버튼(✅)을, 넣지 않은 날짜는 사용자가 저장할 수
    있는 '일정 추가' 화면 버튼(📅)을 단다.
    """
    calendar_links = calendar_links or {}
    links: list[dict] = [{"type": 2, "style": 5, "label": "원문", "url": item.url[:512]}]
    for deadline in analysis.deadlines:
        days = _days_left(deadline, today)
        if days is None or days < 0 or len(links) >= 5:
            continue
        added = calendar_links.get(calendar_key(deadline))
        if added:
            links.append({"type": 2, "style": 5, "label": _clip(f"✅ 캘린더: {deadline.label}", 40), "url": added[:512]})
            continue
        if deadline.kind == "start" or len(links) >= 4:
            continue
        link = calendar_url(f"[{'마감' if deadline.kind == 'deadline' else '일정'}] {deadline.label}", deadline.date, deadline.time, item.url, account)
        if link:
            links.append({"type": 2, "style": 5, "label": _clip(f"📅 {deadline.label}", 40), "url": link})
    key = str(item.article_id)
    actions = [
        {"type": 2, "style": 1, "label": "💬 대화", "custom_id": f"ajou:thread:{key}"[:100]},
        {"type": 2, "style": 3, "label": "완료", "custom_id": f"ajou:done:{key}"[:100]},
        {"type": 2, "style": 2, "label": "패스", "custom_id": f"ajou:skip:{key}"[:100]},
    ]
    return [{"type": 1, "components": links}, {"type": 1, "components": actions}]


def fit_embed(embed: dict) -> dict:
    """Discord는 임베드 전체 글자 수도 제한한다. 필드를 지키고 요약을 줄인다."""
    size = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
    size += sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
    if size > EMBED_LIMIT:
        keep = max(200, len(embed["description"]) - (size - EMBED_LIMIT))
        embed["description"] = _clip(embed["description"], keep)
    return embed


def build_embed(item: ListItem, detail: Detail, analysis: Analysis, notes: list[str], failures: list[str], today: date, changed: bool) -> dict:
    fields = [
        {"name": "왜 나에게", "value": _clip(analysis.why_relevant or "-", 1024)},
        {"name": "자격", "value": _clip(f"{ELIGIBILITY[analysis.eligibility]}: {analysis.eligibility_reason}", 1024)},
        {"name": "다음 행동", "value": _clip(analysis.action or "-", 1024)},
    ]
    deadlines = format_deadlines(analysis, today)
    if deadlines:
        fields.append({"name": "일정", "value": _clip(deadlines, 1024)})
    if analysis.unknowns:
        fields.append({"name": "확인 필요", "value": _clip("\n".join(analysis.unknowns), 1024)})
    if detail.attachments:
        links = "\n".join(f"[{_clip(a.name.replace(']', ' '), 60)}]({a.url})" for a in detail.attachments[:8])
        fields.append({"name": "첨부", "value": _clip(links, 1024)})
    warnings = [*failures, *notes]
    if warnings:
        fields.append({"name": "⚠️ 다 읽지 못함", "value": _clip("\n".join(warnings), 1024)})
    prefix = changed if isinstance(changed, str) else "[수정됨] " if changed else ""
    return fit_embed({
        "title": _clip(prefix + item.title, 256),
        "url": item.url,
        "description": _clip(analysis.summary, 4000),
        "color": COLORS[analysis.relevance],
        "fields": fields,
        "footer": {"text": _clip(f"관련도 {RELEVANCE[analysis.relevance]} · {item.department or item.board_key} · {item.date}", 200)},
    })


def upload_images(images: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """원본 공지 이미지를 함께 올린다. Discord 한도(메시지당 10개)와 용량을 넘기지 않는다."""
    files: list[tuple[str, bytes]] = []
    total = 0
    for index, (_, data) in enumerate(images[:10], start=1):
        if total + len(data) > MAX_UPLOAD:
            break
        total += len(data)
        files.append((f"notice-{index}.jpg", data))
    return files
