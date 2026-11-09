#!/bin/sh
# AWS 쪽을 올리거나 고친다. 맥이나 아무 컴퓨터에서 aws CLI와 uv로 실행한다.
#   AWS_PROFILE=... AWS_REGION=... BOUNDARY_ARN=... ALERT_EMAIL=... DISCORD_WEBHOOK_URL=... sh aws/deploy.sh
# 처음 한 번은 DISCORD_WEBHOOK_URL이 있어야 한다. 다음부터 빼면 이전 값을 그대로 쓴다.
# 하는 일: 1) 본체 스택(ajou-assistant-core) 2) Lambda 수집기 코드 3) 현황 페이지 파일 4) CloudFront 스택(ajou-assistant-page)
# 집 서버용 접근 키는 만들지 않는다. README의 'AWS' 절을 따라 한 번만 만든다.
set -eu
cd "$(dirname "$0")"
: "${AWS_REGION:?AWS_REGION을 정하세요}"
CORE=${CORE_STACK:-ajou-assistant-core}
PAGE=${PAGE_STACK:-ajou-assistant-page}

set -- "BoundaryPolicyArn=${BOUNDARY_ARN:-}" "AlertEmail=${ALERT_EMAIL:-}"
[ -n "${DISCORD_WEBHOOK_URL:-}" ] && set -- "$@" "DiscordWebhookUrl=$DISCORD_WEBHOOK_URL"
[ -n "${COLLECTOR_BOARDS:-}" ] && set -- "$@" "CollectorBoards=$COLLECTOR_BOARDS"
[ -n "${GITHUB_ECR:-}" ] && set -- "$@" "GitHubEcr=$GITHUB_ECR"
echo "1/4 본체 스택 $CORE"
aws cloudformation deploy --stack-name "$CORE" --template-file stack.yaml --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset --parameter-overrides "$@"
output() { aws cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }
SHARED=$(output "$CORE" SharedBucket)

echo "2/4 Lambda 수집기 코드"
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
uv pip install --quiet --target "$BUILD" --python-version 3.12 --python-platform aarch64-manylinux2014 httpx beautifulsoup4
mkdir "$BUILD/ajou_assistant"
for name in __init__ config scrape store cloud; do cp "../ajou_assistant/$name.py" "$BUILD/ajou_assistant/"; done
cp lambda/collector.py "$BUILD/"
(cd "$BUILD" && find . -name '__pycache__' -prune -exec rm -rf {} + && zip -qr collector.zip . -x 'collector.zip')
aws lambda update-function-code --function-name ajou-assistant-collector --zip-file "fileb://$BUILD/collector.zip" \
  --query 'CodeSize' --output text
aws lambda wait function-updated-v2 --function-name ajou-assistant-collector

echo "3/4 현황 페이지 파일"
aws s3 cp site/index.html "s3://$SHARED/site/index.html" --content-type 'text/html; charset=utf-8' --cache-control max-age=300 --only-show-errors

echo "4/4 현황 페이지 스택 $PAGE"
aws cloudformation deploy --stack-name "$PAGE" --template-file page.yaml --no-fail-on-empty-changeset \
  --parameter-overrides "SharedBucket=$SHARED"
echo "현황 페이지: $(output "$PAGE" PageUrl)"
