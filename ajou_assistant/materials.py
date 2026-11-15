"""스레드의 자료를 기록으로 남긴다 ([📎 자료 남기기] 버튼, v0.6.0).

자동으로 잡지 않는다. 사용자가 버튼을 누른 스레드만, 사용자가 쓴 글의 원문과 올리거나 만든 파일 목록을
records/materials/에 한 장으로 남긴다. 파일 자체는 스레드 폴더(outputs/threads/<번호>/)에 그대로 있고 git이
함께 올린다. 다시 누르면 그 뒤에 쓴 글까지 넣어 같은 장을 고쳐 쓰고, 취소하면 그 장만 지운다.

11/15 사용자 결정: 일정이 끝나면 먼저 묻던 기록 요청(followup.py)을 없애고 이 버튼으로 바꿨다. 기록 요청은
11일 동안 20번 물어 3번 답을 받았고, 그중 두 번은 강의 자료 파일이었는데 파일을 저장하지 않았다.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import HOME_DIR
from .record import LOG_NAME

MATERIALS_DIR = HOME_DIR / "records" / "materials"
HEADING = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (.+)$", re.M)
SKIP_PARTS = {"notice"}  # 공지 원문은 공지 기록에 있다. 스레드가 받아 둔 사본은 목록에 넣지 않는다.


def user_messages(log_text: str) -> list[tuple[str, str]]:
    """log.md에서 사용자가 쓴 글만 (시각, 글)로 뽑는다."""
    marks = list(HEADING.finditer(log_text))
    found = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(log_text)
        if mark.group(2).strip() == "나":
            text = log_text[mark.end():end].strip()
            if text:
                found.append((mark.group(1), text))
    return found


def files_in(folder: Path) -> list[str]:
    """스레드 폴더의 파일(올린 것, 만든 것). 데이터 폴더 기준 경로."""
    if not folder.exists():
        return []
    return [str(path.relative_to(HOME_DIR)) for path in sorted(folder.rglob("*"))
            if path.is_file() and path.name != LOG_NAME and not SKIP_PARTS & set(path.relative_to(folder).parts)]


def page_path(title: str, first_day: str) -> Path:
    slug = re.sub(r"[^\w가-힣]+", "_", title).strip("_")[:40] or "자료"
    return MATERIALS_DIR / f"{first_day}_{slug}.md"


def save(folder: Path, title: str, link: str, existing: str = "") -> Path | None:
    """한 장을 (다시) 쓴다. 남길 글도 파일도 없으면 None. existing은 전에 남긴 장의 데이터 폴더 기준 경로."""
    log = folder / LOG_NAME
    messages = user_messages(log.read_text(encoding="utf-8")) if log.exists() else []
    files = files_in(folder)
    if not messages and not files:
        return None
    path = HOME_DIR / existing if existing else page_path(title, (messages[0][0] if messages else "")[:10] or "날짜없음")
    lines = [f"# {title}", "", f"- Discord 스레드: {link}", f"- 스레드 폴더: {folder.relative_to(HOME_DIR)}",
             "- 사용자가 [📎 자료 남기기]를 눌러 남긴 장이다. 글은 스레드에 쓴 그대로 옮겼다.", ""]
    if files:
        lines += ["## 파일", ""] + [f"- {name}" for name in files] + [""]
    for stamp, text in messages:
        lines += [f"## {stamp}", "", text, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def remove(relative: str) -> bool:
    path = HOME_DIR / relative
    if path.is_file() and MATERIALS_DIR in path.parents:
        path.unlink()
        return True
    return False
