#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=13", "httpx>=0.27"]
# ///
"""peers 연결 진단.

"설치는 됐는데 모든 도구가 409" 처럼 증상만 보이고 원인이 안 보일 때 씁니다.
REST 와 WebSocket 을 따로 시험해서 어느 쪽이 왜 깨지는지 짚어 줍니다.

  uv run --script doctor.py https://peers.soldoc.co.kr <토큰>

워크스페이스 이름을 직접 주고 싶으면 세 번째 인자로 넘깁니다.
생략하면 현재 디렉터리 이름을 씁니다 — 실제 세션이 하는 것과 같습니다.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

OK, BAD, WARN = "  ✓", "  ✗", "  !"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")
    token = sys.argv[2]
    raw_ws = sys.argv[3] if len(sys.argv) > 3 else Path(os.getcwd()).name
    sid = str(uuid.uuid4())

    print(f"\n브로커   {base}")
    print(f"워크스페이스 {raw_ws!r}")
    print(f"토큰     {token[:8]}…\n")

    # 1) 워크스페이스 이름이 헤더에 담길 수 있는가
    print("[1] 헤더에 담을 수 있는 이름인가")
    if raw_ws.isascii():
        ws_name = re.sub(r"[^\w.-]", "_", raw_ws, flags=re.ASCII)[:64] or "ws"
        print(f"{OK} ASCII — 그대로 보냅니다: {ws_name!r}")
    else:
        print(f"{WARN} 비ASCII 문자가 있습니다 (한글 디렉터리 이름 등)")
        print("      구버전(0.1.0) 플러그인은 이 경우 WebSocket 연결이 통째로 실패합니다.")
        print("      0.2.0 이상은 해시를 붙인 이름으로 바꿔 보냅니다.")
        ws_name = "doctor-ascii-fallback"
        print(f"      이 진단은 {ws_name!r} 로 대신 시험합니다.")

    # 2) REST
    print("\n[2] REST (/api/peers)")
    hdr = {"authorization": f"Bearer {token}", "x-peers-session": sid}
    try:
        r = httpx.get(f"{base}/api/peers", headers=hdr, timeout=15)
        if r.status_code == 200:
            print(f"{OK} 200 — 세션이 이미 등록돼 있습니다")
        elif r.status_code == 409:
            print(f"{OK} 409 — 정상입니다. WebSocket 이 아직 없어서 나는 응답입니다")
        elif r.status_code == 401:
            print(f"{BAD} 401 — 토큰이 이 브로커에서 유효하지 않습니다")
            print("      다른 브로커의 토큰을 쓰고 있을 수 있습니다. 새로 발급받으세요.")
            return 1
        else:
            print(f"{BAD} {r.status_code}: {r.text[:120]}")
            return 1
    except Exception as e:
        print(f"{BAD} {type(e).__name__}: {str(e)[:150]}")
        print("      브로커에 닿지 못합니다. 주소, 방화벽(SG 화이트리스트), VPN 을 확인하세요.")
        return 1

    # 3) WebSocket — 실제로 세션을 등록하는 경로
    print("\n[3] WebSocket (/stream)")
    url = re.sub(r"^http", "ws", base) + "/stream"
    headers = {
        "authorization": f"Bearer {token}",
        "x-peers-session": sid,
        "x-peers-workspace": ws_name,
        "x-peers-listen": "1",
        "x-peers-room": "public",
        "x-peers-room-subject": quote("doctor 진단", safe=""),
    }

    async def probe() -> int:
        try:
            async with websockets.connect(url, additional_headers=headers, open_timeout=20):
                print(f"{OK} 연결 성공 — 세션이 등록됐습니다")
                r = httpx.get(f"{base}/api/peers", headers=hdr, timeout=15)
                if r.status_code == 200:
                    print(f"{OK} 이 상태에서 /api/peers 가 200 입니다. 정상 동작합니다.")
                else:
                    print(f"{WARN} WebSocket 은 붙었는데 /api/peers 가 {r.status_code} 입니다")
                return 0
        except Exception as e:
            name = type(e).__name__
            msg = str(e)[:160]
            print(f"{BAD} {name}: {msg}")
            if "InvalidHeaderValue" in name or "invalid" in msg.lower():
                print("      헤더에 담을 수 없는 값이 있습니다. 플러그인이 구버전일 가능성이 큽니다.")
                print("      claude plugin update peers@claude-peers  (0.2.0 이상 필요)")
            elif "400" in msg:
                print("      리버스 프록시가 WebSocket upgrade 를 넘기지 못하고 있습니다. 서버 담당자에게 알리세요.")
            elif "401" in msg:
                print("      토큰이 유효하지 않습니다.")
            else:
                print("      연결 자체가 안 됩니다. 방화벽(SG), VPN, 프록시 환경변수를 확인하세요.")
                print(f"      HTTPS_PROXY={os.environ.get('HTTPS_PROXY') or '(없음)'}")
            return 1

    return asyncio.run(probe())


if __name__ == "__main__":
    raise SystemExit(main())
