"""컨테이너 안의 시계: systemd 타이머와 같은 시각, 놓친 차례는 한 번만."""
from datetime import datetime, timedelta, timezone

from ajou_assistant.cli import due_jobs, last_slot

KST = timezone(timedelta(hours=9))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=KST)


def test_slots_match_the_old_timers():
    assert last_slot("run", at("2026-11-03T16:06")) == at("2026-11-03T15:07")
    assert last_slot("run", at("2026-11-03T16:07")) == at("2026-11-03T16:07")
    assert last_slot("digest", at("2026-11-03T21:29")) == at("2026-11-02T21:30")
    assert last_slot("digest", at("2026-11-03T21:30")) == at("2026-11-03T21:30")
    assert last_slot("weekly", at("2026-11-08T20:59")) == at("2026-11-01T21:00")  # 11/8은 일요일
    assert last_slot("weekly", at("2026-11-08T21:00")) == at("2026-11-08T21:00")
    assert last_slot("weekly", at("2026-11-10T09:00")) == at("2026-11-08T21:00")


def test_missed_turns_run_once_after_downtime():
    done = {job: "2026-11-03T16:07:30+09:00" for job in ("run", "digest", "weekly")}
    assert due_jobs(at("2026-11-03T16:30"), done) == []
    assert due_jobs(at("2026-11-03T17:07"), done) == ["run"]
    # 21:00~23:00 꺼져 있다 23:00에 켜지면 run 한 번, digest 한 번.
    assert due_jobs(at("2026-11-03T23:00"), done) == ["run", "digest"]
    done = {"run": "2026-11-08T23:07:00+09:00", "digest": "2026-11-08T21:30:00+09:00", "weekly": "2026-11-01T21:00:00+09:00"}
    assert due_jobs(at("2026-11-08T23:10"), done) == ["weekly"]


def test_backup_runs_once_a_day_at_half_past_four():
    assert last_slot("backup", at("2026-11-09T04:29")) == at("2026-11-08T04:30")
    assert last_slot("backup", at("2026-11-09T04:30")) == at("2026-11-09T04:30")
    done = {"run": "2026-11-09T04:07:00+09:00", "digest": "2026-11-08T21:30:00+09:00",
            "weekly": "2026-11-08T21:00:00+09:00", "backup": "2026-11-08T04:30:10+09:00"}
    assert due_jobs(at("2026-11-09T04:31"), done) == ["backup"]
