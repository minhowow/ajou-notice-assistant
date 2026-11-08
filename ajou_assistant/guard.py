"""스레드 봇이 claude.ai 커넥터를 쓰기 전에 코드로 검사한다. Claude Code PreToolUse 훅으로 실행된다.

스레드는 공지 원문이나 붙여 넣은 문자처럼 믿을 수 없는 글을 읽는다. 그 글에 숨은 지시가 AI를 움직여도
되돌리기 어려운 일은 못 하게 여기서 막는다. 막으면 종료 코드 2와 이유를 돌려주고, Claude는 그 이유를
보고 사용자에게 설명한다.

- 캘린더: 보기, 만들기, 고치기, 참석 응답은 된다. 삭제는 막는다. 나 말고 다른 사람을 참석자로 넣는 것도
  막는다. 초대 메일이 나가면 일정 내용이 밖으로 나가기 때문이다.
- 노션: 읽기만 된다. 팀 노션은 함께 쓰는 공간이라 잘못 고치면 남의 문서가 바뀐다.
- 그 밖의 커넥터(Gmail 등): 막는다.
- 파일 쓰기: 쓰기는 스레드 폴더 안으로 제한되어 있지만, 그 안에서도 `.git`으로 시작하는 파일(`.gitattributes`로
  그 폴더의 git-crypt 암호화를 끌 수 있다), `.claude` 폴더, `CLAUDE.md`(다음 대화에 지시로 읽힌다), `.mcp.json`은
  쓰지 못하게 한다. 공지 속 숨은 지시가 남긴 파일이 나중에 규칙처럼 작동하는 것을 막는다(11/6 점검).

표준 라이브러리만 쓴다. `python -I guard.py`로 실행되어 저장소 코드를 불러오지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys

CALENDAR = "mcp__claude_ai_Google_Calendar__"
NOTION = "mcp__claude_ai_Notion__"
CALENDAR_ALLOWED = {
    "list_calendars", "list_events", "search_events", "get_event", "suggest_time",
    "create_event", "update_event", "respond_to_event",
}
NOTION_READ = {
    "notion-search", "notion-fetch", "notion-get-self", "notion-get-users", "notion-get-teams",
    "notion-get-comments", "notion-list-recent-pages", "notion-list-favorite-pages", "notion-list-shared-pages",
    "notion-list-private-pages", "notion-list-page-backlinks", "notion-query-data-sources",
    "notion-query-multiple-data-sources", "notion-query-meeting-notes",
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PEOPLE_KEYS = ("attendee", "guest", "invite")
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
FILE_MATCHER = "|".join(sorted(FILE_TOOLS))
RULE_FILES = {"claude.md", "claude.local.md", ".mcp.json"}


def tool_names() -> list[str]:
    """스레드 봇에 허용할 커넥터 도구. 이 목록 밖의 커넥터 도구는 dontAsk 모드에서 거부된다."""
    return [CALENDAR + name for name in sorted(CALENDAR_ALLOWED)] + [NOTION + name for name in sorted(NOTION_READ)]


def people(value, inside: bool = False) -> list[str]:
    """참석자 자리(attendees, guests 등)에 들어간 메일 주소만 모은다. 설명란의 주소는 세지 않는다."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += people(item, inside or any(word in str(key).lower() for word in PEOPLE_KEYS))
    elif isinstance(value, list):
        for item in value:
            found += people(item, inside)
    elif isinstance(value, str) and inside:
        found += [match.group(0).lower() for match in EMAIL.finditer(value)]
    return found


def rule_path(path: str) -> bool:
    """git이나 Claude Code가 설정·지시로 읽는 파일이면 True. 경로 어느 단계든 본다."""
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    return any(part.lower().startswith((".git", ".claude")) for part in parts) or (
        bool(parts) and parts[-1].lower() in RULE_FILES)


def verdict(tool: str, arguments: dict, owners: set[str]) -> str | None:
    """막을 이유를 돌려준다. 허용이면 None."""
    if tool in FILE_TOOLS:
        path = arguments.get("file_path") or arguments.get("notebook_path") or ""
        if rule_path(path):
            return ("git 설정(.git*), Claude 설정(.claude), CLAUDE.md, .mcp.json은 스레드에서 쓰지 않습니다. "
                    "필요하면 사용자에게 맥 CLI에서 고치라고 안내하세요.")
        return None
    if tool.startswith(CALENDAR):
        name = tool[len(CALENDAR):]
        if name == "delete_event":
            return "비서는 캘린더 일정을 지우지 않습니다. 지울 일정의 제목과 날짜를 사용자에게 알려 직접 지우게 하세요."
        if name not in CALENDAR_ALLOWED:
            return f"허용하지 않은 캘린더 기능입니다: {name}"
        others = sorted({email for email in people(arguments) if email not in owners})
        if others:
            return ("다른 사람을 참석자로 넣으면 초대 메일이 나가므로 막았습니다: " + ", ".join(others)
                    + ". 참석자 없이 다시 만들고, 초대가 필요하면 사용자에게 직접 하라고 안내하세요.")
        return None
    if tool.startswith(NOTION):
        name = tool[len(NOTION):]
        if name not in NOTION_READ:
            return "노션은 읽기만 합니다. 고칠 내용은 사용자에게 초안으로 보여 주세요."
        return None
    if tool.startswith("mcp__"):
        return "이 스레드에서 허용하지 않은 커넥터입니다."
    return None


def owners_from_env() -> set[str]:
    return {value.strip().lower() for value in os.environ.get("AJOU_OWNER_EMAILS", "").split(",") if value.strip()}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("훅 입력을 읽지 못해 막았습니다.", file=sys.stderr)
        return 2
    reason = verdict(str(event.get("tool_name", "")), event.get("tool_input") or {}, owners_from_env())
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
