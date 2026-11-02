"""학교 Google 계정의 Apps Script 연결. 설정하지 않았으면 조용히 건너뛴다.

setup/setup-google.sh가 배포 주소와 비밀값을 env에 넣는다. 캘린더 쓰기는 이 모듈의 함수로만 하고,
메일은 읽기만 한다. 스크립트 2판부터 일정에 나를 참석자로 넣어 캘린더에서 예/아니요/미정을 고를 수 있다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import httpx

from .config import load_secrets

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
def version() -> int:
    return int(call("ping").get("version", 1))


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


def remove_event(event_id: str) -> dict:
    """비서가 만든 일정(ajou_key가 있는 일정)만 지울 수 있다. 스크립트가 다시 확인한다."""
    if version() < 2:
        raise BridgeError("Apps Script를 2판으로 올려야 일정을 지울 수 있음")
    return call("calendar_remove", id=event_id)


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
    event.setdefault("link", "")
    if not event["link"]:
        event["link"] = day_link(day)
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
