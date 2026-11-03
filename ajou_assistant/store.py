"""상태는 사람이 읽고 git으로 병합할 수 있는 JSONL로 둔다. 맥과 서버가 같은 기록을 잇는다.

전원이 끊기면 마지막 줄이 잘릴 수 있다. 잘린 줄 하나 때문에 이후 실행이 전부 죽으면 안 되므로
읽을 때는 건너뛰고, 쓸 때는 앞줄이 끝나지 않았으면 줄을 바꾼 뒤 디스크까지 내려 쓴다.
건너뛴 글은 처음 보는 글로 취급되어 다시 처리된다. 중복 알림이 누락보다 낫다.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import ACTIONS_FILE, ANALYSIS_DIR, HEARTBEAT_DIR, NOTICES, REMINDERS_FILE, REPOSTS_FILE, SEEN_FILE, USAGE_DIR

KST = timezone(timedelta(hours=9))


def now() -> datetime:
    return datetime.now(KST)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"잘린 줄을 건너뜁니다: {path.name}: {line[:80]}")
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab+") as handle:
        prefix = b""
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = b"\n"
        handle.write(prefix + json.dumps(row, ensure_ascii=False).encode() + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_seen(path: Path = SEEN_FILE) -> dict[int | str, dict]:
    """같은 글의 줄이 여러 개면 뒤의 값으로 덮어쓴다. 병합으로 순서가 섞여도 시각으로 정렬한다."""
    rows = read_jsonl(path)
    rows.sort(key=lambda row: row.get("at", ""))
    seen: dict[int | str, dict] = {}
    for row in rows:
        if "id" in row:
            seen[row["id"]] = {**seen.get(row["id"], {}), **row}
    return seen


def append_seen(row: dict, path: Path = SEEN_FILE) -> None:
    append_jsonl(path, {**row, "at": now().isoformat(timespec="seconds")})


def append_analysis(row: dict) -> None:
    append_jsonl(ANALYSIS_DIR / f"{now():%Y-%m}.jsonl", row)


def load_analysis(month: str) -> list[dict]:
    return read_jsonl(ANALYSIS_DIR / f"{month}.jsonl")


def recent_analysis(months: int = 3) -> list[dict]:
    """최근 몇 달의 분석을 글별 최신 한 건으로 돌려준다."""
    latest: dict[int | str, dict] = {}
    current = now().date().replace(day=1)
    keys = []
    for _ in range(months):
        keys.append(f"{current:%Y-%m}")
        current = (current - timedelta(days=1)).replace(day=1)
    for key in reversed(keys):
        for row in load_analysis(key):
            latest[row["id"]] = row
    return list(latest.values())


def load_reposts() -> dict[int | str, dict]:
    """재공지 → {"parent": 이 글이 되풀이한 이전 공지, "changes": 바뀐 점}. 처음 공지는 repost.root_of로 찾는다."""
    return {row["id"]: row for row in read_jsonl(REPOSTS_FILE) if "id" in row}


def add_repost(article_id: int | str, parent: int | str, changes: str, via: str) -> None:
    append_jsonl(REPOSTS_FILE, {"id": article_id, "parent": parent, "changes": changes, "via": via,
                                "at": now().isoformat(timespec="seconds")})


RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def posted_order(row: dict) -> tuple[str, str]:
    """게시일, 같으면 분석 시각 순. 옛 글을 다시 분석해도 새로 올라온 재공지보다 앞에 둔다."""
    return str(row.get("date", "")), str(row.get("analyzed_at", ""))


def notice_groups(rows: list[dict] | None = None) -> list[dict]:
    """재공지를 처음 공지에 묶어 묶음마다 한 건을 돌려준다. 마감 독촉·요약·캘린더가 같은 공지를 두 번 다루지 않게.

    날짜와 자격은 가장 늦게 올라온 글의 분석을 따른다(마감 연장을 반영). 관련도는 묶음에서 가장 높은 것을 쓴다.
    돌려주는 행의 `root`가 처음 공지 번호, `members`가 묶음의 모든 번호다.
    """
    from .repost import root_of

    rows = recent_analysis() if rows is None else rows
    links = load_reposts()
    groups: dict[int | str, dict] = {}
    for row in sorted(rows, key=posted_order):
        root = root_of(row["id"], links)
        previous = groups.get(root)
        merged = {**row, "root": root, "members": (previous["members"] if previous else set()) | {row["id"]}}
        if previous and RANK[previous["analysis"]["relevance"]] > RANK[row["analysis"]["relevance"]]:
            merged["analysis"] = {**row["analysis"], "relevance": previous["analysis"]["relevance"]}
        groups[root] = merged
    return list(groups.values())


def item_actions() -> dict[int | str, str]:
    """글마다 마지막 표시(done, skip, open, track)."""
    state: dict[int | str, str] = {}
    for row in read_jsonl(ACTIONS_FILE):
        state[row["id"]] = row["action"]
    return state


def closed_items() -> set[int | str]:
    """완료·패스로 표시한 글. 마감 독촉을 멈춘다. 마지막 표시가 우선이다."""
    return {key for key, action in item_actions().items() if action in ("done", "skip")}


def tracked_items() -> set[int | str]:
    """사용자가 '계속 추적'하라고 한 글. 관련도가 보통이어도 높음처럼 마감 독촉하고 일일요약에 남긴다."""
    return {key for key, action in item_actions().items() if action == "track"}


def mark_item(article_id: int | str, action: str) -> None:
    append_jsonl(ACTIONS_FILE, {"id": article_id, "action": action, "at": now().isoformat(timespec="seconds")})


def reminded() -> set[str]:
    return {row["key"] for row in read_jsonl(REMINDERS_FILE)}


def mark_reminded(key: str) -> None:
    append_jsonl(REMINDERS_FILE, {"key": key, "at": now().isoformat(timespec="seconds")})


def add_usage(kind: str, amount: float, note: str = "") -> None:
    append_jsonl(USAGE_DIR / f"{now():%Y-%m}.jsonl",
                 {"kind": kind, "usd": round(amount, 4), "note": note, "at": now().isoformat(timespec="seconds")})


def month_usage(kind: str | None = None) -> float:
    """이번 달 API 비용. 종류를 안 주면 구독으로 돈 것(-plan, 실제로 돈이 나가지 않음)은 뺀다."""
    rows = read_jsonl(USAGE_DIR / f"{now():%Y-%m}.jsonl")
    if kind is None:
        return sum(row["usd"] for row in rows if not row["kind"].endswith("-plan"))
    return sum(row["usd"] for row in rows if row["kind"] == kind)


def write_heartbeat(summary: dict) -> None:
    """호스트별 파일이라 맥과 서버가 서로 충돌하지 않는다."""
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname().split(".")[0]
    path = HEARTBEAT_DIR / f"{host}.json"
    moment = now()
    previous = read_heartbeat(path)
    bucket = moment.replace(hour=moment.hour - moment.hour % 3, minute=0, second=0, microsecond=0)
    if previous.get("bucket") != bucket.isoformat() or summary.get("new"):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"bucket": bucket.isoformat(), "last_run": moment.isoformat(timespec="seconds"), **summary}, ensure_ascii=False) + "\n")
        temporary.replace(path)


def read_heartbeat(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def ensure_dirs() -> None:
    NOTICES.mkdir(parents=True, exist_ok=True)
