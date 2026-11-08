"""`ajou open`으로 연 Claude Code의 훅. 바뀐 파일과 턴 끝을 한 줄씩 적어 둔다(handoff.Shipper가 읽는다).

표준 라이브러리만 쓴다. `python -I handoff_hook.py file|stop <기록 파일>`로 실행된다. 무엇이 실패해도
작업을 막지 않도록 항상 0으로 끝난다. 파일 내용이나 도구 결과는 적지 않는다.
"""
import json
import sys


def main() -> int:
    try:
        kind, outbox = sys.argv[1], sys.argv[2]
        event = json.load(sys.stdin)
        if kind == "file":
            arguments = event.get("tool_input") or {}
            path = arguments.get("file_path") or arguments.get("notebook_path")
            row = {"file": str(path)} if path else None
        else:
            row = {"stop": str(event.get("last_assistant_message") or "")[:500]}
        if row:
            with open(outbox, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
