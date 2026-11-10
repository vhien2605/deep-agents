"""JSON-lines bridge between the VS Code extension and the chat agent.

The extension spawns `uv run python -m src.chat_server` and talks over stdio:

  request  (one line on stdin):  {"id": "...", "thread_id": "...",
                                   "workspace": "/abs/path", "message": "..."}
  events   (lines on stdout):    {"type": "ready"}                       once, at start
                                 {"id": "...", "type": "text",  "text": "<token delta>"}
                                 {"id": "...", "type": "tool",  "text": "<step label>"}
                                 {"id": "...", "type": "error", "text": "..."}
                                 {"id": "...", "type": "done"}

Requests are handled one at a time. Anything else a library prints goes to
stderr, so stdout carries only protocol lines.
"""

import json
import sys
import traceback

from src.chat import create_chat_agent, expand_skill_command, stream_reply


def main() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr
    agents: dict[str, object] = {}

    def emit(event: dict) -> None:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")
        out.flush()

    emit({"type": "ready"})
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            workspace = request["workspace"]
            if workspace not in agents:
                agents[workspace] = create_chat_agent(workspace)
            message = expand_skill_command(request["message"])
            for kind, text in stream_reply(agents[workspace], message, request["thread_id"]):
                emit({"id": request_id, "type": kind, "text": text})
        except Exception as exc:  # report to the chat instead of killing the bridge
            traceback.print_exc()
            emit({"id": request_id, "type": "error", "text": f"{type(exc).__name__}: {exc}"})
        emit({"id": request_id, "type": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
