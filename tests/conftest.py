"""모든 시험에 공통: 개발 컴퓨터의 실제 설정·상태 파일을 읽거나 쓰지 않는다.

11/16: 자동 진행 규칙표가 맥의 ~/Ajou/assistant/autopilot.json(옛 규칙)을 읽어, 같은 시험이 맥과 CI에서 다르게 돌았다.
"""
import pytest

from ajou_assistant import autopilot


@pytest.fixture(autouse=True)
def isolated_autopilot(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "RULES_FILE", tmp_path / "autopilot-rules-none.json")
    monkeypatch.setattr(autopilot, "STATE_FILE", tmp_path / "autopilot-state.json")
