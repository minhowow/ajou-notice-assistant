"""공용 수집 계층의 Lambda 수집기. 로그인 없이 보이는 게시판의 첫 쪽을 읽어 공지는 DynamoDB에, 본문은 S3에 넣는다.

판단과 알림은 하지 않는다. 학교 사이트가 해외 IP를 막는 게시판은 BOARDS에서 빼고, 그런 게시판은 집 서버가
읽은 목록을 같은 테이블에 올린다(ajou_assistant/cloud.py의 share). 본문은 처음 본 글만 한 번 읽는다(학교 서버 부담).

환경변수: TABLE, BUCKET, BOARDS(쉼표로 구분한 게시판 key), MAX_DETAILS(한 번에 읽을 본문 수)
배포: aws/deploy.sh가 이 파일과 ajou_assistant의 수집 모듈, httpx·beautifulsoup4만 묶어 올린다.
"""
import json
import os
import re

import boto3

from ajou_assistant import cloud
from ajou_assistant.config import BOARDS, LIST_PAGE_SIZE
from ajou_assistant.scrape import Fetcher

db = boto3.client("dynamodb")
s3 = boto3.client("s3")
watch = boto3.client("cloudwatch")


def handler(event, context):
    table, bucket = os.environ["TABLE"], os.environ["BUCKET"]
    wanted = {key.strip() for key in os.environ.get("BOARDS", "").split(",") if key.strip()}
    limit = int(os.environ.get("MAX_DETAILS", "40"))
    stamp = cloud.utc_now()
    fetcher = Fetcher()
    seen = new = details = 0
    errors: list[str] = []
    for board in [board for board in BOARDS if board.key in wanted]:
        try:
            rows = fetcher.list_page(board, 0, LIST_PAGE_SIZE)
        except Exception as error:
            errors.append(f"{board.key}: {type(error).__name__}: {str(error)[:100]}")
            continue
        for item in rows:
            source = cloud.source_of(item.board_url, item.board_key)
            key = {"source": {"S": source}, "id": {"S": str(item.article_id)}}
            expression, names, values = cloud.item_fields(item, "lambda_seen", stamp)
            old = db.update_item(TableName=table, Key=key, UpdateExpression=expression, ExpressionAttributeNames=names,
                                 ExpressionAttributeValues=values, ReturnValues="ALL_OLD").get("Attributes", {})
            seen += 1
            new += "first_seen" not in old
            if "body_key" in old or details >= limit:
                continue
            try:
                detail = fetcher.detail(item, board)
            except Exception as error:
                errors.append(f"{board.key}/{item.article_id}: {type(error).__name__}")
                continue
            details += 1
            body_key = f"raw/{source.replace('#', '/')}/{re.sub(r'[^A-Za-z0-9_.-]', '-', str(item.article_id))}.json"
            body = {"source": source, "id": str(item.article_id), "url": item.url, "title": item.title, "date": item.date,
                    "body": detail.body_text, "images": detail.image_urls,
                    "attachments": [{"name": file.name, "url": file.url} for file in detail.attachments],
                    "content_hash": detail.content_hash, "fetched": stamp}
            s3.put_object(Bucket=bucket, Key=body_key, Body=json.dumps(body, ensure_ascii=False).encode(),
                          ContentType="application/json", ServerSideEncryption="AES256")
            db.update_item(TableName=table, Key=key, UpdateExpression="SET body_key = :key, content_hash = :hash",
                           ExpressionAttributeValues={":key": {"S": body_key}, ":hash": {"S": detail.content_hash}})
    watch.put_metric_data(Namespace=cloud.NAMESPACE, MetricData=[
        {"MetricName": "CollectorItems", "Value": seen, "Unit": "Count"},
        {"MetricName": "CollectorNew", "Value": new, "Unit": "Count"}])
    result = {"at": stamp, "items": seen, "new": new, "details": details, "errors": errors}
    print(json.dumps(result, ensure_ascii=False))
    if errors and not seen:
        raise RuntimeError("모든 게시판 수집 실패: " + "; ".join(errors))
    return result
