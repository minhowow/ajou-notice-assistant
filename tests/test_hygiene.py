"""저장소에 개인 데이터나 비밀값이 섞여 들어가지 않았는지 보는 일반 검사.

특정 사람의 이름 같은 것은 여기에 적지 않는다. 형태로 알아볼 수 있는 것만 본다.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "Discord 스노플레이크처럼 긴 숫자": re.compile(r"(?<![\d.])\d{15,}(?![\d.])"),
    "Anthropic API 키": re.compile(r"sk-" + r"ant-[A-Za-z0-9_-]{10,}"),
    "개인 Apps Script 배포 주소": re.compile(r"script\.google\.com/macros/s/[A-Za-z0-9_-]{10,}"),
    "Discord 봇 토큰": re.compile(r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
    "비공개 키 블록": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "집 전화번호·휴대폰 번호": re.compile(r"\b01[016789]-\d{3,4}-\d{4}\b"),
}


def tracked_text_files() -> list[Path]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, text=True, check=True).stdout.split("\n")
    paths = []
    for name in filter(None, listed):
        path = ROOT / name
        if path.suffix in {".png", ".lock"} or not path.is_file():
            continue
        paths.append(path)
    return paths


def test_no_personal_looking_data_in_repository():
    found = []
    for path in tracked_text_files():
        text = path.read_text(errors="ignore")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                found.append(f"{path.relative_to(ROOT)}: {label}")
    assert not found, found


def test_data_files_are_ignored_by_git():
    ignored = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "discord.json", "CURRENT_PROFILE.md", "private_facts.md",
         "auto/notices/seen.jsonl", "outputs/threads/1/log.md", "records/2026-01.md", "boards.json"],
        capture_output=True, text=True).stdout.split()
    assert len(ignored) == 7
