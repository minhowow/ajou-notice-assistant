"""기준을 넘으면 정해진 단계까지 미리 진행한다 (v0.7.0, 11/16 사용자 결정).

- AI(판단)는 사실만 뽑는다: 분류, 관련도, 자격, 마감, 남에게 부탁할 서류. 몇 단계까지 갈지는 이 코드가 규칙표로
  정한다. 같은 공지는 언제나 같은 단계이고, 공지 속 숨은 지시가 단계를 마음대로 올리지 못한다.
- 단계: 0 알림 · 1 캘린더 · 2 준비(체크리스트, 남에게 부탁할 서류의 부탁 메일 초안, 답장 초안) · 3 초안(지원서·신청서 양식).
- 1단계는 공지 실행과 메일 확인이 코드로 한다(캘린더). 2·3단계는 카드에 스레드를 열고 그 스레드의 Claude Code
  세션이 그 단계까지 진행한다. 사용자가 끼어들고 싶으면 그 스레드에 이어 쓰면 된다(11/16 사용자: 단계를 늘리지
  않고 채팅을 이어 가는 것으로).
- 바깥에 흔적이 남는 일(캘린더)은 믿을 수 있는 출처(학교 게시판, 학교 메일)만. 제출·발송은 어느 단계에도 없다.
- 같은 주제는 한 번만 진행한다(재공지 묶음 단위). 2단계 이상은 하루 상한이 있고, 23~8시에 온 것은 아침에 한다.
- 규칙표는 설정 폴더의 `autopilot.json`(없으면 DEFAULT). `shadow: true`면 단계만 카드에 적고 진행하지 않는다.
"""
from __future__ import annotations

import json
import re

from . import store
from .config import CONFIG_DIR, STATE_DIR

RULES_FILE = CONFIG_DIR / "autopilot.json"
STATE_FILE = STATE_DIR / "autopilot.json"
NAMES = {0: "알림", 1: "캘린더", 2: "준비", 3: "초안"}
AUTO_TAG = "-# 자동 진행 "
DEFAULT = {
    "daily_limit": 3,
    "quiet_hours": [23, 8],
    "shadow": False,
    "rules": [
        # 1단계: v0.6.6까지 코드에 박혀 있던 캘린더 규칙을 옮긴 것
        {"name": "관련 높은 공지의 일정", "when": {"source": "notice", "relevance": ["high"], "eligibility": ["likely", "unknown"]}, "level": 1},
        {"name": "자격이 확실한 보통 공지의 마감", "when": {"source": "notice", "relevance": ["medium"], "eligibility": ["likely"]}, "level": 1},
        {"name": "학교 메일의 날짜가 정해진 일", "when": {"source": "mail", "trusted": True, "has_events": True}, "level": 1},
        # 2단계: 준비
        {"name": "남에게 부탁할 서류가 있는 공지", "when": {"source": "notice", "relevance": ["high", "medium"],
                                                  "eligibility": ["likely", "unknown"], "has_asks": True}, "level": 2},
        {"name": "교수·교직원 메일의 할 일", "when": {"source": "mail", "trusted": True, "kind": ["교수·교직원"], "has_action": True}, "level": 2},
        # 3단계: 초안
        {"name": "자격 되는 장학·국제·활동", "when": {"source": "notice", "category": ["scholarship", "international", "activity"],
                                              "relevance": ["high"], "eligibility": ["likely"]}, "level": 3},
        # 상한
        {"name": "채용 공고는 캘린더까지", "when": {"board": ["recruit"]}, "max": 1},
    ],
}
TASKS = {
    2: ("[자동 진행 2단계: 준비] 이 스레드의 {what}을 보고 준비를 해 둔다.\n"
        "1. 자격과 마감을 내 상황(CURRENT_PROFILE.md, private_facts.md)과 대조해 한 줄로 정리한다.\n"
        "2. 준비물 체크리스트를 만든다: 서류, 누구에게 받는지, 언제까지(마감에서 거꾸로 센다).\n"
        "3. 남에게 부탁할 서류가 있으면 부탁 메일 초안을, 메일에 답해야 하면 답장 초안을 쓴다.\n"
        "결과는 작업 폴더에 `준비.md`로 저장하고, 스레드에는 세 줄 요약과 파일 이름만 쓴다. 제출과 발송은 하지 않는다."),
    3: ("[자동 진행 3단계: 초안] 이 스레드의 {what}을 보고 지원 초안까지 만든다.\n"
        "1. 먼저 2단계 준비를 한다: 자격·마감 정리, 준비물 체크리스트, 부탁 메일 초안(`준비.md`).\n"
        "2. 첨부에 지원서·신청서 양식이 있으면 칸 순서대로 내 정보로 채운 초안을 `초안.md`에 만든다. 양식이 없으면 "
        "자기소개·지원 동기 초안을 쓴다. 모르는 칸은 [확인 필요]로 비우고 지어내지 않는다.\n"
        "스레드에는 요약, 남은 빈칸, 파일 이름만 쓴다. 제출과 발송은 하지 않는다."),
}


def load_rules() -> dict:
    try:
        data = json.loads(RULES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT
    return {**DEFAULT, **{key: value for key, value in data.items() if not key.startswith("_")}}


def notice_facts(item, analysis, today) -> dict:
    left = [deadline for deadline in analysis.deadlines if deadline.date >= today.isoformat()]
    return {"source": "notice", "board": item.board_key, "category": analysis.category, "relevance": analysis.relevance,
            "eligibility": analysis.eligibility, "has_deadline": any(deadline.kind == "deadline" for deadline in left),
            "has_events": bool(left), "has_asks": bool(analysis.from_others), "trusted": True}


def mail_facts(mail, trusted: bool) -> dict:
    return {"source": "mail", "kind": mail.kind, "importance": mail.importance, "has_events": bool(mail.events),
            "has_action": bool(mail.action.strip()), "trusted": trusted}


def matches(when: dict, facts: dict) -> bool:
    for key, wanted in when.items():
        value = facts.get(key)
        if isinstance(wanted, list):
            if value not in wanted:
                return False
        elif value != wanted:
            return False
    return True


def decide(facts: dict, rules: dict | None = None) -> tuple[int, list[str]]:
    """(단계, 맞은 규칙 이름). 올리는 규칙 중 가장 높은 단계를, 상한 규칙 중 가장 낮은 상한으로 자른다."""
    rules = rules or load_rules()
    hit = [rule for rule in rules.get("rules", []) if matches(rule.get("when", {}), facts)]
    level = max([int(rule["level"]) for rule in hit if "level" in rule], default=0)
    cap = min([int(rule["max"]) for rule in hit if "max" in rule], default=3)
    return max(0, min(level, cap, 3)), [rule.get("name", "") for rule in hit]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    temporary.replace(STATE_FILE)


def badge(level: int, rules: dict | None = None, held: str = "") -> str:
    """카드에 붙이는 한 줄."""
    if level < 2:
        return ""
    rules = rules or load_rules()
    if rules.get("shadow"):
        return f"🤖 {level}단계({NAMES[level]}) 대상 · 그림자 운영이라 진행하지 않음"
    if held:
        return f"🤖 {level}단계({NAMES[level]}) 대상 · {held}"
    return f"🤖 {level}단계({NAMES[level]})까지 스레드에서 진행합니다. 끼어들려면 그 스레드에 쓰세요."


def claim(key: str, level: int, rules: dict | None = None) -> str:
    """2단계 이상을 진행해도 되는지. 된다면 ""를, 아니면 이유를 돌려준다. 되면 기록까지 한다(한 번만)."""
    rules = rules or load_rules()
    if level < 2:
        return "단계 낮음"
    if rules.get("shadow"):
        return "그림자 운영"
    state = load_state()
    done = state.setdefault("done", {})
    if int(done.get(key, 0)) >= level:
        return "이미 진행함"
    day = store.now().date().isoformat()
    days = state.setdefault("days", {})
    if days.get(day, 0) >= int(rules.get("daily_limit", 3)):
        return "오늘 상한이라 진행하지 않음"
    done[key] = level
    days[day] = days.get(day, 0) + 1
    state["days"] = dict(list(days.items())[-14:])
    save_state(state)
    return ""


def task_line(level: int, key: str, what: str) -> str:
    """스레드에 남기는 진행 표시. 마지막 줄의 표시로 봇이 알아보고 정해진 지시로 턴을 돌린다."""
    return (f"🤖 {level}단계({NAMES[level]})를 진행합니다. 끼어들고 싶으면 여기에 이어 쓰세요.\n"
            f"{AUTO_TAG}{level} {what} {key}")


def parse_tag(text: str) -> tuple[int, str, str] | None:
    """(단계, 대상 종류, 키). 표시가 없으면 None."""
    found = re.search(r"^" + re.escape(AUTO_TAG) + r"([23]) (\S+) (\S+)\s*$", text or "", re.M)
    return (int(found.group(1)), found.group(2), found.group(3)) if found else None


def task_prompt(level: int, what: str) -> str:
    return TASKS[level].format(what={"notice": "공지", "mail": "메일"}.get(what, what))


def quiet_now(rules: dict | None = None) -> bool:
    rules = rules or load_rules()
    start, end = (rules.get("quiet_hours") or [23, 8])[:2]
    hour = store.now().hour
    return hour >= start or hour < end


def start_on_card(discord, channel: str, message_id: str, title: str, level: int, key: str, what: str) -> str | None:
    """카드에 스레드를 열고(이미 있으면 그 스레드) 진행 표시를 남긴다. 봇이 그 표시를 보고 턴을 돌린다."""
    from .notify import SUPPRESS_EMBEDS, Tier
    message = discord.fetch(channel, message_id)
    thread = (message.get("thread") or {}).get("id") or discord.open_thread(channel, message_id, title)
    if thread:
        discord.send(thread, {"content": task_line(level, key, what), "flags": SUPPRESS_EMBEDS}, tier=Tier.QUIET)
    return thread
