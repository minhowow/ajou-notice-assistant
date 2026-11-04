"""일정이 끝나면 기록을 청한다.

강연·면담·면접처럼 남길 만한 일정이 끝나고 30분이 지나면, 봇이 대화 채널에 스레드를 열어 어땠는지 묻는다.
사용자가 스레드에 쓴 글은 AI를 거치지 않고 그대로 records/reflections/에 쌓인다. 대화가 쉬면 평소 기록 단계가
할 일과 사실을 따로 뽑는다. 남길 만한 일정인지는 코드가 정한다:

- 비서가 넣은 일정은 사용자가 캘린더에서 '참석'으로 답한 것만
- 사용자가 직접 넣은 일정은 제목에 강연·면담 같은 말이 있는 것만
- 종일 일정, 불참, [마감]·[접수 시작]·[면담 후보] 같은 할 일 표시는 묻지 않는다
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import HOME_DIR
from .store import KST

REFLECTIONS_DIR = HOME_DIR / "records" / "reflections"
DELAY = timedelta(minutes=30)
WINDOW = timedelta(hours=36)
MAX_PER_RUN = 3
KEYWORDS = ("강연", "특강", "세미나", "설명회", "간담회", "면담", "면접", "인터뷰", "미팅", "회의", "멘토", "멘토링",
            "대회", "페스티벌", "박람회", "워크숍", "워크샵", "talk", "meetup", "interview")
SKIP_PREFIXES = ("[마감]", "[접수 시작]", "[면담 후보]")


def _moment(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(KST) if moment.tzinfo else moment.replace(tzinfo=KST)


def worth(row: dict) -> bool:
    """끝난 뒤 기록을 청할 만한 일정인가."""
    title = str(row.get("title", "")).strip()
    if row.get("allDay") or row.get("status") == "declined" or title.startswith(SKIP_PREFIXES):
        return False
    if row.get("status") == "accepted":
        return True
    if row.get("key"):  # 비서가 넣었는데 참석 답이 없는 일정은 할 일 표시이거나 가지 않은 일정이다.
        return False
    lowered = title.lower()
    return any(word in lowered for word in KEYWORDS)


def due(rows: list[dict], now: datetime, asked: set[str]) -> list[dict]:
    """끝나고 30분이 지났고 36시간 안인, 아직 묻지 않은 일정. 오래된 것부터 하루 최대 몇 개만."""
    found = []
    for row in rows:
        if str(row.get("id")) in asked or not worth(row):
            continue
        end = _moment(row.get("end", ""))
        if end is not None and now - WINDOW <= end <= now - DELAY:
            found.append((end, row))
    return [row for _, row in sorted(found, key=lambda pair: pair[0])][:MAX_PER_RUN]


def quiet_hours(now: datetime) -> bool:
    return now.hour >= 23 or now.hour < 8


def when_text(row: dict, now: datetime) -> str:
    start = _moment(row.get("start", ""))
    if start is None:
        return ""
    if start.date() == now.date():
        day = "오늘"
    elif start.date() == (now - timedelta(days=1)).date():
        day = "어제"
    else:
        day = f"{start:%m/%d}"
    return f"{day} {start:%H:%M}"


def prompt(row: dict, now: datetime) -> str:
    return (f"✍️ {when_text(row, now)} 「{row.get('title', '일정')}」 어땠나요?\n"
            "이 스레드에 편하게 남겨 주세요. 기억에 남는 말, 새로 알게 된 것, 해 볼 것 중 하나만 써도 됩니다.")


def thread_title(row: dict) -> str:
    return f"✍️ {row.get('title', '일정')}"[:90]


def reflection_path(event: dict) -> Path:
    start = _moment(event.get("start", ""))
    day = f"{start:%Y-%m-%d}" if start else "날짜없음"
    slug = re.sub(r"[^\w가-힣]+", "_", str(event.get("title", "일정"))).strip("_")[:40] or "일정"
    return REFLECTIONS_DIR / f"{day}_{slug}.md"


def save(event: dict, text: str, when: datetime) -> Path:
    """사용자가 쓴 글을 그대로 붙인다. 처음이면 일정 정보로 머리를 단다."""
    path = reflection_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        start, end = _moment(event.get("start", "")), _moment(event.get("end", ""))
        span = f"{start:%Y-%m-%d %H:%M}~{end:%H:%M}" if start and end else ""
        path.write_text(f"# {event.get('title', '일정')}\n\n- 일정: {span}\n- Discord 스레드에 쓴 글을 그대로 옮긴 기록이다.\n")
    with path.open("a") as handle:
        handle.write(f"\n## {when:%Y-%m-%d %H:%M}\n\n{text.strip()}\n")
    return path
