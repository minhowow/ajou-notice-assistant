"""AWS 연동 (v0.5.0): 꺼져 있으면 아무것도 안 함, 공용 테이블 일치율, 현황 집계, 백업."""
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone

from ajou_assistant import cloud
from ajou_assistant.scrape import ListItem

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 9, 10, 7, tzinfo=timezone.utc)


def item(board: str, number, url="https://job.ajou.ac.kr/x.aspx") -> ListItem:
    return ListItem(number, board, url, f"제목 {number}", "", "", "2026-11-09")


class FakeTable:
    def __init__(self, rows: dict):
        self.rows = rows
        self.calls = []

    def update_item(self, **call):
        self.calls.append(call)
        key = (call["Key"]["source"]["S"], call["Key"]["id"]["S"])
        old = self.rows.get(key)
        return {"Attributes": old} if old else {}


def test_disabled_without_region(monkeypatch):
    monkeypatch.setattr(cloud, "setting", lambda name, default="": "")
    assert not cloud.enabled()
    cloud.after_run(datetime.now(KST), {1: item("general", 1)}, [], 0.0)  # 아무 호출도 하지 않고 끝난다


def test_share_compares_only_boards_the_lambda_reaches(monkeypatch):
    monkeypatch.setattr(cloud, "setting", lambda name, default="": "ajou-assistant-notices" if name == "AJOU_AWS_TABLE" else "")
    fresh = {"lambda_seen": {"S": "2026-11-09T10:05:00Z"}}
    stale = {"lambda_seen": {"S": "2026-11-09T06:05:00Z"}}
    db = FakeTable({("ajou.ac.kr#recruit", "r:1"): fresh, ("ajou.ac.kr#recruit", "r:2"): stale,
                    ("ajou.ac.kr#general", "7"): {"home_seen": {"S": "x"}}})
    items = {"r:1": item("recruit", "r:1"), "r:2": item("recruit", "r:2"), "r:3": item("recruit", "r:3"),
             7: item("general", 7, "https://www.ajou.ac.kr/kr/ajou/notice.do")}
    result = cloud.share(items, NOW, db)
    # general은 Lambda가 닿지 않는 게시판이라 비교에서 빠진다. recruit 3건 중 2시간 안에 본 것은 1건.
    assert result == {"shared": 4, "compared": 3, "matched": 1, "lambda_boards": 1}
    call = next(call for call in db.calls if call["Key"]["id"]["S"] == "7")
    assert call["Key"]["source"]["S"] == "ajou.ac.kr#general" and "home_seen = :now" in call["UpdateExpression"]
    assert "if_not_exists(first_seen" in call["UpdateExpression"] and call["ReturnValues"] == "ALL_OLD"


def test_summary_has_counts_but_no_titles():
    rows = [{"at": f"2026-11-0{day}T{hour:02d}:07:30+09:00", "ok": True, "scanned": 180, "new": 2, "relevant": 1,
             "alerts": 1, "seconds": 40.0, "cost": 0.1, "title": "비밀 제목", "shared": 180, "compared": 60, "matched": 57,
             "lambda_boards": 2}
            for day in (8, 9) for hour in range(0, 10)]
    rows[-1]["ok"] = False
    summary = cloud.summarize(rows, datetime(2026, 11, 9, 10, 0, tzinfo=KST))
    text = json.dumps(summary, ensure_ascii=False)
    assert "비밀" not in text and "cost" not in text
    assert summary["week"] == {"runs": 20, "new": 40, "relevant": 20, "alerts": 20}
    assert summary["shared"]["match_percent"] == 95.0 and summary["last_run"]["ok"] is False
    assert [day["date"] for day in summary["days"]] == ["2026-11-08", "2026-11-09"]
    assert 0 < summary["uptime_percent"] <= 100


def test_metrics_include_match_rate_only_when_compared():
    row = {"ok": True, "scanned": 10, "new": 1, "relevant": 1, "alerts": 0, "seconds": 3.0, "cost": 0.01}
    assert "SharedMatchPercent" not in {metric["MetricName"] for metric in cloud.metrics(row)}
    names = {metric["MetricName"]: metric["Value"] for metric in cloud.metrics({**row, "compared": 4, "matched": 3})}
    assert names["SharedMatchPercent"] == 75.0 and names["RunSuccess"] == 1.0


def test_backup_leaves_out_login_and_locks(tmp_path):
    (tmp_path / ".cache/ajou-assistant/locks").mkdir(parents=True)
    (tmp_path / ".cache/ajou-assistant/threads.json").write_text("{}")
    (tmp_path / ".cache/ajou-assistant/run.lock").write_text("")
    (tmp_path / ".cache/ajou-assistant/locks/10.lock").write_text("")
    (tmp_path / ".claude/projects/p").mkdir(parents=True)
    (tmp_path / ".claude/projects/p/s.jsonl").write_text("{}\n")
    (tmp_path / ".claude/.credentials.json").write_text("secret")
    (tmp_path / ".config/ajou-assistant").mkdir(parents=True)
    (tmp_path / ".config/ajou-assistant/env").write_text("KEY=1")
    names = tarfile.open(fileobj=io.BytesIO(cloud.make_backup(tmp_path))).getnames()
    assert ".cache/ajou-assistant/threads.json" in names and ".claude/projects/p/s.jsonl" in names
    assert not [name for name in names if "lock" in name or "credentials" in name or ".config" in name]


def test_restore_only_into_an_empty_folder(tmp_path):
    (tmp_path / "home/.cache/ajou-assistant").mkdir(parents=True)
    (tmp_path / "home/.cache/ajou-assistant/threads.json").write_text('{"1": {}}')
    data = cloud.make_backup(tmp_path / "home")

    class FakeS3:
        def get_paginator(self, name):
            return type("P", (), {"paginate": lambda self, **kw: [{"Contents": [{"Key": "backup/2026-11-08.tar.gz"},
                                                                                  {"Key": "backup/2026-11-09.tar.gz"}]}]})()

        def get_object(self, Bucket, Key):
            assert Key == "backup/2026-11-09.tar.gz"
            return {"Body": io.BytesIO(data)}

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("")
    assert cloud.restore(FakeS3(), "b", busy) == 1
    assert cloud.restore(FakeS3(), "b", tmp_path / "out") == 0
    assert (tmp_path / "out/.cache/ajou-assistant/threads.json").read_text() == '{"1": {}}'
