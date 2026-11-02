#!/bin/sh
# 학교 Google 계정의 캘린더·메일 연결(선택). Google Cloud 프로젝트가 필요 없다.
# 실행: sh setup/setup-google.sh  (터미널에서 직접)
# 다른 비밀값 위치를 쓰려면 AJOU_ENV_FILE=/경로 를 지정한다.
set -u
umask 077
here="$(cd "$(dirname "$0")/.." && pwd)"
env_file="${AJOU_ENV_FILE:-$HOME/.config/ajou-assistant/env}"
mkdir -p "$(dirname "$env_file")"
[ -t 0 ] || { echo "터미널에서 직접 실행하세요." >&2; exit 1; }

save_value() {
  tmp="$env_file.tmp.$$"
  grep -v "^$1=" "$env_file" > "$tmp" 2>/dev/null
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  mv "$tmp" "$env_file"
}

. "$here/setup/lib.sh"

secret="$(openssl rand -hex 32)"
sed "s/__SECRET__/$secret/" "$here/google/Code.gs" | copy_text
cat <<'TEXT'

1. 브라우저에서 학교 Google 계정으로 script.google.com 을 엽니다. (개인 계정으로도 되지만 학교 일정과 메일을 쓰려면 학교 계정으로 합니다.)
2. '새 프로젝트'를 누르고, 이름을 Ajou Assistant 로 바꿉니다.
3. 왼쪽 톱니바퀴(프로젝트 설정)에서 'appsscript.json 표시'를 켭니다.
4. 편집기로 돌아와 Code.gs 내용을 모두 지우고 붙여넣기(Cmd/Ctrl+V) 합니다. 코드는 이미 복사되어 있습니다.
TEXT
open_url "https://script.google.com/home/start"
printf '다 했으면 Enter: '; read -r _
copy_text < "$here/google/appsscript.json"
cat <<'TEXT'

5. 왼쪽 파일 목록에서 appsscript.json 을 열어 내용을 모두 지우고 붙여넣기(Cmd/Ctrl+V) 합니다.
6. 저장(Cmd/Ctrl+S) 후 오른쪽 위 '배포 → 새 배포 → 유형: 웹 앱'을 고릅니다.
   - 다음 사용자 인증 정보로 실행: 나
   - 액세스 권한이 있는 사용자: 모든 사용자
7. 권한 승인 창이 뜨면 학교 계정을 고릅니다.
   '확인되지 않은 앱' 경고가 나오면 고급 → 이동을 누릅니다. 본인이 방금 만든 스크립트입니다.
   '관리자가 차단' 이라고 나오면 학교가 외부 스크립트를 막은 것입니다. 이 창을 닫으세요.
   캘린더·메일 연결 없이도 공지 수집과 알림은 동작합니다.
8. 완료 화면의 '웹 앱 URL'을 복사합니다.
TEXT
printf '웹 앱 URL 붙여넣기: '; read -r url
case "$url" in https://script.google.com/*) ;; *) echo "URL 형식이 아닙니다."; exit 1 ;; esac
touch "$env_file"; chmod 600 "$env_file"
save_value GOOGLE_BRIDGE_URL "$url"
save_value GOOGLE_BRIDGE_SECRET "$secret"
uv run --project "$here" -q ajou google-check || exit 1
cat <<'TEXT'

연결 완료. 위 두 값은 이 컴퓨터의 env 파일에만 저장되었습니다.
다른 컴퓨터(서버 등)에서도 쓰려면 그곳의 env 파일에 GOOGLE_BRIDGE_URL, GOOGLE_BRIDGE_SECRET 두 줄을 직접 옮기세요.
TEXT
