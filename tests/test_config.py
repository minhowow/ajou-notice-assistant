"""설정 분리 시험: 데이터 폴더, 비밀값, 게시판 파일, 선택 기능이 환경변수만으로 바뀌는지 본다."""
import json
from pathlib import Path

import pytest

from ajou_assistant import config, sync
from ajou_assistant.config import SetupError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_home_defaults_to_current_folder_and_follows_ajou_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AJOU_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config._home() == tmp_path.resolve()
    other = tmp_path / "data"
    monkeypatch.setenv("AJOU_HOME", str(other))
    assert config._home() == other.resolve()


def test_data_paths_all_live_under_home():
    for path in (config.AUTO, config.PROFILE_FILE, config.PRIVATE_FACTS_FILE, config.THREAD_DIR,
                 config.DISCORD_FILE, config.BOARDS_FILE, config.SEEN_FILE, config.USAGE_DIR):
        assert config.HOME_DIR in path.parents or path == config.HOME_DIR


def test_secrets_file_wins_and_environment_fills_gaps(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text("# 주석\nANTHROPIC_API_KEY=from-file\n\nAJOU_USER_NAME=가나다\n")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-environment")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token-from-environment")
    monkeypatch.delenv("AJOU_USER_NAME", raising=False)
    secrets = config.load_secrets()
    assert secrets["ANTHROPIC_API_KEY"] == "from-file"
    assert secrets["DISCORD_BOT_TOKEN"] == "token-from-environment"
    assert config.user_name() == "가나다"


def test_missing_secret_explains_how_to_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "없음")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SetupError, match="setup-secrets.sh"):
        config.load_secrets()["ANTHROPIC_API_KEY"]
    assert "GOOGLE_BRIDGE_URL" not in config.load_secrets()


def test_defaults_are_neutral(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "없음")
    monkeypatch.delenv("AJOU_USER_NAME", raising=False)
    monkeypatch.delenv("AJOU_SCHOOL", raising=False)
    assert config.user_name() == "사용자"
    assert config.school_name() == "아주대학교"


def test_boards_default_and_override(tmp_path):
    assert [board.key for board in config.load_boards(tmp_path / "없음.json")] == [board.key for board in config.DEFAULT_BOARDS]
    path = tmp_path / "boards.json"
    path.write_text(json.dumps([{"key": "a", "name": "공지", "url": "https://example.ac.kr/n"},
                                {"key": "b", "name": "채용", "url": "https://example.ac.kr/j", "kind": "recruit",
                                 "model": "claude-haiku-5-5", "pages": 3}]))
    boards = config.load_boards(path)
    assert [(b.key, b.kind, b.pages) for b in boards] == [("a", "cms", config.MAX_LIST_PAGES), ("b", "recruit", 3)]


@pytest.mark.parametrize("rows, message", [
    ([{"key": "a", "name": "n", "url": "https://x.kr", "kind": "mystery"}], "kind"),
    ([{"key": "a", "name": "n", "url": "ftp://x.kr"}], "http"),
    ([{"key": "a", "name": "n", "url": "https://x.kr"}, {"key": "a", "name": "m", "url": "https://y.kr"}], "중복"),
    ([{"key": "a", "name": "n"}], "잘못"),
    ([], "목록"),
])
def test_bad_boards_file_is_rejected_with_a_reason(tmp_path, rows, message):
    path = tmp_path / "boards.json"
    path.write_text(json.dumps(rows))
    with pytest.raises(SetupError, match=message):
        config.load_boards(path)


def test_example_files_are_valid_and_empty_of_ids():
    assert [board.key for board in config.load_boards(EXAMPLES / "boards.example.json")] == ["notice", "jobs"]
    discord = json.loads((EXAMPLES / "discord.example.json").read_text())
    assert discord["guild_id"] == "" and discord["owner_id"] == "" and discord["calendar_account"] == ""
    assert set(config.REQUIRED_CHANNELS) <= set(discord["channels"])
    assert all(value == "" for value in discord["channels"].values())
    for name in ("CURRENT_PROFILE.example.md", "private_facts.example.md"):
        assert (EXAMPLES / name).exists()


def test_discord_config_must_be_filled_in(tmp_path, monkeypatch):
    path = tmp_path / "discord.json"
    monkeypatch.setattr(config, "DISCORD_FILE", path)
    with pytest.raises(SetupError, match="discord.example.json"):
        config.load_discord()
    path.write_text((EXAMPLES / "discord.example.json").read_text())
    with pytest.raises(SetupError, match="guild_id"):
        config.load_discord()
    assert config.load_discord(complete=False)["channels"]["urgent"] == ""
    filled = json.loads(path.read_text())
    filled.update(guild_id="1", owner_id="2", channels={name: "3" for name in config.REQUIRED_CHANNELS})
    path.write_text(json.dumps(filled))
    assert config.load_discord()["owner_id"] == "2"


def test_profile_is_required_and_private_facts_are_optional(tmp_path, monkeypatch):
    from ajou_assistant import triage
    profile, facts = tmp_path / "CURRENT_PROFILE.md", tmp_path / "private_facts.md"
    monkeypatch.setattr(triage, "PROFILE_FILE", profile)
    monkeypatch.setattr(triage, "PRIVATE_FACTS_FILE", facts)
    with pytest.raises(SetupError, match="CURRENT_PROFILE.example.md"):
        triage.load_profile()
    profile.write_text("프로필")
    assert triage.load_profile() == "프로필"
    facts.write_text("개인 사실")
    assert triage.load_profile().endswith("개인 사실")
    facts.write_bytes(b"\x00GITCRYPT\x00...")  # 복호화 전 암호문은 AI에 보내지 않는다
    assert triage.load_profile() == "프로필"


def test_prompts_use_configured_names(monkeypatch):
    from ajou_assistant import bot, record, triage
    monkeypatch.setenv("AJOU_USER_NAME", "홍길동")
    monkeypatch.setenv("AJOU_SCHOOL", "예시대학교")
    command = bot.claude_command("outputs/threads/1", None)
    assert "홍길동" in command[command.index("--append-system-prompt") + 1]
    assert "홍길동" in record.RULES.format(user=config.user_name())
    assert "예시대학교" in triage.RULES.format(school=config.school_name())
    assert "예시대학교" in triage.PREFILTER_RULES.format(school=config.school_name())


def test_git_sync_is_off_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "없음")
    monkeypatch.delenv("AJOU_GIT_SYNC", raising=False)
    monkeypatch.setattr(sync, "HOME_DIR", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(sync, "git", lambda *args: pytest.fail("동기화가 꺼져 있는데 git을 불렀다"))
    assert not sync.enabled()
    assert sync.sync_pull() is None and sync.sync_push("m", "auto") is None and sync.sync_all("m") is None
    monkeypatch.setenv("AJOU_GIT_SYNC", "1")
    assert sync.enabled()
    (tmp_path / ".git").rmdir()
    assert not sync.enabled()  # git 저장소가 아니면 켜 두어도 하지 않는다


def test_data_paths_can_come_from_the_env_file(tmp_path, monkeypatch):
    import importlib
    from ajou_assistant import config
    env = tmp_path / "env"
    env.write_text(f"AJOU_HOME={tmp_path / 'data'}\nAJOU_CONFIG_DIR={tmp_path / 'data' / 'conf'}\n")
    monkeypatch.setenv("AJOU_ENV_FILE", str(env))
    monkeypatch.delenv("AJOU_HOME", raising=False)
    monkeypatch.delenv("AJOU_CONFIG_DIR", raising=False)
    try:
        importlib.reload(config)
        assert config.HOME_DIR == (tmp_path / "data").resolve()
        assert config.DISCORD_FILE == (tmp_path / "data" / "conf" / "discord.json").resolve()
    finally:
        monkeypatch.undo()
        importlib.reload(config)
