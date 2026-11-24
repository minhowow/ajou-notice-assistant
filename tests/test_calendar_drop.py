"""v0.8.2 '안 함'으로 표시한 공지의 앞으로의 일정은 캘린더에서 뺀다(11/24 사용자)."""
from datetime import date

from ajou_assistant import google_bridge, repost


def test_skip_removes_only_this_notice_groups_assistant_events(monkeypatch):
    rows = [
        {"id": "a", "key": "notice:379091:deadline:2026-11-24", "title": "[마감] 해동 교내 접수"},
        {"id": "b", "key": "notice:379090:event:2026-12-02", "title": "[일정] 서류심사"},      # 재공지 묶음
        {"id": "c", "key": "notice:3790910:event:2026-12-02", "title": "다른 공지(번호 앞부분만 같음)"},
        {"id": "d", "key": "", "title": "사용자가 직접 넣은 해동 메모"},
        {"id": "e", "key": "thread:1:2026-12-07:10", "title": "대화에서 넣은 일정"},
        {"id": "f", "key": "notice:r:66f6e670a17f146b:deadline:2026-11-24", "title": "채용"},
    ]
    removed = []
    monkeypatch.setattr(google_bridge, "configured", lambda: True)
    monkeypatch.setattr(google_bridge, "list_events", lambda first, last: rows)
    monkeypatch.setattr(google_bridge, "remove_event", lambda event_id: removed.append(event_id))
    monkeypatch.setattr(repost, "root_of", lambda key, links=None: 379090)
    monkeypatch.setattr(repost, "group_ids", lambda root, links=None: {379090, 379091})
    titles = google_bridge.drop_notice_events(379091, today=date(2026, 11, 24))
    assert removed == ["a", "b"] and titles == ["[마감] 해동 교내 접수", "[일정] 서류심사"]

    removed.clear()
    monkeypatch.setattr(repost, "root_of", lambda key, links=None: key)
    monkeypatch.setattr(repost, "group_ids", lambda root, links=None: {root})
    google_bridge.drop_notice_events("r:66f6e670a17f146b", today=date(2026, 11, 24))
    assert removed == ["f"]


def test_nothing_happens_without_the_bridge(monkeypatch):
    monkeypatch.setattr(google_bridge, "configured", lambda: False)
    assert google_bridge.drop_notice_events(1) == []
