"""스레드 턴이 도는 동안 비서가 무엇을 하는지 Discord에 실시간으로 보여 준다.

Claude Code를 `--output-format stream-json --verbose`로 돌리면 도구를 쓸 때마다 사건이 한 줄씩 나온다.
steps()가 사건을 짧은 한국어 줄로 바꾸고, Progress가 스레드의 상태 메시지 하나를 몇 초에 한 번까지만 고친다
(Discord 수정 한도). 턴이 끝나면 상태 메시지는 한 줄 요약으로 줄어든다.

경로, 검색어, 일정 제목처럼 무엇을 하는지만 보여 주고 파일 내용이나 도구 결과는 옮기지 않는다.
상태 표시는 덤이다. Discord 수정이 실패해도 턴은 그대로 진행한다.
"""
from __future__ import annotations

import asyncio
import time

import discord

KEEP_LINES = 8
EDIT_SECONDS = 2.0
TICK_SECONDS = 15.0
TEXT_CHARS = 90
CALENDAR = "mcp__claude_ai_Google_Calendar__"
NOTION = "mcp__claude_ai_Notion__"
GMAIL = "mcp__claude_ai_Gmail__"
CALENDAR_WORDS = {
    "list_calendars": "캘린더 목록", "list_events": "일정 보기", "search_events": "일정 찾기", "get_event": "일정 보기",
    "suggest_time": "빈 시간 찾기", "create_event": "일정 만들기", "update_event": "일정 고치기",
    "respond_to_event": "참석 응답", "delete_event": "일정 지우기",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_path(path: str, home: str) -> str:
    """홈(데이터 폴더) 기준 상대경로로 줄이고, 길면 앞을 자른다."""
    path = str(path or "")
    home = home.rstrip("/") + "/"
    if path.startswith(home):
        path = path[len(home):]
    return path if len(path) <= 60 else "…" + path[-59:]


def tool_line(name: str, arguments: dict, home: str) -> str:
    arguments = arguments if isinstance(arguments, dict) else {}
    path = short_path(arguments.get("file_path") or arguments.get("path") or "", home)
    if name == "Read":
        return f"📄 읽기 {path}"
    if name == "Grep":
        where = f" · {path}" if path and path != "." else ""
        return f"🔎 찾기 '{_clip(arguments.get('pattern', ''), 40)}'{where}"
    if name == "Glob":
        return f"🔎 파일 찾기 {_clip(arguments.get('pattern', ''), 40)}"
    if name == "Write":
        return f"✏️ 쓰기 {path}"
    if name in ("Edit", "MultiEdit"):
        return f"✏️ 고치기 {path}"
    if name == "WebSearch":
        return f"🌐 웹 검색 '{_clip(arguments.get('query', ''), 50)}'"
    if name == "TodoWrite":
        return "📝 할 일 나누기"
    if name.startswith(CALENDAR):
        action = name[len(CALENDAR):]
        title = arguments.get("summary") or arguments.get("query") or ""
        return f"📅 {CALENDAR_WORDS.get(action, action)}" + (f" '{_clip(title, 40)}'" if title else "")
    if name.startswith(NOTION):
        action = name[len(NOTION):]
        query = arguments.get("query") or ""
        return ("🗂 노션 찾기" if "search" in action or "query" in action else "🗂 노션 읽기") + (
            f" '{_clip(query, 40)}'" if query else "")
    if name.startswith(GMAIL):
        query = arguments.get("query") or arguments.get("q") or ""
        return "✉️ 메일 " + ("찾기" if "search" in name or "list" in name else "읽기") + (
            f" '{_clip(query, 40)}'" if query else "")
    return f"🔧 {_clip(name, 50)}"


def steps(event: dict, home: str) -> list[str]:
    """stream-json 사건 하나를 상태 줄로 바꾼다. 보여 줄 것이 없으면 빈 목록."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "permission_denied":
        return [f"⛔ 권한 없음: {_clip(event.get('tool_name', ''), 40)}"]
    if kind != "assistant":
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    lines = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use":
            lines.append(tool_line(str(part.get("name", "")), part.get("input") or {}, home))
        elif part.get("type") == "text" and str(part.get("text", "")).strip():
            first = str(part["text"]).strip().splitlines()[0]
            lines.append(f"💬 {_clip(first, TEXT_CHARS)}")
    return lines


def elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}분 {seconds % 60}초" if seconds >= 60 else f"{seconds}초"


class Progress:
    """상태 메시지 하나. note()로 사건을 넣고, 바뀐 것이 있으면 EDIT_SECONDS마다 고친다.

    사건이 없어도 TICK_SECONDS마다 걸린 시간을 고쳐 멈춘 것인지 도는 것인지 알 수 있게 한다.
    """

    def __init__(self, channel, home: str, view: discord.ui.View | None = None,
                 edit_seconds: float = EDIT_SECONDS, tick_seconds: float = TICK_SECONDS) -> None:
        self.channel = channel
        self.home = home
        self.view = view
        self.edit_seconds = edit_seconds
        self.tick_seconds = tick_seconds
        self.lines: list[str] = []
        self.count = 0
        self.started = time.monotonic()
        self.shown_at = 0.0
        self.dirty = False
        self.message = None
        self.task: asyncio.Task | None = None

    def note(self, event: dict) -> None:
        for line in steps(event, self.home):
            self.add(line)

    def add(self, line: str) -> None:
        self.lines.append(line)
        self.count += 1
        self.dirty = True

    def render(self) -> str:
        head = f"⏳ 작업 중 · {elapsed(time.monotonic() - self.started)}"
        shown = self.lines[-KEEP_LINES:]
        hidden = len(self.lines) - len(shown)
        body = ([f"… 앞 단계 {hidden}개"] if hidden else []) + shown
        return "\n".join([head, *body])[:1990]

    async def start(self) -> None:
        try:
            self.message = await self.channel.send(self.render(), view=self.view, silent=True,
                                                   allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        except discord.HTTPException:
            self.message = None
            return
        self.shown_at = time.monotonic()
        self.task = asyncio.create_task(self.refresh())

    async def refresh(self) -> None:
        while True:
            await asyncio.sleep(self.edit_seconds)
            if self.dirty or time.monotonic() - self.shown_at >= self.tick_seconds:
                await self.show(self.render())

    async def show(self, text: str, **extra) -> None:
        if self.message is None:
            return
        self.dirty = False
        self.shown_at = time.monotonic()
        try:
            await self.message.edit(content=text, **extra)
        except discord.HTTPException:
            pass

    async def finish(self, mark: str = "✅ 끝") -> None:
        """한 줄 요약으로 줄이고 버튼을 뗀다."""
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.view is not None:
            self.view.stop()  # discord.py가 들고 있는 버튼 처리기를 놓는다.
        done = f"{mark} · {elapsed(time.monotonic() - self.started)}" + (f" · 단계 {self.count}개" if self.count else "")
        await self.show(done, view=None)
