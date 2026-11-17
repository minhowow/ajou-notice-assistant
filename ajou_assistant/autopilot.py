"""기준을 넘으면 정해진 단계까지 미리 진행한다 (v0.7.0, 11/16 사용자 결정).

- AI(판단)는 사실만 뽑는다: 분류, 관련도, 자격, 마감, 남에게 부탁할 서류. 몇 단계까지 갈지는 이 코드가 규칙표로
  정한다. 같은 공지는 언제나 같은 단계이고, 공지 속 숨은 지시가 단계를 마음대로 올리지 못한다.
- 단계: 0 알림 · 1 캘린더 · 2 준비(체크리스트, 남에게 부탁할 서류의 부탁 메일 초안, 답장 초안) · 3 초안(지원서·신청서 양식).
- 1단계는 공지 실행과 메일 확인이 코드로 한다(캘린더). 2·3단계는 카드에 스레드를 열고 그 스레드의 Claude Code
  세션이 그 단계까지 진행한다. 다 자동으로 끝나므로 사용자는 끼어들지 않고, 이어서 할 일이 있으면 그 스레드에
  쓴다(11/16 사용자: 끼어들기는 사실상 후속 진행뿐이다).
- 카드와 스레드에는 'N단계'가 아니라 판정된 범위에서 실제로 할 일을 적는다(plan). Claude 지시도 같은 목록으로 만든다
  (11/16 사용자: 신청서까지 작성한다, 캘린더에 추가한다처럼 자세히).
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
    "followup_limit": 5,  # 대화가 멈춘 뒤 비서가 남은 문서 일을 마무리하는 후속 정리(record.followup_prompt) 하루 상한
    "quiet_hours": [23, 8],
    "shadow": False,
    "rules": [
        # 1단계: v0.6.6까지 코드에 박혀 있던 캘린더 규칙을 옮긴 것
        {"name": "관련 높은 공지의 일정", "when": {"source": "notice", "relevance": ["high"], "eligibility": ["likely", "unknown"]}, "level": 1},
        {"name": "자격이 확실한 보통 공지의 마감", "when": {"source": "notice", "relevance": ["medium"], "eligibility": ["likely"]}, "level": 1},
        # 메일은 v0.7.1부터 이 학생 기준 관련도가 있다. 학교 발신이라고 모두 넣지 않는다(11/16 선배만남의 날 사례).
        {"name": "나와 관련 높은 학교 메일의 일정", "when": {"source": "mail", "trusted": True, "has_events": True, "relevance": ["high"]}, "level": 1},
        {"name": "내 수업(아주Bb) 메일의 일정", "when": {"source": "mail", "blackboard": True, "has_events": True}, "level": 1},
        # 2단계: 준비
        {"name": "남에게 부탁할 서류가 있는 공지", "when": {"source": "notice", "relevance": ["high", "medium"],
                                                  "eligibility": ["likely", "unknown"], "has_asks": True}, "level": 2},
        {"name": "교수·교직원 메일의 할 일", "when": {"source": "mail", "trusted": True, "kind": ["교수·교직원"], "has_action": True,
                                              "relevance": ["high", "medium"]}, "level": 2},
        # 3단계: 초안
        {"name": "자격 되는 장학·국제·활동", "when": {"source": "notice", "category": ["scholarship", "international", "activity"],
                                              "relevance": ["high"], "eligibility": ["likely"]}, "level": 3},
        # 상한
        {"name": "채용 공고는 캘린더까지", "when": {"board": ["recruit"]}, "max": 1},
    ],
}
CALENDAR_STEP = "일정을 캘린더에 넣음"
FILES = ("결과는 작업 폴더에 남긴다: 체크리스트는 `준비.md`, 지원서·신청서 초안은 `초안.md`. 메일은 create_draft로 Gmail 초안을 "
         "만들고(답장이면 replyToMessageId), 받는 사람·제목·초안 링크를 `준비.md`와 스레드에 적는다. 받는 사람 주소를 모르면 비워 두고 [확인 필요]")


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
            "has_events": bool(left), "has_asks": bool(analysis.from_others), "trusted": True,
            "asks": [f"{prep.who}: {prep.document}" for prep in analysis.from_others][:3]}


def plan(level: int, facts: dict) -> list[str]:
    """판정된 단계에서 실제로 할 일. 카드·스레드 안내와 Claude 지시에 같이 쓴다."""
    steps = []
    if level >= 1 and facts.get("has_events"):
        steps.append(CALENDAR_STEP)
    if level >= 2:
        steps.append("자격·마감을 내 상황과 대조해 정리하고, 마감에서 거꾸로 센 준비물 체크리스트를 만듦")
        if facts.get("asks"):
            steps.append("남에게 부탁할 서류의 부탁 메일을 Gmail 초안으로 (" + ", ".join(facts["asks"]) + ")")
        if facts.get("source") == "mail" and facts.get("has_action"):
            steps.append("이 메일에 보낼 답장을 Gmail 초안으로 (원래 메일 스레드에)")
    if level >= 3:
        steps.append("지원서·신청서 양식을 내 정보로 채운 초안 (모르는 칸은 [확인 필요])")
    return steps


def mail_facts(mail, trusted: bool) -> dict:
    from .mail import from_blackboard
    return {"source": "mail", "kind": mail.kind, "importance": mail.importance, "relevance": mail.relevance,
            "has_events": bool(mail.events), "has_action": bool(mail.action.strip()), "trusted": trusted,
            "blackboard": from_blackboard(mail)}


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


def badge(level: int, rules: dict | None = None, held: str = "", steps: list[str] | None = None) -> str:
    """카드에 붙이는 안내. 판정된 범위에서 할 일을 그대로 적는다."""
    if level < 2:
        return ""
    rules = rules or load_rules()
    listing = "\n".join(f"· {step}" for step in (steps or [])) or f"· {NAMES[level]} 단계"
    if rules.get("shadow"):
        return f"해 둘 일(그림자 운영이라 하지 않음):\n{listing}"
    if held:
        return f"해 둘 일({held}):\n{listing}\n스레드에 '진행해'라고 쓰면 합니다."
    return f"해 둡니다. 결과는 이 카드의 스레드에 올라옵니다:\n{listing}"


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


def task_line(level: int, key: str, what: str, steps: list[str] | None = None) -> str:
    """스레드에 남기는 진행 표시. 할 일 목록이 Claude 지시가 되고, 마지막 줄의 표시로 봇이 알아본다."""
    target = {"notice": "공지는", "mail": "메일은"}.get(what, what)
    listing = "\n".join(f"- {step}" for step in (steps or []))
    return (f"🤖 이 {target} 여기까지 해 둡니다:\n{listing}\n끝나면 결과를 여기에 올립니다. 이어서 할 일이 있으면 여기에 쓰세요.\n"
            f"{AUTO_TAG}{level} {what} {key}")


def steps_from(text: str) -> list[str]:
    """진행 표시 글에서 할 일 목록을 다시 꺼낸다(봇이 Claude 지시를 만들 때)."""
    return [line[2:].strip() for line in (text or "").splitlines() if line.startswith("- ")]


def parse_tag(text: str) -> tuple[int, str, str] | None:
    """(단계, 대상 종류, 키). 표시가 없으면 None."""
    found = re.search(r"^" + re.escape(AUTO_TAG) + r"([23]) (\S+) (\S+)\s*$", text or "", re.M)
    return (int(found.group(1)), found.group(2), found.group(3)) if found else None


def task_prompt(level: int, what: str, steps: list[str] | None = None) -> str:
    """자동 진행 지시. 판정된 범위의 할 일만 한다. 캘린더는 코드가 이미 넣었다."""
    target = {"notice": "공지를", "mail": "메일을"}.get(what, what)
    work = [step for step in (steps or plan(level, {"source": what})) if step != CALENDAR_STEP]
    listing = "\n".join(f"{index}. {step}" for index, step in enumerate(work, start=1))
    return (f"[자동 진행] 이 스레드의 {target} 보고 아래만 해 둔다. 범위는 규칙표로 이미 정해졌다. 더 하지 않는다.\n{listing}\n"
            f"- 내 상황은 CURRENT_PROFILE.md와 private_facts.md를 본다. 경험이나 수치를 지어내지 않고, 모르는 것은 [확인 필요]로 둔다.\n"
            f"- {FILES}\n- 스레드에는 한 일, 남은 빈칸, 파일 이름만 짧게 쓴다. 캘린더는 이미 넣었다. 제출과 발송은 하지 않는다.")


def quiet_now(rules: dict | None = None) -> bool:
    rules = rules or load_rules()
    start, end = (rules.get("quiet_hours") or [23, 8])[:2]
    hour = store.now().hour
    return hour >= start or hour < end


def start_on_card(discord, channel: str, message_id: str, title: str, level: int, key: str, what: str,
                  steps: list[str] | None = None) -> str | None:
    """카드에 스레드를 열고(이미 있으면 그 스레드) 진행 표시를 남긴다. 봇이 그 표시를 보고 턴을 돌린다."""
    from .notify import SUPPRESS_EMBEDS, Tier
    message = discord.fetch(channel, message_id)
    thread = (message.get("thread") or {}).get("id") or discord.open_thread(channel, message_id, title)
    if thread:
        discord.send(thread, {"content": task_line(level, key, what, steps), "flags": SUPPRESS_EMBEDS}, tier=Tier.QUIET)
    return thread


def claim_followup(key: str, rules: dict | None = None) -> bool:
    """후속 정리를 해도 되는지. 하루 상한(followup_limit) 안이면 세고 True."""
    rules = rules or load_rules()
    state = load_state()
    day = store.now().date().isoformat()
    days = state.setdefault("followups", {})
    if days.get(day, 0) >= int(rules.get("followup_limit", 5)):
        return False
    days[day] = days.get(day, 0) + 1
    state["followups"] = dict(list(days.items())[-14:])
    save_state(state)
    return True
