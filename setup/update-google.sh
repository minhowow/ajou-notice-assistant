#!/bin/sh
# 이미 연결한 Apps Script를 새 코드로 바꾼다. 주소와 비밀값은 그대로 쓴다.
# 실행: AJOU_HOME=<데이터 폴더> sh setup/update-google.sh  (터미널에서 직접)
# 마지막 세 명령은 데이터 폴더의 기록을 읽으므로 AJOU_HOME이 맞아야 한다 (기본은 현재 폴더).
set -u
umask 077
here="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${AJOU_ENV_FILE:-$HOME/.config/ajou-assistant/env}"
[ -t 0 ] || { echo "터미널에서 직접 실행하세요." >&2; exit 1; }
. "$here/setup/lib.sh"
secret="$(grep '^GOOGLE_BRIDGE_SECRET=' "$env_file" | cut -d= -f2-)"
[ -n "$secret" ] || { echo "연결 기록이 없습니다. setup-google.sh를 먼저 실행하세요." >&2; exit 1; }

sed "s/__SECRET__/$secret/" "$here/google/Code.gs" | copy_text
cat <<'TEXT'

1. 학교 계정으로 script.google.com 에서 'Ajou Assistant' 프로젝트를 엽니다.
2. Code.gs 내용을 모두 지우고 붙여넣기 합니다. 코드는 이미 복사되어 있습니다.
TEXT
open_url "https://script.google.com/home"
printf '다 했으면 Enter: '; read -r _
copy_text < "$here/google/appsscript.json"
cat <<'TEXT'

3. appsscript.json 을 열어 모두 지우고 붙여넣기 합니다. 캘린더 고급 서비스가 추가됩니다.
4. 저장합니다.
5. 오른쪽 위 '배포 → 배포 관리'를 누릅니다. '새 배포'가 아닙니다. 새 배포를 만들면 주소가 바뀝니다.
6. 연필(수정) 아이콘 → 버전: '새 버전' → 배포.
7. 권한 승인 창이 뜨면 학교 계정으로 승인합니다.
TEXT
printf '다 했으면 Enter: '; read -r _
ajou() { uv run --project "$here" -q ajou "$@"; }
ajou google-check

printf "\n비서가 만든 일정 중 내 일정과 같은 시각에 겹치는 것을 지웁니다\n"
ajou calendar-dedupe
printf "\n대화 기록의 일정에 참석 응답을 채웁니다\n"
ajou records-apply
printf "\n밀린 공지 일정을 캘린더에 넣습니다 (확실한 것만, 이미 있으면 건너뜀)\n"
ajou calendar-sync
