"""Discord 서버 모양을 코드로 맞춘다. 여러 번 실행해도 결과가 같다.

봇에 '서버 관리' 권한이 없으면 서버 아이콘만 건너뛰고 나머지를 적용한다.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from .config import DISCORD_FILE, SetupError, load_discord, load_secrets, setting

API = "https://discord.com/api/v10"
ICON = Path(__file__).resolve().parents[1] / "assets" / "icon.png"
CATEGORY = setting("AJOU_DISCORD_CATEGORY", "아주대 비서")  # 서버에 이 이름의 카테고리를 먼저 만들어 둔다.
CHANNELS = [  # (키, 이름, 설명)
    ("urgent", "🚨긴급", "7일 안에 마감되는 중요 공지. 강한 알림"),
    ("scholarship", "🎓장학", "장학 공지"),
    ("international", "🌏국제교류", "교환학생·해외 프로그램"),
    ("career", "💼취업-비교과", "채용·인턴·비교과 프로그램"),
    ("general", "📢공지", "그 밖의 관련 공지"),
    ("chat", "💬대화", "여기에 쓰면 비서와 새 스레드를 엽니다"),
    ("study", "📖공부", "강의 받아쓰기·자료·질문을 올리면 과목별 노트와 확인 문제를 만듭니다"),
    ("english", "📚영어", "단어 글을 붙여 넣으면 정리하고 아침·밤에 복습 문제를 냅니다. 복습·현황"),
    ("pins", "📌핀", "어느 채널에서든 핀한 메시지가 모입니다. 핀을 풀면 빠집니다"),
    ("digest", "🗓일일요약", "매일 21:30 요약"),
    ("records", "📝기록", "주간 정리와 대화 기록"),
    ("system", "⚙시스템", "장애와 복구 알림"),
]


def apply_style() -> int:
    token = load_secrets()["DISCORD_BOT_TOKEN"]
    client = httpx.Client(base_url=API, timeout=30, headers={
        "Authorization": f"Bot {token}", "User-Agent": "DiscordBot (ajou-assistant, 0.2)"})
    config = load_discord(complete=False)
    if not str(config.get("guild_id", "")).isdigit():
        raise SetupError(f"{DISCORD_FILE}의 guild_id를 먼저 채우세요.")
    guild = config["guild_id"]
    existing = {row["id"]: row for row in client.get(f"/guilds/{guild}/channels").raise_for_status().json()}
    parent = next((row["id"] for row in existing.values() if row["type"] == 4 and row["name"] == CATEGORY), None)
    if parent is None:
        raise SetupError(f"Discord 서버에 '{CATEGORY}' 카테고리를 먼저 만드세요 (이름은 AJOU_DISCORD_CATEGORY로 바꿀 수 있음).")
    channels = config["channels"]
    if not channels.get("chat"):  # Discord가 기본으로 만든 '일반' 채널을 대화 채널로 쓴다.
        default = next((row for row in existing.values() if row["type"] == 0 and row["name"] == "일반"), None)
        if default:
            channels["chat"] = default["id"]
    for position, (key, name, topic) in enumerate(CHANNELS):
        body = {"name": name, "topic": topic, "parent_id": parent, "position": position}
        if channels.get(key):
            current = existing.get(channels[key], {})
            if all(current.get(k) == v for k, v in body.items()):
                continue
            client.patch(f"/channels/{channels[key]}", json=body).raise_for_status()
        else:
            created = client.post(f"/guilds/{guild}/channels", json={**body, "type": 0}).raise_for_status().json()
            channels[key] = created["id"]
        print("채널", name)
    DISCORD_FILE.write_text(json.dumps({**config, "channels": channels}, ensure_ascii=False, indent=2) + "\n")
    image = "data:image/png;base64," + base64.b64encode(ICON.read_bytes()).decode()
    me = client.get("/users/@me").raise_for_status().json()
    if not me.get("avatar"):
        client.patch("/users/@me", json={"avatar": image}).raise_for_status()
        print("봇 아이콘 적용")
    info = client.get(f"/guilds/{guild}").raise_for_status().json()
    if info.get("icon"):
        print("서버 아이콘 이미 있음")
        return 0
    response = client.patch(f"/guilds/{guild}", json={"icon": image})
    print("서버 아이콘", "적용" if response.status_code == 200 else f"보류: 권한 필요 ({response.status_code})")
    return 0
