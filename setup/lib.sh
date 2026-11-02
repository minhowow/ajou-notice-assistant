# setup 스크립트가 함께 쓰는 도구. 직접 실행하지 않고 `. setup/lib.sh` 로 불러온다.

# macOS는 pbcopy/open, Linux는 wl-copy·xclip/xdg-open을 쓴다. 클립보드 도구가 없으면 파일로 저장한다.
copy_text() {
  if command -v pbcopy >/dev/null; then pbcopy
  elif command -v wl-copy >/dev/null; then wl-copy
  elif command -v xclip >/dev/null; then xclip -selection clipboard
  else
    out="$(mktemp -t ajou-paste.XXXXXX)"; cat > "$out"
    echo "(클립보드 도구가 없어 파일에 저장했습니다. 이 파일을 열어 내용을 복사하세요: $out)"
  fi
}

open_url() { open "$1" 2>/dev/null || xdg-open "$1" 2>/dev/null || echo "브라우저에서 직접 여세요: $1"; }
