#!/usr/bin/env python3
"""브로커 + 채널 서버를 띄우고 가짜 Claude Code로 전 경로를 검증하는 E2E 테스트.

실행: (broker 디렉터리에서) python tests/e2e.py

가짜 Claude Code는 MCP 파이썬 클라이언트가 아니라 원시 JSON-RPC로 구현했다.
파이썬 클라이언트 SDK는 모르는 method의 notification을 버리는데,
이 프로젝트의 핵심이 바로 그 커스텀 notification(notifications/claude/channel)이기 때문이다.
실제 Claude Code가 하는 일에 더 가깝기도 하다.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BROKER_DIR = HERE.parent
PLUGIN_SERVER = BROKER_DIR.parent / "marketplace" / "plugins" / "peers" / "server.py"
PY = sys.executable  # PATH의 python이 구버전일 수 있으므로 이 테스트와 같은 인터프리터를 쓴다

tmp = Path(tempfile.mkdtemp(prefix="peers-"))
PORT = 18000 + random.randrange(1000)
BROKER_URL = f"http://127.0.0.1:{PORT}"
BROKER_ENV = {
    **os.environ,
    "PORT": str(PORT),
    "HOST": "127.0.0.1",
    "PEERS_DB": str(tmp / "peers.db"),
    "PEERS_TOKENS": str(tmp / "tokens.json"),
    "QUESTION_TTL_SEC": "3",
    "SWEEP_MS": "500",
    "MAX_HOPS": "1",
    "LIMIT_PER_PAIR": "5",
}

passed = 0
open_peers: list["Peer"] = []


def ok(label: str) -> None:
    global passed
    passed += 1
    print(f"  ✓ {label}")


def wait_for(fn, label: str, timeout: float = 8.0):
    start = time.time()
    while True:
        v = fn()
        if v:
            return v
        if time.time() - start > timeout:
            raise AssertionError(f"timeout: {label}")
        time.sleep(0.1)


def issue_token(user: str) -> str:
    out = subprocess.check_output([PY, "issue_token.py", user], cwd=BROKER_DIR, env=BROKER_ENV)
    return out.decode().strip()


def revoke(user: str) -> None:
    subprocess.run([PY, "issue_token.py", "--revoke", user], cwd=BROKER_DIR, env=BROKER_ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


class Peer:
    """채널 서버 하나를 stdio로 붙잡고 Claude Code 흉내를 내는 가짜 클라이언트."""

    def __init__(self, user: str, workspace: str, listen: bool, token: str,
                 room: str | None = None, subject: str | None = None):
        self.user = user
        self.events: list[dict] = []
        self._next_id = 0
        self._replies: dict[int, dict] = {}
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(
            [PY, str(PLUGIN_SERVER)],
            cwd=str(tmp),
            env={
                **os.environ,
                "PEERS_BROKER_URL": BROKER_URL,
                "PEERS_TOKEN": token,
                "PEERS_LISTEN": "1" if listen else "0",
                "PEERS_WORKSPACE": workspace,
                **({"PEERS_ROOM": room} if room else {}),
                **({"PEERS_ROOM_SUBJECT": subject} if subject else {}),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if not os.environ.get("VERBOSE") else None,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self.instructions = self._initialize()
        open_peers.append(self)

    def _reader(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            with self._lock:
                if m.get("method") == "notifications/claude/channel":
                    self.events.append(m["params"])
                elif "id" in m:
                    self._replies[m["id"]] = m

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
        got = wait_for(lambda: self._replies.get(rid), f"{method} 응답", timeout)
        if "error" in got:
            raise AssertionError(f"{method} 실패: {got['error']}")
        return got["result"]

    def _initialize(self) -> str:
        res = self._request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {},
             "clientInfo": {"name": "fake-claude-code", "version": "0"}},
        )
        assert res["capabilities"].get("experimental", {}).get("claude/channel") is not None, \
            "claude/channel capability가 선언되지 않았다"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res.get("instructions", "")

    def list_tools(self) -> list[str]:
        return sorted(t["name"] for t in self._request("tools/list")["tools"])

    def call(self, name: str, args: dict | None = None) -> dict:
        res = self._request("tools/call", {"name": name, "arguments": args or {}})
        text = res["content"][0]["text"]
        if res.get("isError"):
            return {"error": text, "data": None}
        return {"error": None, "data": json.loads(text)}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
        if self in open_peers:
            open_peers.remove(self)


def http_status(path: str, token: str) -> int:
    req = urllib.request.Request(BROKER_URL + path, headers={"authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def err_body(msg: str) -> dict:
    return json.loads(msg[msg.index("{"):])


def main() -> int:
    global passed
    tokens = {u: issue_token(u) for u in ("alice", "bob", "carol", "dave")}
    broker = subprocess.Popen(
        [PY, "server.py"], cwd=BROKER_DIR, env=BROKER_ENV,
        stdout=subprocess.DEVNULL if not os.environ.get("VERBOSE") else None, stderr=None,
    )

    def up() -> bool:
        try:
            with urllib.request.urlopen(BROKER_URL + "/healthz", timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    try:
        wait_for(up, "broker up")

        def peer(u: str, ws: str, listen: bool, room: str | None = None,
                 subject: str | None = None) -> Peer:
            p = Peer(u, ws, listen, tokens[u], room, subject)
            wait_for(lambda: not p.call("list_peers")["error"], f"{u} connected")
            return p

        alice = peer("alice", "payments-web", False)
        bob = peer("bob", "billing-api", True)
        carol = peer("carol", "auth-api", True)
        dave = peer("dave", "infra", True)

        # 1. 도구와 instructions
        assert alice.list_tools() == ["ask_peer", "check_inbox", "list_peers", "reply", "set_status"]
        assert "payments-web" in alice.instructions
        ok("도구 5개와 instructions 노출")

        # 2. presence
        peers = {p["address"]: p for p in alice.call("list_peers")["data"]["peers"]}
        assert peers["bob@billing-api"]["listening"] is True
        assert peers["alice@payments-web"]["listening"] is False
        assert peers["alice@payments-web"]["you"] is True
        ok("list_peers: 주소/수신 여부/본인 표시")

        bob.call("set_status", {"summary": "청구 배치 리팩터링 중"})
        again = [p for p in alice.call("list_peers")["data"]["peers"] if p["address"] == "bob@billing-api"][0]
        assert again["summary"] == "청구 배치 리팩터링 중"
        ok("set_status: 작업 요약 공유")

        # 2b. 기본 방은 public 이고 list_peers 에 방이 보인다
        peers_now = alice.call("list_peers")["data"]["peers"]
        assert all(p.get("room") == "public" for p in peers_now), \
            f"방 미지정 세션은 public 이어야 함: {peers_now}"
        ok("방 미지정 세션은 public 에 들어간다")

        # 3. 질문 → 푸쉬 → 답변 → 푸쉬
        asked = alice.call("ask_peer", {"to": "bob", "question": "취소 웹훅 재시도 정책 위치?",
                                        "context": "payments-web 중복 수신 버그"})
        assert asked["error"] is None, asked["error"]
        assert asked["data"]["to"] == "bob@billing-api"
        q_ev = wait_for(lambda: next((e for e in bob.events if e["meta"]["kind"] == "question"), None),
                        "bob receives question")
        assert q_ev["meta"]["from"] == "alice@payments-web"
        assert q_ev["meta"]["msg_id"] == asked["data"]["msg_id"]
        assert q_ev["meta"]["hops"] == "0"
        assert "--- context ---" in q_ev["content"]
        assert all(re.fullmatch(r"\w+", k) for k in q_ev["meta"]), "meta 키는 식별자만"
        ok("ask_peer → 상대 세션에 notifications/claude/channel 푸쉬")

        replied = bob.call("reply", {"msg_id": q_ev["meta"]["msg_id"], "text": "src/webhooks/retry.ts:14, 최대 5회"})
        assert replied["data"]["delivered_live"] is True
        a_ev = wait_for(lambda: next((e for e in alice.events if e["meta"]["kind"] == "answer"), None),
                        "alice receives answer")
        assert a_ev["meta"]["reply_to"] == asked["data"]["msg_id"]
        assert a_ev["meta"]["from"] == "bob@billing-api"
        ok("reply → 질문자 세션에 답변 푸쉬")

        dup = bob.call("reply", {"msg_id": q_ev["meta"]["msg_id"], "text": "또 답함"})
        assert "409" in dup["error"]
        not_mine = carol.call("reply", {"msg_id": q_ev["meta"]["msg_id"], "text": "끼어들기"})
        assert "403" in not_mine["error"]
        ok("중복 답변(409), 남의 질문 답변(403) 차단")

        # 4. 수신 꺼진 세션에는 질문 불가
        to_alice = bob.call("ask_peer", {"to": "alice", "question": "?"})
        assert "404" in to_alice["error"]
        available = err_body(to_alice["error"])["available"]
        assert "carol@auth-api" in available
        assert "bob@billing-api" not in available, "available에 본인 제외"
        ok("listening=false 세션으로 질문 차단(404) + available 목록")

        # 5. 재질문 깊이 제한과 핑퐁 차단
        q1 = alice.call("ask_peer", {"to": "bob@billing-api", "question": "Q1"})
        wait_for(lambda: next((e for e in bob.events if e["meta"]["msg_id"] == q1["data"]["msg_id"]), None), "bob got Q1")
        q2 = bob.call("ask_peer", {"to": "carol", "question": "Q1 때문에 묻는 Q2"})
        assert q2["error"] is None
        q2_ev = wait_for(lambda: next((e for e in carol.events if e["meta"]["msg_id"] == q2["data"]["msg_id"]), None),
                         "carol got Q2")
        assert q2_ev["meta"]["hops"] == "1"
        q3 = carol.call("ask_peer", {"to": "dave", "question": "Q2 때문에 묻는 Q3"})
        assert "422" in q3["error"] and "재질문 한도" in q3["error"]
        pingpong = carol.call("ask_peer", {"to": "bob", "question": "되묻기"})
        assert "422" in pingpong["error"]
        ok("hops 자동 계산, 한도 초과(422)와 되묻기(422) 차단")

        # 6. 만료 알림
        n1 = wait_for(
            lambda: next((e for e in alice.events
                          if e["meta"]["kind"] == "notice" and e["meta"].get("reply_to") == q1["data"]["msg_id"]), None),
            "alice expiry notice")
        assert "만료" in n1["content"]
        wait_for(lambda: next((e for e in bob.events
                               if e["meta"]["kind"] == "notice" and e["meta"].get("reply_to") == q2["data"]["msg_id"]),
                              None), "bob expiry notice")
        late = bob.call("reply", {"msg_id": q1["data"]["msg_id"], "text": "늦은 답"})
        assert "410" in late["error"]
        ok("TTL 지난 질문 만료 → 질문자에게 notice, 늦은 답변 410")

        # 7. 오프라인 중 도착한 답변은 같은 workspace의 새 세션으로 재전달
        q4 = alice.call("ask_peer", {"to": "dave", "question": "Q4"})
        wait_for(lambda: next((e for e in dave.events if e["meta"]["msg_id"] == q4["data"]["msg_id"]), None), "dave got Q4")
        alice.close()
        time.sleep(0.5)
        r4 = dave.call("reply", {"msg_id": q4["data"]["msg_id"], "text": "A4"})
        assert r4["data"]["delivered_live"] is False
        alice2 = peer("alice", "payments-web", False)
        wait_for(lambda: next((e for e in alice2.events
                               if e["meta"]["kind"] == "answer" and e["meta"].get("reply_to") == q4["data"]["msg_id"]),
                              None), "alice2 gets A4 on reconnect")
        ok("질문자 세션 재시작 후 답변 재전달")

        # 8. check_inbox 폴백
        inbox1 = alice2.call("check_inbox")
        assert any(m["reply_to"] == q4["data"]["msg_id"] for m in inbox1["data"]["messages"])
        inbox2 = alice2.call("check_inbox")
        assert inbox2["data"]["messages"] == []
        ok("check_inbox: 미확인 메시지 반환 후 read 처리")

        # 9. 인증
        assert http_status("/api/peers", "pk_wrong") == 401
        ok("잘못된 토큰 401")

        # 10. 같은 user가 두 워크스페이스에서 수신 중이면 user만으로는 지정할 수 없다
        bob2 = peer("bob", "ledger-api", True)
        ambiguous = alice2.call("ask_peer", {"to": "bob", "question": "모호한 대상"})
        assert "409" in ambiguous["error"]
        assert sorted(err_body(ambiguous["error"])["candidates"]) == ["bob@billing-api", "bob@ledger-api"]
        bob2.close()
        ok("수신 세션이 여러 워크스페이스면 409 + user@workspace 후보 반환")

        # 11. 크기 제한
        too_long = alice2.call("ask_peer", {"to": "bob@billing-api", "question": "x" * 4001})
        assert "413" in too_long["error"]
        ok("question 4000자 초과 413")

        # 12. 같은 상대에게 반복 질문하면 rate limit (LIMIT_PER_PAIR=5)
        codes = []
        for i in range(7):
            r = dave.call("ask_peer", {"to": "carol@auth-api", "question": f"rate{i}"})
            codes.append("ok" if not r["error"] else r["error"][:11])
        assert codes.count("ok") == 5, f"5건만 통과해야 함: {codes}"
        assert all("429" in c for c in codes[5:]), f"6번째부터 429: {codes}"
        ok("같은 상대 반복 질문 rate limit 429")

        # 13. 폐기된 토큰은 더 이상 통하지 않는다 (tokens.json 자동 리로드)
        revoke("dave")
        wait_for(lambda: "401" in (dave.call("list_peers")["error"] or ""), "dave 토큰 폐기 반영", 12)
        ok("--revoke 후 401 (tokens.json 자동 리로드)")

        # 14. 방이 다르면 서로 보이지 않고 질문도 못 한다
        erin = peer("alice", "room-a", True, room="ROOM-A")
        frank = peer("bob", "room-b", True, room="ROOM-B")
        seen = [p["address"] for p in erin.call("list_peers")["data"]["peers"]]
        assert "bob@room-b" not in seen, f"다른 방 세션이 보임: {seen}"
        blocked = erin.call("ask_peer", {"to": "bob@room-b", "question": "다른 방"})
        assert "404" in blocked["error"], blocked["error"]
        ok("방이 다르면 list_peers 에 안 보이고 질문도 404")

        print(f"\n{passed}개 통과")
        return 0
    except Exception as e:
        print(f"\n실패: {e!r}", file=sys.stderr)
        return 1
    finally:
        # 실패 경로에서도 전부 정리한다. 살아 있는 채널 서버가 남으면 테스트가 멈춘 것처럼 보인다.
        for p in list(open_peers):
            p.close()
        broker.terminate()
        try:
            broker.wait(timeout=3)
        except Exception:
            broker.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
