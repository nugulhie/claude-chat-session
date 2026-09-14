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
import uuid
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
        self.sid = str(uuid.uuid4())
        self.token = token
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
                "PEERS_SID": self.sid,
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


def stream_status(token: str, sid: str) -> int:
    """/stream 핸드셰이크를 시도하고 HTTP 상태만 본다.

    업그레이드가 받아들여지면 aiohttp 가 101 을 주는데 urllib 는 그걸 성공으로
    읽지 않으므로, 여기서는 거부 코드(400/401)를 구분하는 데만 쓴다.
    """
    req = urllib.request.Request(BROKER_URL + "/stream", headers={
        "authorization": f"Bearer {token}",
        "x-peers-session": sid,
        "connection": "Upgrade",
        "upgrade": "websocket",
        "sec-websocket-version": "13",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
    })
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        # 업그레이드까지 갔다는 뜻 — 거부 코드가 아니다
        return 101


def err_body(msg: str) -> dict:
    return json.loads(msg[msg.index("{"):])


def main() -> int:
    global passed
    tokens = {u: issue_token(u) for u in ("alice", "bob", "carol", "dave")}

    def start_broker() -> subprocess.Popen:
        return subprocess.Popen(
            [PY, "server.py"], cwd=BROKER_DIR, env=BROKER_ENV,
            stdout=subprocess.DEVNULL if not os.environ.get("VERBOSE") else None, stderr=None,
        )

    broker = start_broker()

    def up() -> bool:
        try:
            with urllib.request.urlopen(BROKER_URL + "/healthz", timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    try:
        wait_for(up, "broker up")

        def restart_broker() -> None:
            """브로커를 껐다 켠다. 붙어 있던 채널 서버들은 스스로 재연결한다."""
            nonlocal broker
            broker.terminate()
            try:
                broker.wait(timeout=5)
            except Exception:
                broker.kill()
            broker = start_broker()
            wait_for(up, "broker 재기동", 20)

        def my_row(p: "Peer") -> dict | None:
            """세션이 브로커에 등록돼 있으면 list_peers 안의 자기 자신 항목."""
            r = p.call("list_peers")
            if r["error"]:
                return None
            return next((x for x in r["data"]["peers"] if x["you"]), None)

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
        assert alice.list_tools() == ["ask_peer", "check_inbox", "create_room",
                                      "join_room", "list_peers", "list_rooms",
                                      "reply", "set_status"]
        assert "payments-web" in alice.instructions
        ok("도구 8개와 instructions 노출")

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

        # /stream 은 토큰과 세션 ID 를 따로 판정한다. 둘을 401 로 묶으면 세션 ID 형식
        # 오류가 "토큰이 거부됐다"로 보여서 멀쩡한 토큰을 몇 시간씩 의심하게 된다.
        assert stream_status(tokens["alice"], str(uuid.uuid4())) != 401
        assert stream_status("pk_wrong", str(uuid.uuid4())) == 401
        assert stream_status(tokens["alice"], "not-a-uuid") == 400
        ok("/stream: 토큰 실패 401, 세션 ID 형식 실패 400")

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
        # 스펙: 404의 available 목록도 같은 방 기준이어야 한다.
        # ROOM-A 에는 erin 혼자이고 available 은 본인을 뺀다 → 빈 목록이 맞다.
        avail = err_body(blocked["error"])["available"]
        assert avail == [], f"available 목록이 방 경계를 넘었다: {avail}"
        ok("방이 다르면 list_peers 에 안 보이고 질문도 404 (available 도 방 기준)")

        # 15. 방 API — 생성/조회/중복
        def broker_post(tok, sid, path, body):
            req = urllib.request.Request(BROKER_URL + path, method="POST",
                             data=json.dumps(body).encode(),
                             headers={"authorization": f"Bearer {tok}",
                                      "x-peers-session": sid,
                                      "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        st, made = broker_post(tokens["alice"], erin.sid, "/api/rooms",
                               {"subject": "웹훅 조사"})
        assert st == 200, (st, made)
        assert made["room"].startswith("r-") and len(made["room"]) == 8, made
        st2, dup = broker_post(tokens["alice"], erin.sid, "/api/rooms",
                               {"name": made["room"]})
        assert st2 == 409, (st2, dup)
        st3, res = broker_post(tokens["alice"], erin.sid, "/api/rooms",
                               {"name": "public"})
        assert st3 == 400, (st3, res)
        ok("create_room: 이름 생성, 중복 409, public 400")

        # 16. 도구로 방을 만들고 옮긴다
        assert erin.list_tools() == ["ask_peer", "check_inbox", "create_room",
                                     "join_room", "list_peers", "list_rooms",
                                     "reply", "set_status"]
        made2 = erin.call("create_room", {"subject": "옮겨갈 방"})["data"]
        listed = erin.call("list_rooms")["data"]["rooms"]
        assert any(r["room"] == made2["room"] and r["peers"] == 0 for r in listed), \
            f"예약된 빈 방이 목록에 없음: {listed}"
        moved = erin.call("join_room", {"room": made2["room"]})["data"]
        assert moved["room"] == made2["room"] and moved["peers"] == 1, moved
        frank.call("join_room", {"room": made2["room"]})
        seen2 = [p["address"] for p in erin.call("list_peers")["data"]["peers"]]
        assert "bob@room-b" in seen2, f"같은 방으로 옮겼는데 안 보임: {seen2}"
        ok("create_room / list_rooms / join_room 도구")

        # 17. 방을 옮겨도 이전 방에서 받은 질문에 답할 수 있다
        gina = peer("carol", "room-c", True, room="ROOM-C")
        # dave 는 시나리오 13에서 토큰이 폐기됐으므로 bob 을 쓴다
        asker = peer("bob", "room-c2", False, room="ROOM-C")
        q = asker.call("ask_peer", {"to": "carol@room-c", "question": "옮기기 전 질문"})
        assert q["error"] is None, q["error"]
        ev = wait_for(lambda: next((e for e in gina.events
                                    if e["meta"]["msg_id"] == q["data"]["msg_id"]), None),
                      "carol got question")
        gina.call("join_room", {"room": "ROOM-ELSEWHERE"})
        late = gina.call("reply", {"msg_id": ev["meta"]["msg_id"], "text": "옮긴 뒤 답변"})
        assert late["error"] is None, f"방을 옮긴 뒤 답장이 막힘: {late['error']}"
        ans = wait_for(lambda: next((e for e in asker.events
                                     if e["meta"].get("reply_to") == q["data"]["msg_id"]), None),
                       "bob got answer")
        assert "옮긴 뒤 답변" in ans["content"]
        ok("방을 옮겨도 이전 방에서 받은 질문에 reply 된다")

        # 18. messages.room 은 보낸 시점 값으로 고정된다
        import sqlite3 as _s
        con = _s.connect(BROKER_ENV["PEERS_DB"]); con.row_factory = _s.Row
        row = con.execute("SELECT room FROM messages WHERE id = ?",
                          (q["data"]["msg_id"],)).fetchone()
        assert row["room"] == "ROOM-C", f"질문이 보낸 시점 방에 묶이지 않음: {row['room']}"
        arow = con.execute("SELECT room FROM messages WHERE reply_to = ?",
                           (q["data"]["msg_id"],)).fetchone()
        assert arow["room"] == "ROOM-C", f"답변이 질문의 방에 묶이지 않음: {arow['room']}"
        con.close()
        ok("messages.room 이 보낸 시점 방으로 고정된다")

        # 19. 한글 주제·방 이름·워크스페이스로도 접속이 된다 (회귀 방지)
        #     헤더 값은 ASCII 만 담을 수 있어서, 예전에는 이 조합이 핸드셰이크를 죽이고
        #     세션이 영영 등록되지 않아 모든 도구가 409 를 돌려줬다.
        KOR_SUBJECT = "취소 웹훅 중복 수신 조사"
        han = peer("alice", "결제-웹", True, room="webhook-dup", subject=KOR_SUBJECT)
        han_me = my_row(han)
        assert han_me is not None, "한글 워크스페이스 세션이 브로커에 등록되지 않았다"
        assert han_me["room"] == "webhook-dup", han_me
        assert han_me["workspace"].isascii() and han_me["workspace"] != "unknown", han_me
        listed_kor = [r for r in han.call("list_rooms")["data"]["rooms"] if r["room"] == "webhook-dup"]
        assert listed_kor and listed_kor[0]["subject"] == KOR_SUBJECT, \
            f"한글 주제가 보존되지 않았다: {listed_kor}"
        ok("한글 주제/워크스페이스로 접속되고 주제가 그대로 보존된다")

        # 20. 서로 다른 한글 워크스페이스가 같은 주소로 뭉개지지 않는다
        #     비ASCII 를 `_` 로 치환만 하면 `결제-웹` 과 `인증-웹` 이 둘 다 `__-_` 가 된다.
        kw1 = peer("bob", "결제-웹", True, room="ws-collide")
        kw2 = peer("bob", "인증-웹", True, room="ws-collide")
        kw_addrs = sorted(p["address"] for p in kw1.call("list_peers")["data"]["peers"])
        assert len(set(kw_addrs)) == 2, f"다른 한글 워크스페이스가 같은 주소로 뭉개졌다: {kw_addrs}"
        assert all(a.isascii() for a in kw_addrs), kw_addrs
        kw2.close()
        ok("한글 워크스페이스 두 개가 서로 다른 ASCII 주소를 갖는다")

        # 21. 형식에 어긋나는 방 이름은 스펙대로 public 으로 떨어진다
        kr_room = peer("carol", "kr-room", True, room="취소웹훅")
        kr_me = my_row(kr_room)
        assert kr_me is not None and kr_me["room"] == "public", \
            f"한글 방 이름이 public 으로 떨어지지 않았다: {kr_me}"
        kr_room.close()
        ok("한글 방 이름은 접속을 죽이지 않고 public 으로 떨어진다")

        # 22. join_room 으로 옮긴 방은 재연결 후에도 유지된다
        #     예전에는 핸드셰이크 헤더가 재연결 루프 밖에서 한 번만 만들어져서
        #     브로커가 튕기면 기동 시 방으로 조용히 되돌아갔다.
        mover = peer("alice", "reconnect-ws", True, room="RC-START")
        joined = mover.call("join_room", {"room": "RC-JOINED"})["data"]
        assert joined["room"] == "RC-JOINED", joined
        restart_broker()
        back = wait_for(lambda: my_row(mover), "mover 재연결", 30)
        assert back["room"] == "RC-JOINED", f"재연결하며 join_room 이 취소됐다: {back['room']}"
        mover.close()
        ok("join_room 으로 옮긴 방이 재연결 후에도 유지된다")

        # 22-b. set_status 로 끈 수신도 재연결 후에도 꺼져 있어야 한다.
        #       끈 줄 알고 있는데 다시 받는 쪽이, 아예 못 끄는 것보다 나쁘다.
        quiet = peer("bob", "quiet-ws", True)
        assert quiet.call("set_status", {"listening": False})["data"]["listening"] is False
        restart_broker()
        q_back = wait_for(lambda: my_row(quiet), "quiet 재연결", 30)
        assert q_back["listening"] is False, "재연결하며 수신이 다시 켜졌다"
        # 다시 켜는 것도 같은 경로로 유지돼야 한다
        assert quiet.call("set_status", {"listening": True})["data"]["listening"] is True
        restart_broker()
        assert wait_for(lambda: my_row(quiet), "quiet 재연결 2", 30)["listening"] is True
        quiet.close()
        ok("set_status 로 끈 수신이 재연결 후에도 꺼져 있다")

        # 23. subject 는 첫 등록자만 설정한다 (스펙 시나리오 9)
        subj1 = peer("alice", "subj-1", True, room="SUBJ-ROOM", subject="첫 주제")
        subj2 = peer("bob", "subj-2", True, room="SUBJ-ROOM", subject="나중 주제")
        shown = [r for r in subj2.call("list_rooms")["data"]["rooms"] if r["room"] == "SUBJ-ROOM"]
        assert shown and shown[0]["subject"] == "첫 주제", f"나중 등록자가 주제를 덮어썼다: {shown}"
        rejoined = subj2.call("join_room", {"room": "SUBJ-ROOM", "subject": "또 다른 주제"})["data"]
        assert rejoined["subject"] == "첫 주제", f"join_room 의 subject 가 덮어썼다: {rejoined}"
        subj1.close()
        subj2.close()
        ok("subject 는 첫 등록자만 설정한다")

        # 24. hops 는 방을 넘어서도 누적된다 (스펙 시나리오 10)
        #     질문 TTL 이 3초라 세션은 먼저 다 띄우고 질문만 연달아 보낸다.
        hop_a1 = peer("alice", "hop-1", True, room="HOP-A")
        hop_a2 = peer("bob", "hop-2", True, room="HOP-A")
        hop_b1 = peer("carol", "hop-3", True, room="HOP-B")
        hop_b2 = peer("alice", "hop-4", True, room="HOP-B")
        hq1 = hop_a1.call("ask_peer", {"to": "bob@hop-2", "question": "HOP-A 에서 받은 질문"})
        assert hq1["error"] is None, hq1["error"]
        wait_for(lambda: next((e for e in hop_a2.events
                               if e["meta"]["msg_id"] == hq1["data"]["msg_id"]), None), "hop-2 가 질문 받음")
        hop_a2.call("join_room", {"room": "HOP-B"})
        hq2 = hop_a2.call("ask_peer", {"to": "carol@hop-3", "question": "방을 넘어 다시 묻기"})
        assert hq2["error"] is None, hq2["error"]
        hev = wait_for(lambda: next((e for e in hop_b1.events
                                     if e["meta"]["msg_id"] == hq2["data"]["msg_id"]), None), "hop-3 가 질문 받음")
        assert hev["meta"]["hops"] == "1", f"방을 넘었더니 hops 가 초기화됐다: {hev['meta']}"
        hq3 = hop_b1.call("ask_peer", {"to": "alice@hop-4", "question": "3단계"})
        assert "422" in (hq3["error"] or ""), f"방을 넘어 한도를 우회했다: {hq3}"
        ok("hops 는 방을 넘어서도 누적된다")

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
