#!/usr/bin/env python3
"""저장소 안 마크다운의 상대 링크와 앵커가 살아 있는지 본다.

섹션을 하나 끼워 넣어 번호가 밀리면 `USAGE.md#6-안-될-때` 같은 링크가 조용히
깨진다. 실제로 그렇게 깨뜨린 적이 있어서 CI 에서 본다.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def anchors(path: Path) -> set[str]:
    """GitHub 이 제목에서 만드는 앵커. 한글은 그대로 두고 기호만 턴다."""
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = HEADING.match(line)
        if not m:
            continue
        t = m.group(1).strip().lower()
        t = re.sub(r"[`*_]", "", t)
        t = re.sub(r"[^\w\s가-힣-]", "", t)
        out.add(t.replace(" ", "-"))
    return out


def main() -> int:
    bad: list[str] = []
    files = [p for p in ROOT.rglob("*.md")
             if ".git" not in p.parts and "node_modules" not in p.parts]

    for f in files:
        for link in LINK.findall(f.read_text(encoding="utf-8")):
            if link.startswith(("http://", "https://", "mailto:", "#!")):
                continue
            target, _, frag = link.partition("#")
            path = (f.parent / target).resolve() if target else f
            rel = f.relative_to(ROOT)
            if not path.exists():
                bad.append(f"{rel}: 대상 없음 -> {link}")
                continue
            if frag and path.suffix == ".md":
                if urllib.parse.unquote(frag).lower() not in anchors(path):
                    bad.append(f"{rel}: 앵커 없음 -> {link}")

    for line in bad:
        print(line, file=sys.stderr)
    print(f"마크다운 {len(files)}개, 링크 오류 {len(bad)}개")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
