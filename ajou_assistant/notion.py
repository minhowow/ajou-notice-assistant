"""개인 Notion에 '내가 한 일'을 남긴다 (v0.8.0, 11/17 사용자 결정).

- 목적: 나중에 내가 무엇을 했는지 보는 기록이다. 공지·할 일 관리는 Discord와 캘린더가 맡고 Notion에 넣지 않는다
  (11/17 검토: 지나간 공지로 가득 차면 정작 내 기록이 묻히고, 같은 것을 두 군데서 관리하게 된다). 공지는 내가
  행동했을 때(지원·참석·결과)만 기록이 된다.
- 구조: 통합에 연결한 페이지(기본 이름 '아주대 비서') 아래 데이터베이스 둘.
  📒 기록은 하루하루 한 일, 🧭 경험은 프로젝트·활동·지원 같은 묶음이다. 기록은 경험에 이어진다.
- 쓰는 방법: Notion 공식 API와 내부 통합 토큰(NOTION_TOKEN). 통합은 연결한 페이지 안에만 쓸 수 있다.
  같은 키(키 칸)는 한 줄만 만들고, 이미 있으면 속성만 고친다. 본문은 처음 만들 때만 쓰고 사용자가 고친 본문은
  덮어쓰지 않는다.
- 묻지 않는다: 이미 일어나는 일(대화 기록, 자료 남기기, 녹음)에서 쌓는다. 지운 기록 요청(11일 20번 물어 3번 답)의 교훈이다.
- 토큰이 없으면 아무것도 하지 않는다. Notion이 실패해도 알림·대화·기록(git)은 그대로 간다.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import httpx

from .config import STATE_DIR, load_secrets, setting

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
ROOT_TITLE = setting("AJOU_NOTION_ROOT", "아주대 비서")
LOG_TITLE, EXP_TITLE = "📒 기록", "🧭 경험"
KINDS = ["특강·행사", "대화·결정", "지원·결과", "작업", "회의", "회고", "자료·녹음"]
AREAS = ["학업", "장학·진로", "국제교류", "활동", "창업·개발", "생활"]
STATES = ["진행", "완료", "중단"]
STATE_FILE = STATE_DIR / "notion.json"
TEXT_LIMIT = 1900  # Notion 글 조각 하나의 한도는 2000자
CATEGORY_AREA = {"scholarship": "장학·진로", "career": "장학·진로", "international": "국제교류", "academic": "학업",
                 "activity": "활동", "campus_life": "생활"}


class NotionError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(load_secrets().get("NOTION_TOKEN"))


class Notion:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        token = token or load_secrets().get("NOTION_TOKEN", "")
        self.client = client or httpx.Client(base_url=API, timeout=30, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"})

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        for _ in range(5):
            response = self.client.request(method, path, json=payload)
            if response.status_code == 429:  # 초당 3번 남짓이 한도다
                time.sleep(float(response.headers.get("Retry-After", "1")) + 0.2)
                continue
            if response.status_code >= 400:
                raise NotionError(f"Notion {response.status_code}: {response.text[:300]}")
            return response.json()
        raise NotionError("Notion 429: 재시도 초과")

    # ── 페이지와 데이터베이스 찾기·만들기 ─────────────────────────

    def find_root(self) -> str:
        """통합에 연결된 페이지 중 이름이 ROOT_TITLE인 것. 연결된 페이지가 하나뿐이면 그 페이지."""
        rows = self.request("POST", "/search", {"filter": {"property": "object", "value": "page"}, "page_size": 50}).get("results", [])
        pages = [(row["id"], page_title(row)) for row in rows if row.get("parent", {}).get("type") in ("workspace", "page_id")]
        exact = [page for page in pages if page[1].strip() == ROOT_TITLE]
        if exact:
            return exact[0][0]
        if len(pages) == 1:
            return pages[0][0]
        raise NotionError(f"통합에 연결된 '{ROOT_TITLE}' 페이지가 없습니다. 페이지 ··· → 연결에서 통합을 추가하세요.")

    def child_databases(self, page_id: str) -> dict[str, str]:
        found, cursor = {}, None
        while True:
            suffix = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self.request("GET", f"/blocks/{page_id}/children{suffix}")
            for block in data.get("results", []):
                if block.get("type") == "child_database":
                    found[block["child_database"]["title"]] = block["id"]
            if not data.get("has_more"):
                return found
            cursor = data.get("next_cursor")

    def ensure(self) -> dict:
        """데이터베이스 둘이 없으면 만든다. {root, log, exp}. 여러 번 불러도 결과가 같다."""
        root = self.find_root()
        existing = self.child_databases(root)
        exp = existing.get(EXP_TITLE) or self.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": root}, "title": rich(EXP_TITLE), "is_inline": False,
            "properties": {
                "이름": {"title": {}}, "기간": {"date": {}}, "역할": {"rich_text": {}},
                "상태": {"select": {"options": [{"name": name} for name in STATES]}},
                "영역": {"select": {"options": [{"name": name} for name in AREAS]}},
                "한 줄 성과": {"rich_text": {}}, "키": {"rich_text": {}},
            }})["id"]
        log = existing.get(LOG_TITLE)
        if not log:
            log = self.request("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": root}, "title": rich(LOG_TITLE), "is_inline": False,
                "properties": {
                    "이름": {"title": {}}, "날짜": {"date": {}},
                    "종류": {"select": {"options": [{"name": name} for name in KINDS]}},
                    "영역": {"select": {"options": [{"name": name} for name in AREAS]}},
                    "경험": {"relation": {"database_id": exp, "type": "dual_property", "dual_property": {}}},
                    "요약": {"rich_text": {}}, "원문": {"url": {}}, "비서가 씀": {"checkbox": {}}, "키": {"rich_text": {}},
                }})["id"]
            # 경험 쪽에 생긴 반대 방향 칸 이름을 '기록'으로 바꾼다.
            props = self.request("GET", f"/databases/{exp}")["properties"]
            back = next((name for name, prop in props.items() if prop.get("type") == "relation" and name != "기록"), None)
            if back:
                self.request("PATCH", f"/databases/{exp}", {"properties": {back: {"name": "기록"}}})
        ids = {"root": root, "log": log, "exp": exp}
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(ids))
        return ids

    def ids(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return self.ensure()

    # ── 한 줄 쓰기 ───────────────────────────────────────────────

    def find_key(self, database: str, key: str) -> str | None:
        rows = self.request("POST", f"/databases/{database}/query",
                            {"filter": {"property": "키", "rich_text": {"equals": key}}, "page_size": 1}).get("results", [])
        return rows[0]["id"] if rows else None

    def upsert(self, database: str, key: str, properties: dict, blocks: list[dict] | None = None) -> tuple[str, bool]:
        """(페이지 번호, 새로 만들었는지). 있으면 속성만 고치고 본문은 건드리지 않는다."""
        properties = {**properties, "키": {"rich_text": rich(key)}}
        found = self.find_key(database, key)
        if found:
            self.request("PATCH", f"/pages/{found}", {"properties": properties})
            return found, False
        blocks = blocks or []
        page = self.request("POST", "/pages", {"parent": {"database_id": database}, "properties": properties,
                                               "children": blocks[:100]})
        for start in range(100, len(blocks), 100):
            self.request("PATCH", f"/blocks/{page['id']}/children", {"children": blocks[start:start + 100]})
        return page["id"], True


# ── 모양 만들기 ─────────────────────────────────────────────────

def rich(text: str) -> list[dict]:
    """긴 글은 2000자 한도에 맞게 나눈다."""
    text = str(text or "")
    return [{"type": "text", "text": {"content": text[start:start + TEXT_LIMIT]}} for start in range(0, len(text), TEXT_LIMIT)] or []


def page_title(row: dict) -> str:
    for prop in row.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    return ""


def block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text), **extra}}


def heading(text: str) -> dict:
    return block("heading_3", text)


def bullets(lines: list[str]) -> list[dict]:
    return [block("bulleted_list_item", line) for line in lines if line]


def paragraphs(text: str) -> list[dict]:
    """긴 글을 문단 블록들로. 빈 줄로 나누고, 한 문단이 길면 잘라 여러 블록으로."""
    found = []
    for part in re.split(r"\n\s*\n", str(text or "").strip()):
        part = part.strip()
        for start in range(0, len(part), TEXT_LIMIT):
            found.append(block("paragraph", part[start:start + TEXT_LIMIT]))
    return found


def select(name: str | None) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def link_from(source: str) -> str | None:
    found = re.search(r"\((https://discord\.com/[^)\s]+)\)", source or "")
    return found.group(1) if found else None


# ── 대화 기록 → 📒 기록 ─────────────────────────────────────────

def record_area(row: dict, categories: dict) -> str | None:
    """결정이 걸린 공지의 분류를 먼저 보고, 없으면 사실의 영역을 본다."""
    for decision in row.get("decisions", []):
        area = CATEGORY_AREA.get(categories.get(str(decision.get("notice_id", ""))))
        if area:
            return area
    for fact in row.get("facts", []):
        if fact.get("area") in AREAS:
            return fact["area"]
    return None


def record_page(row: dict, categories: dict | None = None) -> tuple[str, dict, list[dict]]:
    """records/data의 한 줄을 (키, 속성, 본문)으로."""
    applied = any(decision.get("status") in ("applied", "done") for decision in row.get("decisions", []))
    moment = datetime.fromisoformat(row["at"])
    properties = {
        "이름": {"title": rich(row.get("title") or row.get("summary", "")[:60] or "대화")},
        "날짜": {"date": {"start": moment.isoformat()}},
        "종류": select("지원·결과" if applied else "대화·결정"),
        "영역": select(record_area(row, categories or {})),
        "요약": {"rich_text": rich(row.get("summary", ""))},
        "원문": {"url": link_from(row.get("source", ""))},
        "비서가 씀": {"checkbox": True},
    }
    blocks: list[dict] = []
    if row.get("decisions"):
        blocks += [heading("결정")] + bullets([f"{d['about']} → {d['decision']}" for d in row["decisions"]])
    if row.get("facts"):
        blocks += [heading("남길 사실")] + bullets([f["fact"] for f in row["facts"]])
    if row.get("todos"):
        blocks += [heading("할 일")] + [block("to_do", t["task"] + (f" (기한 {t['due']})" if t.get("due") else ""), checked=False)
                                      for t in row["todos"]]
    if row.get("events"):
        blocks += [heading("일정")] + bullets([f"{e['date']} {e['time']} {e['title']}".strip() for e in row["events"]])
    return f"record:{row.get('thread', '')}:{row['at']}", properties, blocks


def add_record(row: dict, categories: dict | None = None, notion: Notion | None = None) -> str | None:
    """대화 기록 한 토막을 📒 기록에 남긴다. 실패해도 예외를 밖으로 내지 않는다."""
    if not enabled():
        return None
    try:
        notion = notion or Notion()
        key, properties, blocks = record_page(row, categories)
        page, _ = notion.upsert(notion.ids()["log"], key, properties, blocks)
        return page
    except (NotionError, httpx.HTTPError, KeyError, ValueError) as error:
        print(f"Notion 기록 실패: {type(error).__name__}: {str(error)[:200]}", flush=True)
        return None


def add_entry(key: str, title: str, when: datetime, kind: str, summary: str = "", body: str = "", link: str = "",
              area: str | None = None, notion: Notion | None = None) -> str | None:
    """자료 남기기·녹음·회고처럼 대화 기록이 아닌 한 줄."""
    if not enabled():
        return None
    try:
        notion = notion or Notion()
        properties = {"이름": {"title": rich(title[:200])}, "날짜": {"date": {"start": when.isoformat()}},
                      "종류": select(kind if kind in KINDS else None), "영역": select(area if area in AREAS else None),
                      "요약": {"rich_text": rich(summary)}, "원문": {"url": link or None}, "비서가 씀": {"checkbox": True}}
        page, _ = notion.upsert(notion.ids()["log"], key, properties, paragraphs(body))
        return page
    except (NotionError, httpx.HTTPError, KeyError, ValueError) as error:
        print(f"Notion 기록 실패: {type(error).__name__}: {str(error)[:200]}", flush=True)
        return None
