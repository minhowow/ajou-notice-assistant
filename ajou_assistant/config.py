"""경로, 게시판, 모델 설정.

코드와 개인 데이터를 나눈다. 데이터(auto/, outputs/, records/, 프로필, discord.json, boards.json)는
환경변수 AJOU_HOME 폴더 아래에 둔다. 없으면 명령을 실행한 현재 폴더를 쓴다.
비밀값(API 키, 봇 토큰)은 저장소 밖 env 파일에서만 읽는다.

환경변수 (env 파일에 같은 이름으로 적어도 된다)
  AJOU_HOME             데이터 폴더. 기본은 현재 작업 폴더
  AJOU_ENV_FILE         비밀값 파일. 기본 ~/.config/ajou-assistant/env
  AJOU_STATE_DIR        잠금·대응표 폴더. 기본 ~/.cache/ajou-assistant
  AJOU_MODEL            판단에 쓸 모델
  AJOU_USER_NAME        프롬프트에서 부를 사용자 이름. 기본 "사용자"
  AJOU_SCHOOL           프롬프트에 쓸 학교 이름. 기본 "아주대학교"
  AJOU_GIT_SYNC         1이면 데이터 폴더를 git으로 동기화한다 (선택)
  AJOU_THREAD_LIMIT     스레드 대화 월 예산(달러)
  AJOU_STANDBY_HOURS    예비 호스트가 이어받기까지 기다릴 시간
  AJOU_DISCORD_CATEGORY `ajou style`이 찾을 Discord 카테고리 이름
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class SetupError(RuntimeError):
    """설정이 빠졌을 때. 사용자가 고칠 일이므로 트레이스백 없이 메시지만 보여 준다."""


def _home() -> Path:
    raw = os.environ.get("AJOU_HOME")
    return (Path(raw).expanduser() if raw else Path.cwd()).resolve()


HOME_DIR = _home()
AUTO = HOME_DIR / "auto"
NOTICES = AUTO / "notices"
SEEN_FILE = NOTICES / "seen.jsonl"
ANALYSIS_DIR = NOTICES / "analysis"
HEARTBEAT_DIR = AUTO / "heartbeat"
PROFILE_FILE = HOME_DIR / "CURRENT_PROFILE.md"
PRIVATE_FACTS_FILE = HOME_DIR / "private_facts.md"
THREAD_DIR = HOME_DIR / "outputs" / "threads"
ACTIONS_FILE = NOTICES / "actions.jsonl"
REMINDERS_FILE = NOTICES / "reminders.jsonl"
USAGE_DIR = AUTO / "usage"
DISCORD_FILE = HOME_DIR / "discord.json"
BOARDS_FILE = HOME_DIR / "boards.json"
ENV_FILE = Path(os.environ.get("AJOU_ENV_FILE") or Path.home() / ".config" / "ajou-assistant" / "env").expanduser()
STATE_DIR = Path(os.environ.get("AJOU_STATE_DIR") or Path.home() / ".cache" / "ajou-assistant").expanduser()

MODEL = os.environ.get("AJOU_MODEL", "claude-opus-5-5")
USER_AGENT = "ajou-personal-notice-assistant/0.1 (student personal use)"
REQUEST_DELAY = 1.5

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGES_PER_NOTICE = 16
MAX_TEXT_CHARS = 60_000
MAX_PDF_PAGES_CHECKED = 40
MAX_LIST_PAGES = 40
LIST_PAGE_SIZE = 30

SECRET_KEYS = ("ANTHROPIC_API_KEY", "DISCORD_BOT_TOKEN", "GOOGLE_BRIDGE_URL", "GOOGLE_BRIDGE_SECRET")
BOARD_KINDS = ("cms", "program", "recruit")  # scrape.py가 읽을 줄 아는 게시판 종류. 새 파서를 만들면 여기에도 넣는다.


@dataclass(frozen=True)
class Board:
    """수집처 하나. kind가 읽는 방식을 정하고, 글이 많은 곳은 model로 작은 모델을 쓴다."""
    key: str
    name: str
    url: str
    kind: str = "cms"
    model: str | None = None
    pages: int = MAX_LIST_PAGES


# 기본값은 아주대 공개 게시판이다. 다른 학교나 게시판은 데이터 폴더의 boards.json으로 바꾼다.
DEFAULT_BOARDS = [
    Board("general", "일반공지", "https://www.ajou.ac.kr/kr/ajou/notice.do"),
    Board("scholarship", "장학공지", "https://www.ajou.ac.kr/kr/ajou/notice_scholarship.do"),
    Board("international", "국제교류", "https://oia.ajou.ac.kr/oia/notice/notice.do"),
    Board("software", "SW학과", "https://www.ajou.ac.kr/sw/board/notice.do"),
    Board("program", "비교과", "https://job.ajou.ac.kr/Career/CareerTask/ProgramList.aspx", kind="program", pages=5),
    Board("recruit", "채용", "https://job.ajou.ac.kr/Career/Job/RecruitList.aspx?tabs=1&outside=1",
          kind="recruit", model="claude-haiku-5-5", pages=5),
]


def load_boards(path: Path | None = None) -> list[Board]:
    """boards.json이 있으면 그 목록을, 없으면 기본 게시판을 쓴다. 형식은 examples/boards.example.json."""
    path = path or BOARDS_FILE
    if not path.exists():
        return list(DEFAULT_BOARDS)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SetupError(f"{path}를 읽을 수 없습니다: {error}") from error
    if not isinstance(rows, list) or not rows:
        raise SetupError(f"{path}는 게시판 객체의 목록이어야 합니다.")
    boards: list[Board] = []
    for row in rows:
        try:
            board = Board(**row)
        except TypeError as error:
            raise SetupError(f"{path}: 게시판 항목이 잘못되었습니다 ({error})") from error
        if board.kind not in BOARD_KINDS:
            raise SetupError(f"{path}: '{board.key}'의 kind '{board.kind}'를 모릅니다. 가능한 값: {', '.join(BOARD_KINDS)}")
        if not board.url.startswith(("http://", "https://")):
            raise SetupError(f"{path}: '{board.key}'의 url이 http(s) 주소가 아닙니다.")
        if any(other.key == board.key for other in boards):
            raise SetupError(f"{path}: 게시판 key '{board.key}'가 중복되었습니다.")
        boards.append(board)
    return boards


BOARDS = load_boards()


def _read_env_file() -> dict[str, str]:
    try:
        text = ENV_FILE.read_text()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


class Secrets(dict):
    """비밀값 사전. 없는 키를 부르면 어떻게 채우는지 알려 주는 SetupError를 낸다."""

    def __missing__(self, key: str):
        raise SetupError(f"{key} 값이 없습니다. setup/setup-secrets.sh를 실행하거나 {ENV_FILE}에 {key}=... 줄을 넣으세요.")


def load_secrets() -> Secrets:
    """env 파일의 값이 우선이고, 파일에 없는 비밀값만 환경변수에서 채운다."""
    values = Secrets({key: os.environ[key] for key in SECRET_KEYS if os.environ.get(key)})
    values.update(_read_env_file())
    return values


def setting(name: str, default: str = "") -> str:
    """환경변수, env 파일, 기본값 순으로 설정을 찾는다."""
    return os.environ.get(name) or _read_env_file().get(name) or default


def user_name() -> str:
    return setting("AJOU_USER_NAME", "사용자")


def school_name() -> str:
    return setting("AJOU_SCHOOL", "아주대학교")


REQUIRED_CHANNELS = ("urgent", "scholarship", "international", "general", "digest", "system", "chat", "career")


def load_discord(complete: bool = True) -> dict:
    """discord.json을 읽는다. complete면 서버·소유자·채널 ID가 숫자로 채워졌는지도 확인한다.

    `ajou style`은 비어 있는 채널을 만들어 채우는 명령이라 complete=False로 읽는다.
    """
    config = _read_discord()
    if complete:
        channels = config.get("channels", {})
        missing = [name for name in ("guild_id", "owner_id") if not str(config.get(name, "")).isdigit()]
        missing += [f"channels.{name}" for name in REQUIRED_CHANNELS if not str(channels.get(name, "")).isdigit()]
        if missing:
            raise SetupError(f"{DISCORD_FILE}에 숫자 ID가 비어 있습니다: {', '.join(missing)}. 채널은 `ajou style`로 만들 수 있습니다.")
    return config


def _read_discord() -> dict:
    try:
        return json.loads(DISCORD_FILE.read_text())
    except FileNotFoundError:
        raise SetupError(
            f"{DISCORD_FILE}이 없습니다. examples/discord.example.json을 복사해 값을 채우세요 (AJOU_HOME={HOME_DIR})."
        ) from None
    except json.JSONDecodeError as error:
        raise SetupError(f"{DISCORD_FILE}을 읽을 수 없습니다: {error}") from error


def load_channels() -> dict[str, str]:
    return load_discord()["channels"]
