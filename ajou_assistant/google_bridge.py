"""학교 Google 계정의 Apps Script 연결. 설정하지 않았으면 조용히 건너뛴다.

setup/setup-google.sh가 배포 주소와 비밀값을 env에 넣는다. 캘린더 쓰기는 이 모듈의 함수로만 하고,
메일은 읽기만 한다. 스크립트 2판부터 일정에 나를 참석자로 넣어 캘린더에서 예/아니요/미정을 고를 수 있다.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import httpx

from .config import SetupError, load_secrets

KST = timezone(timedelta(hours=9))
DEADLINE_REMINDERS = [60, 1440, 4320]  # 1시간·하루·사흘 전
EVENT_REMINDERS = [30, 1440]
RSVP_STATUSES = ("accepted", "declined", "tentative", "needsAction")


class BridgeError(RuntimeError):
    pass


def configured() -> bool:
    return "GOOGLE_BRIDGE_URL" in load_secrets()


def call(action: str, **params) -> dict | list:
    secrets = load_secrets()
    response = httpx.post(
        secrets["GOOGLE_BRIDGE_URL"],
        json={"secret": secrets["GOOGLE_BRIDGE_SECRET"], "action": action, **params},
        follow_redirects=True, timeout=60,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as error:
        raise BridgeError(f"응답이 JSON이 아님: {response.text[:160]}") from error
    if not body.get("ok"):
        raise BridgeError(str(body.get("error"))[:200])
    return body["result"]


@lru_cache(maxsize=1)
def _ping() -> dict:
    return dict(call("ping"))


def version() -> int:
    return int(_ping().get("version", 1))


def calendar_id() -> str:
    return str(_ping().get("calendar", ""))


def event_times(day: str, clock: str, minutes: int) -> tuple[str, str, bool]:
    if clock:
        start = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        return start.isoformat(), (start + timedelta(minutes=minutes)).isoformat(), False
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST)
    return start.isoformat(), (start + timedelta(days=1)).isoformat(), True


def local_start(row: dict) -> tuple[str, str] | None:
    """일정 시작을 한국 시간 (날짜, HH:MM)으로. 종일 일정이면 None.

    1판 스크립트는 UTC(…Z)로, 2판은 +09:00으로 돌려준다. 문자열을 그대로 자르면 19:00이 10:00이 된다.
    """
    if row.get("allDay"):
        return None
    try:
        moment = datetime.fromisoformat(str(row.get("start", "")).replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None
    return f"{moment:%Y-%m-%d}", f"{moment:%H:%M}"


def same_time(rows: list[dict], day: str, clock: str) -> list[dict]:
    return [row for row in rows if clock and local_start(row) == (day, clock)]


# 제목 비교에서 빼는 흔한 말. 둘 다 '설명회'라고 같은 일정은 아니다.
COMMON_WORDS = {"안내", "설명회", "특강", "마감", "신청", "일정", "모집", "행사", "공지", "참가", "참여", "접수", "시작",
                "zoom", "온라인", "오프라인", "업무", "프로그램", "제출", "기한", "event"}


def _words(title: str) -> set[str]:
    return {word for word in re.findall(r"[0-9a-z가-힣]{2,}", (title or "").lower()) if word not in COMMON_WORDS and not word.isdigit()}


def overlap(first: str, second: str) -> float:
    """두 제목이 얼마나 겹치는지(0~1). 한쪽 낱말이 다른 쪽 낱말 안에 들어 있어도 겹친 것으로 센다('석사'와 '해외석사')."""
    left, right = _words(first), _words(second)
    if not left or not right:
        return 0.0
    hits = sum(1 for word in left if any(word in other or other in word for other in right))
    return hits / min(len(left), len(right))


def find_same(rows: list[dict], day: str, clock: str, title: str, key: str = "") -> dict | None:
    """이미 캘린더에 있는 같은 일정. 없으면 None. (11/16: 메일이 사용자가 넣어 둔 'Purdue 학부 석사' 옆에 같은 설명회를 또 넣었다.)

    같은 날이고 제목이 절반 넘게 겹치거나, 같은 시각이고 제목이 조금이라도 겹치거나, 같은 시각의 제목 없는 일정이면 같은
    일정으로 본다. 같은 시각이라도 제목이 전혀 다른 일정(수업 등)은 다른 일정이다. 같은 key(비서가 이미 넣은 그 일정)는 뺀다.
    """
    for row in sorted(rows, key=lambda row: bool(row.get("key"))):  # 사용자가 직접 만든 일정을 먼저 본다
        if key and row.get("key") == key:
            continue
        start = local_start(row)
        same_day = (start or (str(row.get("start", ""))[:10], ""))[0] == day
        if not same_day:
            continue
        score = overlap(title, str(row.get("title", "")))
        if score >= 0.5:
            return row
        if clock and start == (day, clock) and (score > 0 or not _words(str(row.get("title", "")))):
            return row
    return None


def remove_event(event_id: str) -> dict:
    """비서가 만든 일정(ajou_key가 있는 일정)만 지울 수 있다. 스크립트가 다시 확인한다."""
    if version() < 2:
        raise BridgeError("Apps Script를 2판으로 올려야 일정을 지울 수 있음")
    return call("calendar_remove", id=event_id)


def event_link(row: dict, day: str = "") -> str:
    """일정 하나를 여는 주소. 스크립트가 htmlLink를 주지 않았으면 일정 id와 캘린더 id로 만든다(eid).

    그날 화면 주소는 마지막 수단이다. 사용자는 날짜가 아니라 그 일정을 보고 싶어 한다(11/4).
    """
    if row.get("link") and "/r/day/" not in row["link"]:
        return row["link"]
    if row.get("id"):
        try:
            raw = f"{str(row['id']).split('@')[0]} {calendar_id()}".encode()
            return "https://www.google.com/calendar/event?eid=" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
        except (BridgeError, httpx.HTTPError, KeyError, SetupError):  # 연결 설정이 없어도 날짜 화면으로는 보낸다
            pass
    return day_link(day) if day else ""


def find_event(rows: list[dict], key: str) -> dict | None:
    return next((row for row in rows if row.get("key") == key), None)


def day_link(day: str) -> str:
    """일정 주소를 모를 때 쓰는 그날 화면 주소."""
    return f"https://calendar.google.com/calendar/r/day/{day.replace('-', '/')}"


def add_event(key: str, title: str, day: str, clock: str, notes: str, minutes: int = 30,
              reminders: list[int] | None = None, rsvp: bool = True) -> dict:
    """일정을 넣고 {created, event:{id, link, status...}}를 돌려준다. 같은 key는 한 번만 생긴다."""
    start, end, all_day = event_times(day, clock, minutes)
    result = call("calendar_add", key=key, title=title, start=start, end=end, allDay=all_day, notes=notes,
                  reminders=reminders if reminders is not None else DEADLINE_REMINDERS,
                  rsvp=rsvp and version() >= 2)
    event = result.setdefault("event", {})
    event["link"] = event_link(event, day)
    return result


def list_events(first: str, last: str) -> list[dict]:
    """first~last(포함) 날짜의 일정."""
    start = datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=KST)
    end = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=KST) + timedelta(days=1)
    return call("calendar_list", **{"from": start.isoformat(), "to": end.isoformat()})


def set_rsvp(event_id: str, status: str) -> dict:
    if status not in RSVP_STATUSES:
        raise ValueError(status)
    if version() < 2:
        raise BridgeError("Apps Script를 2판으로 올려야 참석 응답을 바꿀 수 있음 (setup/update-google.sh)")
    return call("calendar_rsvp", id=event_id, status=status)
