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

from .config import ACTIONS_FILE, ANALYSIS_DIR, HEARTBEAT_DIR, NOTICES, REMINDERS_FILE, SEEN_FILE, USAGE_DIR

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


def closed_items() -> set[int | str]:
    """완료·패스로 표시한 글. 마감 독촉을 멈춘다. 마지막 표시가 우선이다."""
    state: dict[int | str, str] = {}
    for row in read_jsonl(ACTIONS_FILE):
        state[row["id"]] = row["action"]
    return {key for key, action in state.items() if action in ("done", "skip")}


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
    rows = read_jsonl(USAGE_DIR / f"{now():%Y-%m}.jsonl")
    return sum(row["usd"] for row in rows if kind is None or row["kind"] == kind)


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
