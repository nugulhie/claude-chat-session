#!/usr/bin/env python3
"""Claude Peers 브로커.

- 토큰 인증, 세션 presence, 질문/답변 라우팅
- SQLite에 모든 메시지 저장 (감사 로그 겸용)
- 질문 만료, 재질문(hops) 제한, rate limit
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from aiohttp import WSMsgType, web

# ─── 설정 ────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8080))
HOST = os.environ.get("HOST", "0.0.0.0")
DB_PATH = os.environ.get("PEERS_DB", "./peers.db")
TOKENS_PATH = Path(os.environ.get("PEERS_TOKENS", "./tokens.json"))
QUESTION_TTL_MS = int(float(os.environ.get("QUESTION_TTL_SEC", 900)) * 1000)  # 답을 기다리는 최대 시간
INBOX_TTL_MS = int(float(os.environ.get("INBOX_TTL_SEC", 86400)) * 1000)  # 못 받은 답변 보관 기간
MAX_HOPS = int(os.environ.get("MAX_HOPS", 1))  # 받은 질문 때문에 다시 묻는 깊이
SWEEP_MS = int(os.environ.get("SWEEP_MS", 15000))
LIMIT_WINDOW_MS = 10 * 60 * 1000
LIMIT_PER_USER = int(os.environ.get("LIMIT_PER_USER", 30))  # 10분당 질문 수
LIMIT_PER_PAIR = int(os.environ.get("LIMIT_PER_PAIR", 10))  # 10분당 같은 상대에게
MAX_QUESTION = 4000
MAX_CONTEXT = 12000
MAX_ANSWER = 16000
MAX_SUMMARY = 300
MAX_BODY = 64 * 1024

SID_RE = re.compile(r"^[0-9a-f-]{36}$")
WORKSPACE_RE = re.compile(r"^[\w.-]{1,64}$", re.ASCII)  # ROOM_RE 와 같은 이유로 ASCII 전용

DEFAULT_ROOM = "public"
# re.ASCII: 파이썬의 \w 는 유니코드 인식이라 한글을 통과시키는데, 방 이름은 HTTP 헤더로
# 도착한다. 헤더에는 ASCII 만 담기므로 유니코드를 허용해 봐야 검증까지 오지 못한다.
ROOM_RE = re.compile(r"^[\w.-]{1,64}$", re.ASCII)
ROOM_RESERVE_MS = int(float(os.environ.get("ROOM_RESERVE_SEC", 1800)) * 1000)
MAX_RESERVED_PER_USER = int(os.environ.get("MAX_RESERVED_PER_USER", 5))
MAX_SUBJECT = 200


def log(*a: Any) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z", *a, flush=True)


def now() -> int:
    return int(time.time() * 1000)


# ─── 인증 (tokens.json: { sha256(token): { user, revoked? } }) ─────────────
# 운영에서는 이 부분을 사내 SSO/OIDC 검증으로 교체하세요.
tokens: dict[str, dict] = {}
_tokens_mtime: float = 0.0


def load_tokens() -> None:
    global tokens
    try:
        tokens = json.loads(TOKENS_PATH.read_text(encoding="utf-8")) if TOKENS_PATH.exists() else {}
    except Exception as e:  # 파일이 반쯤 쓰인 순간에도 죽지 않는다
        log("tokens.json 파싱 실패:", e)


load_tokens()


async def watch_tokens() -> None:
    """tokens.json이 바뀌면 다시 읽는다. 폐기에 재시작이 필요 없다."""
    global _tokens_mtime
    while True:
        try:
            m = TOKENS_PATH.stat().st_mtime if TOKENS_PATH.exists() else 0.0
            if m != _tokens_mtime:
                _tokens_mtime = m
                load_tokens()
        except Exception:
            pass
        await asyncio.sleep(2)


def authenticate(request: web.Request) -> str | None:
    m = re.match(r"^Bearer\s+(\S+)$", request.headers.get("authorization", ""))
    if not m:
        return None
    entry = tokens.get(sha256(m.group(1).encode()).hexdigest())
    if entry and not entry.get("revoked"):
        return entry.get("user")
    return None


# ─── DB ──────────────────────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
  PRAGMA journal_mode = WAL;
  CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,              -- question | answer | notice
    from_user  TEXT, from_ws TEXT, from_sid TEXT,
    to_user    TEXT NOT NULL, to_ws TEXT, to_sid TEXT NOT NULL,
    reply_to   TEXT,
    hops       INTEGER NOT NULL DEFAULT 0,
    body       TEXT NOT NULL,
    context    TEXT,
    room       TEXT NOT NULL DEFAULT 'public',
    status     TEXT NOT NULL,              -- queued | pushed | answered | expired | read | dropped
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_to_sid  ON messages(to_sid, status);
  CREATE INDEX IF NOT EXISTS idx_to_user ON messages(to_user, kind, status);
"""
)
# 기존 DB 에는 컬럼이 없으므로 없을 때만 더한다
_cols = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
if "room" not in _cols:
    db.execute(f"ALTER TABLE messages ADD COLUMN room TEXT NOT NULL DEFAULT '{DEFAULT_ROOM}'")
db.execute("CREATE INDEX IF NOT EXISTS idx_room ON messages(room, created_at)")
db.commit()

COLS = (
    "id, kind, from_user, from_ws, from_sid, to_user, to_ws, to_sid, "
    "reply_to, hops, body, context, room, status, created_at, updated_at"
)


def create_message(**m: Any) -> dict:
    t = now()
    row = {
        "id": str(uuid.uuid4()),
        "hops": 0,
        "reply_to": None,
        "context": None,
        "from_user": None,
        "from_ws": None,
        "from_sid": None,
        "room": DEFAULT_ROOM,
        "status": "queued",
        **m,
        "created_at": t,
        "updated_at": t,
    }
    db.execute(
        f"INSERT INTO messages ({COLS}) VALUES ({', '.join('?' * 16)})",
        [row[c.strip()] for c in COLS.split(",")],
    )
    db.commit()
    return row


def set_status(status: str, msg_id: str) -> None:
    db.execute("UPDATE messages SET status = ?, updated_at = ? WHERE id = ?", (status, now(), msg_id))
    db.commit()


# ─── Presence ────────────────────────────────────────────────────────────
@dataclass
class Session:
    sid: str
    user: str
    workspace: str
    listening: bool
    ws: web.WebSocketResponse
    connected_at: int
    room: str = DEFAULT_ROOM
    summary: str = ""


sessions: dict[str, Session] = {}


@dataclass
class Room:
    subject: str | None = None
    reserved_until: int | None = None
    created_by: str | None = None


rooms: dict[str, Room] = {DEFAULT_ROOM: Room()}


def room_peers(room: str) -> int:
    return sum(1 for s in sessions.values() if s.room == room)


def touch_room(room: str, subject: str | None = None) -> None:
    """방을 등록한다. subject 는 최초 등록자만 설정한다."""
    r = rooms.get(room)
    if r is None:
        rooms[room] = Room(subject=subject or None)
        return
    if r.subject is None and subject:
        r.subject = subject


def drop_empty_rooms() -> None:
    t = now()
    for name in [n for n in rooms if n != DEFAULT_ROOM]:
        r = rooms[name]
        if room_peers(name) == 0 and (r.reserved_until is None or r.reserved_until < t):
            del rooms[name]


def address_of(s: Session) -> str:
    return f"{s.user}@{s.workspace}"


def wire(m: dict | sqlite3.Row) -> dict:
    m = dict(m)
    return {
        "type": "message",
        "id": m["id"],
        "kind": m["kind"],
        "from": f"{m['from_user']}@{m['from_ws']}" if m.get("from_user") else "broker",
        "reply_to": m.get("reply_to"),
        "hops": m.get("hops"),
        "body": m["body"],
        "context": m.get("context"),
    }


def push(m: dict | sqlite3.Row) -> None:
    m = dict(m)
    s = sessions.get(m["to_sid"])
    if s and not s.ws.closed:
        asyncio.create_task(_send(s, wire(m)))


async def _send(s: Session, payload: dict) -> None:
    try:
        await s.ws.send_json(payload)
    except Exception:
        pass  # 끊기는 중이면 조용히 버린다. 메시지는 queued로 남아 재전달된다.


# ─── Rate limit (메모리, 단일 인스턴스 기준) ─────────────────────────────
hits: dict[str, list[int]] = {}


def allow(key: str, limit: int) -> bool:
    t = now()
    arr = [x for x in hits.get(key, []) if t - x < LIMIT_WINDOW_MS]
    ok = len(arr) < limit
    if ok:
        arr.append(t)
    hits[key] = arr
    return ok


# ─── HTTP 유틸 ───────────────────────────────────────────────────────────
class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def _str(v: Any, name: str, mx: int, required: bool = True) -> str:
    if v is None or v == "":
        if required:
            raise HttpError(400, f"{name}이(가) 필요합니다")
        return ""
    if not isinstance(v, str):
        raise HttpError(400, f"{name}은(는) 문자열이어야 합니다")
    if len(v) > mx:
        raise HttpError(413, f"{name}이(가) 너무 깁니다 (최대 {mx}자)")
    return v


async def read_json(request: web.Request) -> dict:
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise HttpError(413, "요청 본문이 너무 큽니다")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise HttpError(400, "JSON 형식 오류")


def my_session(request: web.Request, user: str) -> Session:
    sid = request.headers.get("x-peers-session")
    s = sessions.get(sid) if sid else None
    if not s or s.user != user:
        raise HttpError(409, "브로커에 연결된 세션이 없습니다. 잠시 후 다시 시도하세요.")
    return s


# ─── API 핸들러 ──────────────────────────────────────────────────────────
def list_peers(me: Session) -> list[dict]:
    by_addr: dict[str, dict] = {}
    for s in sessions.values():
        if s.room != me.room:
            continue
        a = address_of(s)
        cur = by_addr.setdefault(
            a,
            {
                "address": a,
                "user": s.user,
                "workspace": s.workspace,
                "room": s.room,
                "subject": rooms.get(s.room, Room()).subject,
                "listening": False,
                "summary": "",
                "sessions": 0,
                "you": False,
            },
        )
        cur["sessions"] += 1
        cur["listening"] = cur["listening"] or s.listening
        if s.summary and (s.listening or not cur["summary"]):
            cur["summary"] = s.summary
        cur["you"] = cur["you"] or s.sid == me.sid
        by_addr[a] = cur
    return sorted(by_addr.values(), key=lambda p: (not p["listening"], p["address"]))


def ask(me: Session, body: dict) -> dict:
    to = _str(body.get("to"), "to", 200).strip()
    question = _str(body.get("question"), "question", MAX_QUESTION)
    context = _str(body.get("context"), "context", MAX_CONTEXT, required=False)

    candidates = [
        s
        for s in sessions.values()
        if s.listening
        and s.sid != me.sid
        and s.room == me.room
        and (address_of(s) == to if "@" in to else s.user == to)
    ]
    if not candidates:
        available = [p["address"] for p in list_peers(me) if p["listening"] and not p["you"]]
        raise HttpError(404, f"{to}: 지금 이 방({me.room})에서 질문을 받을 수 있는 세션이 없습니다",
                        available=available)

    addrs = sorted({address_of(s) for s in candidates})
    if len(addrs) > 1:
        raise HttpError(409, "대상이 여러 곳입니다. user@workspace 형식으로 지정하세요", candidates=addrs)
    target = max(candidates, key=lambda s: s.connected_at)

    # 받은 질문을 처리하다가 다시 묻는 경우 hops가 늘어난다
    row = db.execute(
        "SELECT MAX(hops) AS h FROM messages WHERE kind = 'question' AND to_sid = ? AND status IN ('queued','pushed')",
        (me.sid,),
    ).fetchone()
    hops = 0 if row["h"] is None else row["h"] + 1
    if hops > MAX_HOPS:
        raise HttpError(422, f"재질문 한도({MAX_HOPS}) 초과: 받은 질문에는 알고 있는 범위에서 reply로 답하세요")

    # 나에게 질문한 세션에게 되묻는 핑퐁 방지
    open_from = db.execute(
        "SELECT id FROM messages WHERE kind = 'question' AND to_sid = ? AND from_sid = ? "
        "AND status IN ('queued','pushed') LIMIT 1",
        (me.sid, target.sid),
    ).fetchone()
    if open_from:
        raise HttpError(
            422,
            f"{address_of(target)}이(가) 보낸 질문이 열려 있습니다. "
            "되묻지 말고 reply로 답하거나 확인 요청을 담아 reply 하세요",
        )

    if not allow(f"u:{me.user}", LIMIT_PER_USER) or not allow(f"p:{me.user}>{target.user}", LIMIT_PER_PAIR):
        raise HttpError(429, "질문 빈도 제한에 걸렸습니다. 잠시 후 다시 시도하세요")

    msg = create_message(
        kind="question",
        from_user=me.user,
        from_ws=me.workspace,
        from_sid=me.sid,
        to_user=target.user,
        to_ws=target.workspace,
        to_sid=target.sid,
        hops=hops,
        body=question,
        context=context or None,
        room=me.room,
    )
    push(msg)
    log(f"ask {msg['id']} {address_of(me)} -> {address_of(target)} hops={hops}")
    return {"msg_id": msg["id"], "to": address_of(target), "expires_in_sec": QUESTION_TTL_MS / 1000}


def reply(me: Session, body: dict) -> dict:
    msg_id = _str(body.get("msg_id"), "msg_id", 100)
    text = _str(body.get("text"), "text", MAX_ANSWER)
    question = db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if not question or question["kind"] != "question":
        raise HttpError(404, "해당 질문이 없습니다")
    if question["to_user"] != me.user:
        raise HttpError(403, "나에게 온 질문이 아닙니다")
    if question["status"] == "answered":
        raise HttpError(409, "이미 답한 질문입니다")
    if question["status"] == "expired":
        raise HttpError(410, "만료된 질문입니다. 질문자가 더 이상 기다리지 않습니다")

    answer = create_message(
        kind="answer",
        from_user=me.user,
        from_ws=me.workspace,
        from_sid=me.sid,
        to_user=question["from_user"],
        to_ws=question["from_ws"],
        to_sid=question["from_sid"],
        reply_to=question["id"],
        body=text,
        room=question["room"],
    )
    set_status("answered", question["id"])
    push(answer)
    log(f"reply {answer['id']} for {question['id']}")
    return {"ok": True, "delivered_live": question["from_sid"] in sessions}


def set_session_status(me: Session, body: dict) -> dict:
    if "listening" in body and body["listening"] is not None:
        me.listening = bool(body["listening"])
    if "summary" in body and body["summary"] is not None:
        me.summary = _str(body["summary"], "summary", MAX_SUMMARY, required=False)
    return {"address": address_of(me), "listening": me.listening, "summary": me.summary}


def inbox(me: Session) -> dict:
    rows = db.execute(
        "SELECT * FROM messages WHERE to_user = ? AND kind IN ('answer','notice') "
        "AND status IN ('queued','pushed') ORDER BY created_at",
        (me.user,),
    ).fetchall()
    for r in rows:
        set_status("read", r["id"])
    open_q = db.execute(
        "SELECT * FROM messages WHERE kind = 'question' AND to_user = ? "
        "AND status IN ('queued','pushed') ORDER BY created_at",
        (me.user,),
    ).fetchall()
    return {"messages": [wire(r) for r in rows], "open_questions_to_me": [wire(r) for r in open_q]}


ROOM_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def gen_room_name() -> str:
    import secrets
    return "r-" + "".join(secrets.choice(ROOM_ALPHABET) for _ in range(6))


def create_room(me: Session, body: dict) -> dict:
    subject = _str(body.get("subject"), "subject", MAX_SUBJECT, required=False)
    name = _str(body.get("name"), "name", 64, required=False)

    if name:
        if name == DEFAULT_ROOM:
            raise HttpError(400, f"{DEFAULT_ROOM} 은 예약된 방 이름입니다")
        if not ROOM_RE.match(name):
            raise HttpError(400, "방 이름은 영문/숫자/. _ - 만 쓸 수 있습니다 (최대 64자)")
        if name in rooms:
            raise HttpError(409, f"{name}: 이미 있는 방입니다")
    else:
        for _ in range(20):
            name = gen_room_name()
            if name not in rooms:
                break
        else:
            raise HttpError(500, "방 이름 생성 실패")

    reserved = sum(
        1 for n, r in rooms.items()
        if r.reserved_until and r.reserved_until > now() and room_peers(n) == 0
        and r.created_by == me.user
    )
    if reserved >= MAX_RESERVED_PER_USER:
        raise HttpError(429, f"비어 있는 예약 방이 너무 많습니다 (최대 {MAX_RESERVED_PER_USER})")
    if not allow(f"u:{me.user}", LIMIT_PER_USER):
        raise HttpError(429, "요청 빈도 제한에 걸렸습니다. 잠시 후 다시 시도하세요")

    rooms[name] = Room(subject=subject or None,
                       reserved_until=now() + ROOM_RESERVE_MS,
                       created_by=me.user)
    log(f"room create {name} by {me.user}")
    return {"room": name, "subject": subject or None,
            "reserved_for_sec": ROOM_RESERVE_MS // 1000}


def join_room(me: Session, body: dict) -> dict:
    room = _str(body.get("room"), "room", 64).strip()
    if not ROOM_RE.match(room):
        raise HttpError(400, "방 이름은 영문/숫자/. _ - 만 쓸 수 있습니다 (최대 64자)")
    subject = _str(body.get("subject"), "subject", MAX_SUBJECT, required=False)

    old = me.room
    me.room = room
    touch_room(room, subject or None)
    if old != room:
        drop_empty_rooms()
        log(f"room join {address_of(me)} {old} -> {room}")
    return {"room": room, "subject": rooms[room].subject, "peers": room_peers(room)}


def list_rooms(me: Session) -> dict:
    t = now()
    out = []
    for name, r in rooms.items():
        n = room_peers(name)
        if n == 0 and name != DEFAULT_ROOM and not (r.reserved_until and r.reserved_until > t):
            continue
        out.append({"room": name, "subject": r.subject, "peers": n, "you": name == me.room})
    out.sort(key=lambda x: (not x["you"], -x["peers"], x["room"]))
    return {"rooms": out}


# ─── 라우팅 ──────────────────────────────────────────────────────────────
def json_response(status: int, body: dict) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(body, ensure_ascii=False),
        content_type="application/json",
        charset="utf-8",
    )


def guarded(handler, *, needs_body: bool):
    async def wrapped(request: web.Request) -> web.Response:
        try:
            user = authenticate(request)
            if not user:
                raise HttpError(401, "인증 실패")
            me = my_session(request, user)
            body = await read_json(request) if needs_body else {}
            return json_response(200, handler(me, body) if needs_body else handler(me))
        except HttpError as e:
            return json_response(e.status, {"error": e.message, **e.extra})
        except Exception as e:
            log("internal error", repr(e))
            return json_response(500, {"error": "internal error"})

    return wrapped


async def healthz(request: web.Request) -> web.Response:
    return json_response(200, {"ok": True, "sessions": len(sessions), "rooms": len(rooms)})


# ─── WebSocket: 세션 연결과 푸쉬 ─────────────────────────────────────────
async def stream(request: web.Request) -> web.WebSocketResponse:
    user = authenticate(request)
    if not user:
        return web.Response(status=401, text="Unauthorized")
    # 세션 ID 형식 오류를 401 로 묶으면 멀쩡한 토큰이 거부당한 것처럼 보인다.
    # 원인이 전혀 다르므로 상태 코드와 문구를 나눈다.
    sid = request.headers.get("x-peers-session", "")
    if not SID_RE.match(sid):
        return web.Response(status=400, text="x-peers-session 은 UUID 여야 합니다")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_BODY)
    await ws.prepare(request)

    raw_ws = request.headers.get("x-peers-workspace", "")
    workspace = raw_ws if WORKSPACE_RE.match(raw_ws) else "unknown"

    raw_room = request.headers.get("x-peers-room", "")
    room = raw_room if ROOM_RE.match(raw_room) else DEFAULT_ROOM
    # 주제는 한글이 들어가므로 채널 서버가 percent-encode 해서 보낸다. 자르기 전에 풀어야
    # 이스케이프 한가운데서 잘리지 않는다. 인코딩하지 않는 구버전 클라이언트의 ASCII 값은
    # unquote 를 통과해도 그대로다.
    raw_subject = unquote(request.headers.get("x-peers-room-subject") or "")[:MAX_SUBJECT]

    prev = sessions.get(sid)
    if prev:
        await prev.ws.close(code=4001, message=b"replaced")

    s = Session(
        sid=sid,
        user=user,
        workspace=workspace,
        listening=request.headers.get("x-peers-listen") == "1",
        ws=ws,
        connected_at=now(),
        room=room,
    )
    sessions[sid] = s
    touch_room(room, raw_subject or None)
    log(f"connect {address_of(s)} sid={sid[:8]} listening={s.listening} room={room}")

    # 1) 이 세션으로 보냈지만 ack 못 받은 메시지 재전송
    for m in db.execute(
        "SELECT * FROM messages WHERE to_sid = ? AND status = 'queued' ORDER BY created_at", (sid,)
    ).fetchall():
        push(m)
    # 2) 같은 user@workspace의 끊긴 이전 세션 앞으로 쌓인 답변을 새 세션으로 옮김
    for m in db.execute(
        "SELECT * FROM messages WHERE to_user = ? AND to_ws = ? AND kind IN ('answer','notice') "
        "AND status IN ('queued','pushed') ORDER BY created_at",
        (user, workspace),
    ).fetchall():
        if m["to_sid"] != sid and m["to_sid"] not in sessions:
            db.execute("UPDATE messages SET to_sid = ?, status = 'queued', updated_at = ? WHERE id = ?",
                       (sid, now(), m["id"]))
            db.commit()
            push({**dict(m), "to_sid": sid})

    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except Exception:
                continue
            if m.get("type") == "ack" and isinstance(m.get("id"), str):
                db.execute(
                    "UPDATE messages SET status = 'pushed', updated_at = ? "
                    "WHERE id = ? AND to_sid = ? AND status = 'queued'",
                    (now(), m["id"], sid),
                )
                db.commit()
    finally:
        # 새 연결이 이미 자리를 차지했으면 건드리지 않는다
        if sessions.get(sid) is s:
            del sessions[sid]
            drop_empty_rooms()
        log(f"disconnect {address_of(s)} sid={sid[:8]}")
    return ws


# ─── 만료 처리 ───────────────────────────────────────────────────────────
async def sweeper() -> None:
    while True:
        await asyncio.sleep(SWEEP_MS / 1000)
        try:
            t = now()
            for question in db.execute(
                "SELECT * FROM messages WHERE kind = 'question' AND status IN ('queued','pushed') AND created_at < ?",
                (t - QUESTION_TTL_MS,),
            ).fetchall():
                set_status("expired", question["id"])
                notice = create_message(
                    kind="notice",
                    to_user=question["from_user"],
                    to_ws=question["from_ws"],
                    to_sid=question["from_sid"],
                    reply_to=question["id"],
                    body=(
                        f"{question['to_user']}@{question['to_ws']}에게 보낸 질문이 "
                        f"{round(QUESTION_TTL_MS / 60000)}분 안에 답을 받지 못해 만료되었습니다. "
                        f"질문: {question['body'][:200]}"
                    ),
                    room=question["room"],
                )
                push(notice)
                log(f"expired {question['id']}")
            db.execute(
                "UPDATE messages SET status = 'dropped', updated_at = ? "
                "WHERE kind IN ('answer','notice') AND status IN ('queued','pushed') AND created_at < ?",
                (t, t - INBOX_TTL_MS),
            )
            db.commit()
            drop_empty_rooms()
        except Exception as e:
            log("sweep error", repr(e))


# ─── 기동 ────────────────────────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    app["tasks"] = [asyncio.create_task(sweeper()), asyncio.create_task(watch_tokens())]


async def on_cleanup(app: web.Application) -> None:
    for t in app.get("tasks", []):
        t.cancel()
    db.close()


def make_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/stream", stream),
            web.get("/api/peers", guarded(lambda me: {"peers": list_peers(me)}, needs_body=False)),
            web.post("/api/ask", guarded(ask, needs_body=True)),
            web.post("/api/reply", guarded(reply, needs_body=True)),
            web.post("/api/status", guarded(set_session_status, needs_body=True)),
            web.get("/api/inbox", guarded(inbox, needs_body=False)),
            web.post("/api/rooms", guarded(create_room, needs_body=True)),
            web.post("/api/rooms/join", guarded(join_room, needs_body=True)),
            web.get("/api/rooms", guarded(list_rooms, needs_body=False)),
        ]
    )
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    log(f"claude-peers broker listening on {HOST}:{PORT}")
    web.run_app(make_app(), host=HOST, port=PORT, print=None, access_log=None)
