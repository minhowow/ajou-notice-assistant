#!/bin/sh
# 서버(봇이 도는 곳)에서 돈다: 표준 입력의 한 줄을 비밀값 파일(env)의 KEY=값으로 넣는다. 이미 있으면 바꾼다.
# 값은 명령줄 인자로 받지 않는다(같은 컴퓨터의 다른 사용자가 ps로 볼 수 있다). 화면에도 쓰지 않고 글자 수만 알린다.
#
# 다른 컴퓨터에서 쓰는 법 (값은 입력해도 화면에 안 보이고 셸 기록에도 남지 않는다):
#   printf '값 붙여넣기: '; stty -echo; IFS= read -r v; stty echo; echo
#   printf '%s\n' "$v" | ssh 서버 'sh ~/.local/bin/ajou-put-secret KEY'; unset v
set -eu
key="${1:-}"
case "$key" in
  ""|*[!A-Z0-9_]*) echo "키 이름은 대문자·숫자·밑줄만: $key" >&2; exit 1 ;;
esac
env_file="${AJOU_ENV_FILE:-$HOME/.config/ajou-assistant/env}"
IFS= read -r value || true
value="$(printf '%s' "$value" | tr -d '\r')"
[ -n "$value" ] || { echo "값이 비었습니다. 아무것도 바꾸지 않았습니다." >&2; exit 1; }
umask 077
touch "$env_file"
tmp="$env_file.tmp.$$"
grep -v "^$key=" "$env_file" > "$tmp" || true
printf '%s=%s\n' "$key" "$value" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$env_file"
echo "$key 저장함 (${#value}자). 값은 출력하지 않습니다."
