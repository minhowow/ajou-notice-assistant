#!/bin/sh
# 비밀값(Claude API 키, Discord 봇 토큰)을 저장소 밖 env 파일에 넣는다.
# 실행: sh setup/setup-secrets.sh  (터미널에서 직접)
# 입력한 값은 화면에 보이지 않고, 파일 권한은 본인만 읽을 수 있게(600) 둔다.
# 다른 곳에 두려면 AJOU_ENV_FILE=/경로 를 지정한다.
set -u
umask 077
env_file="${AJOU_ENV_FILE:-$HOME/.config/ajou-assistant/env}"
[ -t 0 ] || { echo "터미널에서 직접 실행하세요." >&2; exit 1; }
mkdir -p "$(dirname "$env_file")"
touch "$env_file"; chmod 600 "$env_file"

save_value() {
  tmp="$env_file.tmp.$$"
  grep -v "^$1=" "$env_file" > "$tmp" 2>/dev/null
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  mv "$tmp" "$env_file"
}

ask_secret() {
  printf '%s (건너뛰려면 Enter): ' "$2"
  stty -echo 2>/dev/null; read -r value; stty echo 2>/dev/null; echo
  if [ -n "$value" ]; then save_value "$1" "$value"; echo "  저장: $1"; fi
}

echo "저장 위치: $env_file"
ask_secret ANTHROPIC_API_KEY "Claude API 키 (console.anthropic.com에서 발급)"
ask_secret DISCORD_BOT_TOKEN "Discord 봇 토큰 (Developer Portal > Bot > Reset Token)"
echo "완료. 이 파일은 저장소에 넣지 마세요."
