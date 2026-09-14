#!/usr/bin/env python3
"""토큰 발급/폐기.

사용법:
    python issue_token.py <user>            새 토큰 발급 (토큰은 한 번만 출력, 서버에는 해시만 저장)
    python issue_token.py --revoke <user>   해당 사용자의 모든 토큰 폐기
"""

import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

TOKENS_PATH = Path(os.environ.get("PEERS_TOKENS", "./tokens.json"))
USER_RE = re.compile(r"^[a-z0-9._-]{1,40}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load() -> dict:
    if TOKENS_PATH.exists():
        return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    return {}


def save(tokens: dict) -> None:
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8")
    TOKENS_PATH.chmod(0o600)


def main(argv: list[str]) -> int:
    a = argv[0] if argv else None
    b = argv[1] if len(argv) > 1 else None
    tokens = load()

    if a == "--revoke" and b:
        n = 0
        for entry in tokens.values():
            if entry.get("user") == b and not entry.get("revoked"):
                entry["revoked"] = _now()
                n += 1
        save(tokens)
        print(f"{b}: 토큰 {n}개 폐기", file=sys.stderr)
        return 0

    if a and USER_RE.match(a):
        token = "pk_" + secrets.token_urlsafe(24)
        tokens[sha256(token.encode()).hexdigest()] = {"user": a, "created": _now()}
        save(tokens)
        print(token)
        return 0

    print("usage: python issue_token.py <user>  |  python issue_token.py --revoke <user>", file=sys.stderr)
    print("user는 소문자, 숫자, . _ - 만 사용 (예: 사내 계정 ID)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
