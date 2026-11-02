"""데이터 폴더의 git 동기화. 선택 기능이다 (AJOU_GIT_SYNC=1이고 데이터 폴더가 git 저장소일 때만).

여러 컴퓨터가 같은 기록을 이어 쓰거나 기록을 백업할 때 쓴다. 수집기와 스레드 봇이 같은 저장소를
쓰므로 파일 잠금으로 순서를 맞춘다. 꺼져 있으면 모든 함수가 아무것도 하지 않고 None을 돌려준다.
기록에는 개인정보가 들어 있으므로 원격은 반드시 비공개 저장소로 둔다.
"""
from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager

from .config import AUTO, HOME_DIR, STATE_DIR, setting


def enabled() -> bool:
    return setting("AJOU_GIT_SYNC", "0").lower() in ("1", "true", "yes") and (HOME_DIR / ".git").exists()


@contextmanager
def _locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "git.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(HOME_DIR), *args], capture_output=True, text=True, timeout=120)


def _pull() -> str | None:
    result = git("pull", "--rebase", "--autostash", "-q")
    if result.returncode == 0:
        return None
    git("rebase", "--abort")
    return result.stderr.strip()[:300]


def sync_pull() -> str | None:
    if not enabled():
        return None
    with _locked():
        return _pull()


def sync_push(message: str, *paths: str) -> str | None:
    """지정한 경로만 커밋해 올린다. 기본은 자동 기록 폴더다."""
    if not enabled():
        return None
    # 아직 없는 경로가 하나라도 섞이면 git add 전체가 실패하므로 있는 것만 넘긴다.
    targets = [path for path in paths if (HOME_DIR / path).exists()] if paths else [str(AUTO)]
    if not targets:
        return None
    with _locked():
        git("add", *targets)
        if git("diff", "--cached", "--quiet").returncode == 0:
            return None
        committed = git("commit", "-q", "-m", message)
        if committed.returncode:
            return committed.stderr.strip()[:300]
        pushed = git("push", "-q")
        for _ in range(2):
            if pushed.returncode == 0:
                return None
            _pull()
            pushed = git("push", "-q")
        return pushed.stderr.strip()[:300] if pushed.returncode else None


def sync_all(message: str) -> str | None:
    """예비 호스트용 전체 동기화. 작업 중인 파일을 치우지 않고 커밋한 뒤 합친다."""
    if not enabled():
        return None
    with _locked():
        git("add", "-A")
        if git("diff", "--cached", "--quiet").returncode:
            committed = git("commit", "-q", "-m", message)
            if committed.returncode:
                return committed.stderr.strip()[:300]
        pulled = git("pull", "--rebase", "-q")
        if pulled.returncode:
            git("rebase", "--abort")
            return pulled.stderr.strip()[:300]
        pushed = git("push", "-q")
        return pushed.stderr.strip()[:300] if pushed.returncode else None
