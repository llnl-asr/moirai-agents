#!/usr/bin/env python3
"""UserPromptSubmit hook: put what is known about this prompt in front of the turn. Whatever goes wrong, the prompt goes on with nothing added."""
import json
import sys


def main() -> int:
    try:
        p = json.load(sys.stdin)
        from moirai_agents.core import knowledge
        print(knowledge.pack(p.get("prompt") or "", str(p.get("session_id") or "hook")), end="")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
