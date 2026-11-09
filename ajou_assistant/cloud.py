"""AWS 연동 (선택). 설정이 없으면 아무것도 하지 않는다. 실패해도 수집과 알림은 계속된다.

하는 일
- 매 실행 끝: CloudWatch 지표(성공, 건수, 시간, AI 비용)를 보내고, 현황 페이지가 읽을 집계 JSON을 올린다.
- 매 실행 끝: 읽은 공지 목록을 학교 공용 테이블(DynamoDB)에 올리고, Lambda 수집기가 같은 시간대에 본 글과 맞춰 본다.
- 하루 한 번(`ajou backup`): 상태 폴더와 Claude 세션을 압축해 S3에 올린다. 비밀값과 Claude 로그인은 넣지 않는다.

설정 (환경변수나 env 파일)
  AJOU_AWS_REGION          이것이 있어야 켜진다. 예: ap-southeast-2
  AJOU_AWS_BACKUP_BUCKET   백업 버킷 (backup/ 아래에 쓴다)
  AJOU_AWS_SITE_BUCKET     현황 페이지 버킷 (site/status.json 하나만 쓴다)
  AJOU_AWS_TABLE           공용 수집 테이블
  AJOU_AWS_LOG_GROUP       매시 수집 실행의 출력을 보낼 CloudWatch Logs 그룹 (요약·주간 정리는 개인 내용이라 보내지 않는다)
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY   env 파일에 둔다. 없으면 boto3 기본 방식으로 찾는다
인프라는 aws/stack.yaml, 설명은 README의 'AWS' 절.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store
from .config import STATE_DIR, setting
from .scrape import ListItem, site_domain

NAMESPACE = "AjouAssistant"
RUNS_FILE = STATE_DIR / "runs.jsonl"
KEEP_RUNS = 24 * 31
WINDOW = timedelta(hours=2)  # 이 안에 Lambda가 본 글을 '같은 시간대'로 친다
RELEVANT = ("high", "medium")
BACKUP_PARTS = (".cache/ajou-assistant", ".claude/projects", ".claude/settings.json")
BACKUP_SKIP = {"locks", "leases", ".credentials.json"}


def enabled() -> bool:
    return bool(setting("AJOU_AWS_REGION"))


def client(service: str):
    import boto3
    from botocore.config import Config
    return boto3.client(
        service, region_name=setting("AJOU_AWS_REGION"),
        aws_access_key_id=setting("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=setting("AWS_SECRET_ACCESS_KEY") or None,
        config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2}),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_of(board_url: str, board_key: str) -> str:
    """공용 테이블의 파티션 키. 학교 도메인과 게시판으로 나눠 여러 학교가 같은 표를 쓴다."""
    return f"{site_domain(board_url)}#{board_key}"


def item_fields(item: ListItem, seen_by: str, moment: str) -> tuple[str, dict, dict]:
    """공지 한 건을 올릴 UpdateItem 식. 처음 본 시각은 남기고 나머지는 덮어쓴다."""
    values = {":title": item.title, ":date": item.date, ":url": item.url, ":category": item.category,
              ":department": item.department, ":signature": item.signature, ":now": moment}
    expression = (f"SET title = :title, #date = :date, #url = :url, category = :category, department = :department, "
                  f"signature = :signature, {seen_by} = :now, first_seen = if_not_exists(first_seen, :now)")
    return expression, {"#date": "date", "#url": "url"}, {key: {"S": str(value)} for key, value in values.items()}


def share(items: dict, moment: datetime | None = None, db=None) -> dict:
    """읽은 공지를 공용 테이블에 올리고, Lambda 수집기가 맡은 게시판은 일치율을 잰다.

    Lambda가 한 번이라도 본 게시판만 비교한다(학교가 해외 IP를 막는 게시판은 집 서버만 올린다).
    """
    table = setting("AJOU_AWS_TABLE")
    if not table or not items:
        return {}
    db = db or client("dynamodb")
    now = moment or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    since = (now - WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")

    def put(item: ListItem) -> tuple[ListItem, dict]:
        expression, names, values = item_fields(item, "home_seen", stamp)
        answer = db.update_item(TableName=table, Key={"source": {"S": source_of(item.board_url, item.board_key)},
                                                      "id": {"S": str(item.article_id)}},
                                UpdateExpression=expression, ExpressionAttributeNames=names,
                                ExpressionAttributeValues=values, ReturnValues="ALL_OLD")
        return item, answer.get("Attributes", {})

    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(put, items.values()))
    covered = {item.board_key for item, old in rows if "lambda_seen" in old}
    compared = [old for item, old in rows if item.board_key in covered]
    matched = sum(1 for old in compared if old.get("lambda_seen", {}).get("S", "") >= since)
    return {"shared": len(rows), "compared": len(compared), "matched": matched, "lambda_boards": len(covered)}


def run_row(started: datetime, items: dict, outcomes: list[dict], cost: float, shared: dict) -> dict:
    """실행 하나의 집계. 제목이나 내용은 넣지 않는다."""
    finished = store.now()
    return {"at": finished.isoformat(timespec="seconds"), "ok": bool(items), "scanned": len(items),
            "new": len(outcomes), "relevant": sum(1 for row in outcomes if row.get("relevance") in RELEVANT),
            "alerts": sum(1 for row in outcomes if row.get("channel")),
            "seconds": round((finished - started).total_seconds(), 1), "cost": round(cost, 4), **shared}


def metrics(row: dict) -> list[dict]:
    data = [("RunSuccess", 1 if row["ok"] else 0, "Count"), ("Scanned", row["scanned"], "Count"),
            ("NewNotices", row["new"], "Count"), ("Relevant", row["relevant"], "Count"), ("Alerts", row["alerts"], "Count"),
            ("DurationSeconds", row["seconds"], "Seconds"), ("AICostUSD", row["cost"], "None")]
    if row.get("compared"):
        data.append(("SharedMatchPercent", round(100 * row["matched"] / row["compared"], 1), "Percent"))
    return [{"MetricName": name, "Value": float(value), "Unit": unit} for name, value, unit in data]


def summarize(rows: list[dict], now: datetime, notices: list[dict] | None = None) -> dict:
    """현황 페이지용 7일 집계. 공지 제목·프로필·개인정보는 넣지 않는다.

    rows는 이 기능을 켠 뒤의 실행 기록(가동률·일치율), notices는 공지 분석 기록이다. notices를 주면
    날짜별 새 공지·관련·알림 건수를 분석 기록에서 센다(켜기 전 날짜도 실제 처리 기록으로 채워진다).
    옛 글 일괄 분석(backfill)과 수정된 글의 재분석은 세지 않는다.
    """
    since = now - timedelta(days=7)
    recent = [row for row in rows if datetime.fromisoformat(row["at"]) >= since]
    days: dict[str, dict] = {}

    def day_of(stamp: str) -> dict:
        return days.setdefault(stamp[:10], {"date": stamp[:10], "runs": 0, "ok": 0, "new": 0, "relevant": 0, "alerts": 0})

    for row in recent:
        day = day_of(row["at"])
        day["runs"] += 1
        day["ok"] += 1 if row["ok"] else 0
        if notices is None:
            for key in ("new", "relevant", "alerts"):
                day[key] += row.get(key, 0)
    for notice in notices or []:
        stamp = notice.get("analyzed_at", "")
        if not stamp or notice.get("backfill") or notice.get("changed") or datetime.fromisoformat(stamp) < since:
            continue
        day = day_of(stamp)
        day["new"] += 1
        day["relevant"] += 1 if (notice.get("analysis") or {}).get("relevance") in RELEVANT else 0
        day["alerts"] += 1 if notice.get("channel") else 0
    ok_hours = {row["at"][:13] for row in recent if row["ok"]}
    first = min((datetime.fromisoformat(row["at"]) for row in recent), default=now)
    expected = max(1, min(168, int((now - first).total_seconds() // 3600) + 1))
    compared = sum(row.get("compared", 0) for row in recent)
    matched = sum(row.get("matched", 0) for row in recent)
    last = recent[-1] if recent else None
    shared_last = next((row for row in reversed(recent) if row.get("compared")), None)
    return {
        "updated": now.isoformat(timespec="seconds"),
        "watch_since": recent[0]["at"] if recent else None,
        "last_run": {"at": last["at"], "ok": last["ok"], "seconds": last["seconds"], "scanned": last["scanned"]} if last else None,
        "uptime_percent": round(100 * min(len(ok_hours), expected) / expected, 1),
        "week": {key: sum(day[key] for day in days.values()) for key in ("runs", "new", "relevant", "alerts")},
        "days": sorted(days.values(), key=lambda day: day["date"]),
        "shared": {
            "match_percent": round(100 * matched / compared, 1) if compared else None,
            "last_match_percent": round(100 * shared_last["matched"] / shared_last["compared"], 1) if shared_last else None,
            "items_per_run": shared_last.get("shared") if shared_last else None,
            "lambda_boards": shared_last.get("lambda_boards") if shared_last else None,
        },
    }


def load_runs() -> list[dict]:
    return store.read_jsonl(RUNS_FILE)


def save_run(row: dict) -> list[dict]:
    rows = [*load_runs(), row][-KEEP_RUNS:]
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = RUNS_FILE.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows))
    temporary.replace(RUNS_FILE)
    return rows


def after_run(started: datetime, items: dict, outcomes: list[dict], cost: float) -> None:
    """실행 끝에 부른다. 무엇이 실패해도 예외를 밖으로 내지 않는다."""
    if not enabled():
        return
    shared: dict = {}
    try:
        shared = share(items)
    except Exception as error:
        print(f"AWS 공용 테이블 실패: {type(error).__name__}: {str(error)[:160]}")
    row = run_row(started, items, outcomes, cost, shared)
    try:
        rows = save_run(row)
    except OSError as error:
        print(f"실행 기록 실패: {error}")
        rows = [row]
    try:
        client("cloudwatch").put_metric_data(Namespace=NAMESPACE, MetricData=metrics(row))
    except Exception as error:
        print(f"AWS 지표 실패: {type(error).__name__}: {str(error)[:160]}")
    bucket = setting("AJOU_AWS_SITE_BUCKET")
    if bucket:
        try:
            body = json.dumps(summarize(rows, store.now(), store.recent_analysis(2)), ensure_ascii=False).encode()
            client("s3").put_object(Bucket=bucket, Key="site/status.json", Body=body, ContentType="application/json",
                                    CacheControl="max-age=60")
        except Exception as error:
            print(f"AWS 현황 실패: {type(error).__name__}: {str(error)[:160]}")
    print("AWS: " + ", ".join(f"{key} {value}" for key, value in row.items() if key not in ("at",)))


def ship_log(job: str, lines: list[str], logs=None) -> None:
    """수집 실행 하나의 출력을 로그 그룹의 하루치 스트림에 붙인다. 실패해도 조용히 넘어간다."""
    group = setting("AJOU_AWS_LOG_GROUP")
    if not enabled() or not group or not lines:
        return
    try:
        logs = logs or client("logs")
        stream = f"{job}-{store.now():%Y-%m-%d}"
        try:
            logs.create_log_stream(logGroupName=group, logStreamName=stream)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        events = [{"timestamp": stamp, "message": line.rstrip("\n")[:4000] or " "} for line in lines[-2000:]]
        logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=events)
    except Exception as error:
        print(f"AWS 로그 실패: {type(error).__name__}: {str(error)[:160]}")


def skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = set(Path(info.name).parts)
    if parts & BACKUP_SKIP or info.name.endswith(".lock") or info.name.endswith(".tmp"):
        return None
    return info


def make_backup(home: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for part in BACKUP_PARTS:
            if (home / part).exists():
                archive.add(home / part, arcname=part, filter=skip)
    return buffer.getvalue()


def backup_main(args) -> int:
    bucket = setting("AJOU_AWS_BACKUP_BUCKET")
    if not enabled() or not bucket:
        print("AWS 백업이 꺼져 있습니다 (AJOU_AWS_REGION, AJOU_AWS_BACKUP_BUCKET).")
        return 0
    s3 = client("s3")
    if args.restore:
        return restore(s3, bucket, Path(args.restore), args.key)
    data = make_backup(Path.home())
    key = f"backup/{store.now():%Y-%m-%d}.tar.gz"
    s3.put_object(Bucket=bucket, Key=key, Body=data, ServerSideEncryption="AES256")
    print(f"백업 {key}: {len(data) / 1e6:.1f}MB sha256 {hashlib.sha256(data).hexdigest()[:16]}")
    return 0


def restore(s3, bucket: str, target: Path, key: str = "") -> int:
    """백업을 target 폴더에 푼다. 지금 쓰는 파일을 덮지 않도록 빈 폴더에만 푼다."""
    if target.exists() and any(target.iterdir()):
        print(f"{target}가 비어 있지 않습니다. 빈 폴더를 주세요.")
        return 1
    if not key:
        pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="backup/")
        keys = sorted(row["Key"] for page in pages for row in page.get("Contents", []))
        if not keys:
            print("백업이 없습니다.")
            return 1
        key = keys[-1]
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(target, filter="data")
    print(f"{key}를 {target}에 풀었습니다. 확인한 뒤 필요한 파일만 홈 폴더로 옮기세요.")
    return 0
