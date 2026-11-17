"""v0.8.0 개인 Notion 기록: 데이터베이스 만들기, 같은 키는 한 줄, 대화 기록 옮기기."""
import json

import pytest

from ajou_assistant import notion


class FakeNotion(notion.Notion):
    """요청을 기억하는 가짜. 실제 API 모양으로 답한다."""

    def __init__(self):
        self.calls, self.pages, self.dbs, self.children = [], {}, {}, []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/search":
            return {"results": [{"id": "root", "object": "page", "parent": {"type": "workspace"},
                                 "properties": {"title": {"type": "title", "title": [{"plain_text": "아주대 비서"}]}}}]}
        if path.startswith("/blocks/root/children"):
            return {"results": [{"type": "child_database", "id": db_id, "child_database": {"title": title}}
                                for title, db_id in self.dbs.items()], "has_more": False}
        if method == "POST" and path == "/databases":
            title = payload["title"][0]["text"]["content"]
            self.dbs[title] = f"db{len(self.dbs)}"
            return {"id": self.dbs[title]}
        if method == "GET" and path.startswith("/databases/"):
            return {"properties": {"이름": {"type": "title"}, "Related to 📒 기록 (경험)": {"type": "relation"}}}
        if method == "POST" and path.endswith("/query"):
            key = payload["filter"]["rich_text"]["equals"]
            return {"results": [{"id": page} for page, props in self.pages.items() if props["키"]["rich_text"][0]["text"]["content"] == key]}
        if method == "POST" and path == "/pages":
            page = f"p{len(self.pages)}"
            self.pages[page] = payload["properties"]
            self.children.append(payload["children"])
            return {"id": page}
        return {}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(notion, "STATE_FILE", tmp_path / "notion.json")
    return tmp_path


def test_ensure_creates_two_databases_once_and_names_the_back_relation(state):
    fake = FakeNotion()
    ids = fake.ensure()
    assert ids == {"root": "root", "log": "db1", "exp": "db0"} and list(fake.dbs) == ["🧭 경험", "📒 기록"]
    log_call = next(payload for method, path, payload in fake.calls if method == "POST" and path == "/databases"
                    and payload["title"][0]["text"]["content"] == "📒 기록")
    assert log_call["properties"]["경험"]["relation"]["database_id"] == "db0"
    assert ("PATCH", "/databases/db0", {"properties": {"Related to 📒 기록 (경험)": {"name": "기록"}}}) in fake.calls
    fake.calls.clear()
    assert fake.ensure() == ids and not [call for call in fake.calls if call[1] == "/databases"]  # 두 번째는 만들지 않는다
    assert json.loads((state / "notion.json").read_text())["log"] == "db1"


def test_same_key_makes_one_page_and_later_updates_only_properties(state):
    fake = FakeNotion()
    page, created = fake.upsert("db1", "record:1:2026-11-02T20:50", {"이름": {"title": notion.rich("첫 제목")}}, notion.paragraphs("본문"))
    assert created and fake.children[0][0]["paragraph"]["rich_text"][0]["text"]["content"] == "본문"
    again, created = fake.upsert("db1", "record:1:2026-11-02T20:50", {"이름": {"title": notion.rich("고친 제목")}}, notion.paragraphs("다른 본문"))
    assert again == page and not created and len(fake.children) == 1  # 본문은 다시 쓰지 않는다(사용자가 고쳤을 수 있다)
    assert ("PATCH", f"/pages/{page}") in [(method, path) for method, path, _ in fake.calls]


def test_conversation_record_becomes_a_log_page():
    row = {"at": "2026-11-02T20:50:23+09:00", "thread": "156", "title": "[앵커] 랜선 톡톡 신청",
           "source": "[log.md](../x) · [Discord](https://discord.com/channels/1/156)", "summary": "4회 신청 완료",
           "decisions": [{"about": "랜선 톡톡", "decision": "4회 신청", "notice_id": "377587", "status": "applied"}],
           "facts": [], "todos": [{"task": "줌 링크 확인", "due": "2026-11-03", "owner": "me"}], "events": [], "questions": []}
    key, props, blocks = notion.record_page(row, {"377587": "career"})
    assert key == "record:156:2026-11-02T20:50:23+09:00"
    assert props["종류"]["select"]["name"] == "지원·결과" and props["영역"]["select"]["name"] == "장학·진로"
    assert props["원문"]["url"] == "https://discord.com/channels/1/156" and props["비서가 씀"]["checkbox"] is True
    kinds = [block["type"] for block in blocks]
    assert kinds == ["heading_3", "bulleted_list_item", "heading_3", "to_do"]


def test_long_text_is_split_under_the_notion_limit():
    pieces = notion.rich("가" * 4000)
    assert len(pieces) == 3 and all(len(piece["text"]["content"]) <= 2000 for piece in pieces)
    assert len(notion.paragraphs("가" * 4000)) == 3


def test_nothing_happens_without_a_token(monkeypatch):
    monkeypatch.setattr(notion, "load_secrets", lambda: {})
    assert not notion.enabled() and notion.add_record({"at": "2026-11-02T20:50:23+09:00"}) is None
