"""모든 시험에 공통: 개발 컴퓨터의 실제 설정·상태 파일을 읽거나 쓰지 않는다.

11/16: 자동 진행 규칙표가 맥의 ~/Ajou/assistant/autopilot.json(옛 규칙)을 읽어, 같은 시험이 맥과 CI에서 다르게 돌았다.
"""
import os
import tempfile
from pathlib import Path

# 패키지를 불러오기 전에 정한다. config.py는 불러올 때 비밀값 파일에서 데이터 폴더 같은 경로를 읽는다(11/16: 맥에서는 실제
# ~/.config/ajou-assistant/env를 읽어 CI와 결과가 달랐다).
os.environ["AJOU_ENV_FILE"] = str(Path(__file__).parent / "no-such-env")
for name in ("AJOU_HOME", "AJOU_CONFIG_DIR"):
    os.environ.pop(name, None)
# 상태 파일(독촉 모음, 자동 진행 기록 등)도 시험마다 쓰는 임시 폴더로. 맥의 ~/.cache/ajou-assistant를 건드리지 않는다.
os.environ["AJOU_STATE_DIR"] = tempfile.mkdtemp(prefix="ajou-test-state-")

import pytest  # noqa: E402

from ajou_assistant import autopilot  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_autopilot(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "RULES_FILE", tmp_path / "autopilot-rules-none.json")
    monkeypatch.setattr(autopilot, "STATE_FILE", tmp_path / "autopilot-state.json")
