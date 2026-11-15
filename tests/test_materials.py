"""v0.6.0: [📎 자료 남기기] 버튼과, 공지 실행이 처음 알림에 연 스레드를 봇이 받아들이는 것."""
import asyncio
from types import SimpleNamespace

from ajou_assistant import bot, materials, record, store

from tests.test_lifecycle import OWNER, GUILD, helper  # noqa: F401  (fixture)


def write_log(folder, *turns):
    for when, who, text in turns:
        record.log_turn(folder, who, text, when)


def test_user_messages_and_files_make_one_page(tmp_path, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(materials, "HOME_DIR", tmp_path)
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path / "records" / "materials")
    folder = tmp_path / "outputs" / "threads" / "77"
    write_log(folder, (datetime(2026, 11, 11, 20, 48), "나", "스크립트 내용이야"),
              (datetime(2026, 11, 11, 20, 49), "비서", "정리했습니다"),
              (datetime(2026, 11, 11, 20, 50), "나", "### 소제목처럼 보이는 줄도 그대로"))
    (folder / "uploads").mkdir()
    (folder / "uploads" / "message.txt").write_text("특강 받아쓰기 전체")
    (folder / "notice").mkdir()
    (folder / "notice" / "notice.md").write_text("공지 원문 사본")
    path = materials.save(folder, "진로특강", "https://discord.com/channels/2/77")
    text = path.read_text()
    assert path.name == "2026-11-11_진로특강.md"
    assert "outputs/threads/77/uploads/message.txt" in text and "notice.md" not in text
    assert "스크립트 내용이야" in text and "### 소제목처럼" in text and "정리했습니다" not in text
    write_log(folder, (datetime(2026, 11, 12, 9, 0), "나", "추가 메모"))
    again = materials.save(folder, "진로특강", "x", str(path.relative_to(tmp_path)))
    assert again == path and "추가 메모" in path.read_text()
    assert materials.remove(str(path.relative_to(tmp_path))) and not path.exists()
    assert not materials.remove("PROFILE_PRIVATE.md")  # 자료 폴더 밖은 지우지 않는다
    assert materials.save(tmp_path / "outputs" / "threads" / "88", "빈 스레드", "x") is None


def test_keep_button_saves_then_only_undo_remains(helper, tmp_path, monkeypatch):  # noqa: F811
    from datetime import datetime
    monkeypatch.setattr(materials, "HOME_DIR", tmp_path)
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path / "records" / "materials")
    write_log(tmp_path / "outputs" / "threads" / "700", (datetime(2026, 11, 15, 19, 0), "나", "강의 노트"))
    helper.sessions["700"] = {"title": "강의"}
    edited, followups = [], []

    class Press:
        def __init__(self, custom):
            self.data = {"custom_id": custom}
            self.user, self.guild_id, self.channel = SimpleNamespace(id=OWNER), GUILD, None
            self.message = SimpleNamespace(id=5)

            async def edit_message(view=None):
                edited.append([child.custom_id for child in view.children])

            async def send(text, **kwargs):
                followups.append(text)
            self.response = SimpleNamespace(edit_message=edit_message)
            self.followup = SimpleNamespace(send=send)
    asyncio.run(helper.on_interaction(Press("ajou:keep:700")))
    assert edited[-1] == ["ajou:close:700", "ajou:unkeep:700"]
    saved = helper.sessions["700"]["material"]
    assert saved.startswith("records/materials/2026-11-15_") and (tmp_path / saved).exists() and "남겼어요" in followups[-1]
    asyncio.run(helper.on_interaction(Press("ajou:unkeep:700")))
    assert edited[-1] == ["ajou:close:700", "ajou:keep:700"] and "material" not in helper.sessions["700"]
    assert not (tmp_path / saved).exists()
    assert [child.custom_id for child in bot.close_view(700, again=True).children] == ["ajou:close:700", "ajou:keep:700"]
    assert bot.close_view(700, again=True).children[1].label == "새 글까지 남기기"


def test_bot_adopts_the_thread_the_hourly_run_opened_on_an_alert(tmp_path, monkeypatch):
    """재공지 알림을 위해 공지 실행이 처음 알림에 연 스레드. 봇은 그 스레드를 몰라도 공지와 이어 붙인다."""
    from ajou_assistant import repost
    monkeypatch.setattr(bot, "SESSIONS_FILE", tmp_path / "threads.json")
    monkeypatch.setattr(store, "recent_analysis", lambda days=None: [{"id": 7, "title": "OO장학금", "message_id": "555"}])
    assistant = bot.Assistant.__new__(bot.Assistant)
    assistant._connection = SimpleNamespace(user=SimpleNamespace(id=999))
    assistant.guild_id, assistant.sessions = GUILD, {}
    note = repost.note("[재공지] OO장학금", "https://x", 9, "마감 연장")
    message = SimpleNamespace(content=note, guild=SimpleNamespace(id=GUILD), channel=SimpleNamespace(id=555, name="OO장학금"))
    assistant.note_repost(message)
    assert assistant.sessions["555"]["notice"] == 7 and assistant.sessions["555"]["notes"][0]["id"] == "9"
    stranger = SimpleNamespace(content=note, guild=SimpleNamespace(id=GUILD), channel=SimpleNamespace(id=556, name="?"))
    assistant.note_repost(stranger)
    assert "556" not in assistant.sessions
