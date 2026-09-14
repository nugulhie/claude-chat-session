# peers 방(room) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 세션을 방 단위로 묶어 `list_peers`·`ask_peer`의 범위를 같은 방으로 제한하고, 에이전트가 방을 만들고 옮겨 다닐 수 있게 한다.

**Architecture:** 방은 브로커 메모리의 문자열 키 하나다(`rooms: dict[str, Room]`). presence(`Session`)에 `room` 필드를 더해 조회·라우팅 범위를 좁히고, `messages`에 `room` 컬럼을 더해 보낸 시점의 방을 영구 기록한다. 메시지는 여전히 `to_sid`로 배달되므로 방 이동이 배달 경로에 영향을 주지 않는다.

**Tech Stack:** Python 3.11+, aiohttp(브로커), MCP Python SDK 2.x + websockets + httpx(채널 서버), sqlite3, 자체 E2E 하네스(원시 JSON-RPC)

**Spec:** `docs/superpowers/specs/2026-09-14-peers-rooms-design.md`

## Global Constraints

- 파이썬 3.11 이상. 브로커 의존성은 `aiohttp` 하나만 유지한다
- 방 이름 검증: `^[\w.-]{1,64}$`. `public`은 예약어
- 브로커 생성 이름: `r-` + 6자, 알파벳 `23456789abcdefghjkmnpqrstuvwxyz`
- 기본 방은 `public`. `PEERS_ROOM` 없이 뜬 세션은 기존과 동작이 같아야 한다
- `messages.room`은 보낸 시점 값으로 고정하고 이후 수정하지 않는다
- `ROOM_RESERVE_SEC` 기본 1800, `MAX_RESERVED_PER_USER` 기본 5
- 방은 접근 통제가 아니다. 도구 설명과 instructions에 이를 명시한다
- 모든 테스트는 `broker/tests/e2e.py` 한 파일에 추가한다. 새 테스트 파일을 만들지 않는다
- 테스트 실행은 항상 `cd broker && .venv/bin/python tests/e2e.py`

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `broker/server.py` | 방 레지스트리, presence에 방 추가, 범위 제한, 신규 API 3개 | 수정 |
| `marketplace/plugins/peers/server.py` | 방 관련 도구 3개, 방 헤더 전송, 환경변수 | 수정 |
| `marketplace/plugins/peers/.claude-plugin/plugin.json` | `default_room` userConfig, 방 환경변수 통과 | 수정 |
| `broker/tests/e2e.py` | 시나리오 10개 추가 | 수정 |
| `USAGE.md` / `ARCHITECTURE.md` / `INTERNALS.md` | 방 사용법·설계·동작 반영 | 수정 |

기존 파일이 크지 않고(브로커 565줄) 방 로직이 presence·라우팅과 밀착돼 있어 파일을 쪼개지 않는다. 방 레지스트리는 `server.py`의 Presence 절 바로 아래에 둔다.

---

## Task 1: 방 레지스트리와 세션의 방 소속

브로커가 방을 알게 하고, 세션이 방에 속하게 한다. 아직 아무 동작도 바뀌지 않는다 — 전원이 `public`에 들어가므로 기존 테스트 16개가 그대로 통과해야 한다.

**Files:**
- Modify: `broker/server.py` (설정 상수, `Session`, 신규 `rooms` 레지스트리, `stream` 핸들러)
- Test: `broker/tests/e2e.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `DEFAULT_ROOM: str = "public"`
  - `ROOM_RE: re.Pattern` — `^[\w.-]{1,64}$`
  - `Session.room: str` 필드
  - `rooms: dict[str, Room]`, `Room` 데이터클래스 (`subject: str | None`, `reserved_until: int | None`)
  - `room_peers(room: str) -> int` — 그 방의 세션 수
  - `touch_room(room: str, subject: str | None) -> None` — 없으면 등록하고 subject는 최초 1회만 설정
  - `drop_empty_rooms() -> None` — 사람 없고 예약도 만료된 방 제거

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`broker/tests/e2e.py`의 `main()` 안, 기존 시나리오 2(presence) 바로 뒤에 넣는다.

```python
        # 2b. 기본 방은 public 이고 list_peers 에 방이 보인다
        peers_now = alice.call("list_peers")["data"]["peers"]
        assert all(p.get("room") == "public" for p in peers_now), \
            f"방 미지정 세션은 public 이어야 함: {peers_now}"
        ok("방 미지정 세션은 public 에 들어간다")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: FAIL — `AssertionError: 방 미지정 세션은 public 이어야 함` (응답에 `room` 키가 없어 `None`)

- [ ] **Step 3: 최소 구현**

`broker/server.py`의 설정 절(45행 `WORKSPACE_RE` 옆)에 추가:

```python
DEFAULT_ROOM = "public"
ROOM_RE = re.compile(r"^[\w.-]{1,64}$")
ROOM_RESERVE_MS = int(float(os.environ.get("ROOM_RESERVE_SEC", 1800)) * 1000)
MAX_RESERVED_PER_USER = int(os.environ.get("MAX_RESERVED_PER_USER", 5))
MAX_SUBJECT = 200
```

Presence 절(`sessions` 선언 아래)에 추가:

```python
@dataclass
class Room:
    subject: str | None = None
    reserved_until: int | None = None


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
```

`Session` 데이터클래스에 필드 추가 (158행 부근, `summary` 앞에 둔다 — 기본값 있는 필드는 뒤로 모아야 하므로 `room`도 기본값을 준다):

```python
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
```

`stream()` 핸들러에서 헤더를 읽어 세션에 넣는다. `workspace` 결정 직후(442행 부근):

```python
    raw_room = request.headers.get("x-peers-room", "")
    room = raw_room if ROOM_RE.match(raw_room) else DEFAULT_ROOM
    raw_subject = (request.headers.get("x-peers-room-subject") or "")[:MAX_SUBJECT]
```

`Session(...)` 생성에 `room=room`을 추가하고, `sessions[sid] = s` 바로 뒤에 등록과 로그를 넣는다:

```python
    touch_room(room, raw_subject or None)
    log(f"connect {address_of(s)} sid={sid[:8]} listening={s.listening} room={room}")
```

(기존 `log(f"connect ...")` 줄을 위 줄로 교체한다.)

연결이 끊기는 `finally` 블록에서 `del sessions[sid]` 뒤에 `drop_empty_rooms()`를 호출한다.

`list_peers()`가 방을 노출하도록 반환 dict에 필드를 더한다(259행 함수). `cur` 초기값에 `"room": s.room, "subject": rooms.get(s.room, Room()).subject` 를 추가한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: PASS — 17개 통과 (기존 16 + 신규 1). 기존 시나리오가 하나도 깨지지 않아야 한다.

- [ ] **Step 5: 커밋**

```bash
git add broker/server.py broker/tests/e2e.py
git commit -m "방 레지스트리와 세션 소속 추가 — 전원 public, 동작 변화 없음"
```

---

## Task 2: 조회와 질문을 같은 방으로 제한

`list_peers`와 `ask_peer`가 방을 넘지 않게 한다. 여기서 처음으로 동작이 바뀐다.

**Files:**
- Modify: `broker/server.py` (`list_peers`, `ask`)
- Test: `broker/tests/e2e.py`

**Interfaces:**
- Consumes: Task 1의 `Session.room`, `rooms`, `Room`
- Produces: `list_peers(me)`가 `me.room`과 같은 방만 반환, `ask()`가 같은 방으로 후보 제한

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`e2e.py`의 `peer()` 헬퍼가 방을 받도록 시그니처를 바꾼다. 먼저 `Peer.__init__`에 인자를 추가:

```python
    def __init__(self, user: str, workspace: str, listen: bool, token: str,
                 room: str | None = None, subject: str | None = None):
```

`subprocess.Popen`의 `env` dict에 두 줄을 더한다:

```python
                **({"PEERS_ROOM": room} if room else {}),
                **({"PEERS_ROOM_SUBJECT": subject} if subject else {}),
```

`main()` 안의 지역 함수 `peer()`도 함께 바꾼다:

```python
        def peer(u: str, ws: str, listen: bool, room: str | None = None,
                 subject: str | None = None) -> Peer:
            p = Peer(u, ws, listen, tokens[u], room, subject)
            wait_for(lambda: not p.call("list_peers")["error"], f"{u} connected")
            return p
```

그리고 시나리오 13 뒤에 추가:

```python
        # 14. 방이 다르면 서로 보이지 않고 질문도 못 한다
        erin = peer("alice", "room-a", True, room="ROOM-A")
        frank = peer("bob", "room-b", True, room="ROOM-B")
        seen = [p["address"] for p in erin.call("list_peers")["data"]["peers"]]
        assert "bob@room-b" not in seen, f"다른 방 세션이 보임: {seen}"
        blocked = erin.call("ask_peer", {"to": "bob@room-b", "question": "다른 방"})
        assert "404" in blocked["error"], blocked["error"]
        ok("방이 다르면 list_peers 에 안 보이고 질문도 404")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: FAIL — `AssertionError: 다른 방 세션이 보임: [... 'bob@room-b' ...]`

- [ ] **Step 3: 최소 구현**

`list_peers()`의 순회에 방 조건을 건다:

```python
def list_peers(me: Session) -> list[dict]:
    by_addr: dict[str, dict] = {}
    for s in sessions.values():
        if s.room != me.room:
            continue
        ...
```

`ask()`의 후보 필터에 방 조건을 더한다(284행 함수):

```python
    candidates = [
        s
        for s in sessions.values()
        if s.listening
        and s.sid != me.sid
        and s.room == me.room
        and (address_of(s) == to if "@" in to else s.user == to)
    ]
```

`ask()`의 404 메시지도 방을 밝히도록 바꾼다:

```python
        raise HttpError(404, f"{to}: 지금 이 방({me.room})에서 질문을 받을 수 있는 세션이 없습니다",
                        available=available)
```

`available` 목록은 `list_peers(me)`를 그대로 쓰므로 이미 같은 방 기준이다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: PASS — 18개 통과

- [ ] **Step 5: 커밋**

```bash
git add broker/server.py broker/tests/e2e.py
git commit -m "list_peers 와 ask_peer 를 같은 방으로 제한"
```

---

## Task 3: 방 생성·이동·조회 API

브로커에 엔드포인트 3개를 더한다. 채널 서버는 아직 호출하지 않는다.

**Files:**
- Modify: `broker/server.py` (신규 핸들러 3개, `make_app` 라우트, `healthz`)
- Test: `broker/tests/e2e.py`

**Interfaces:**
- Consumes: Task 1의 `rooms`, `touch_room`, `room_peers`, `drop_empty_rooms`, `ROOM_RE`, `DEFAULT_ROOM`
- Produces:
  - `POST /api/rooms` → `{"room","subject","reserved_for_sec"}`
  - `POST /api/rooms/join` → `{"room","subject","peers"}`
  - `GET /api/rooms` → `{"rooms":[{"room","subject","peers","you"}]}`
  - `gen_room_name() -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`e2e.py` 시나리오 14 뒤에 추가. HTTP를 직접 호출하는 헬퍼가 필요하므로 `Peer`에 메서드를 더한다:

```python
    def api(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            BROKER_URL + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"authorization": f"Bearer {self.token}",
                     "x-peers-session": self.sid,
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
```

이를 위해 `Peer.__init__`에 `self.token = token` 과 `self.sid` 를 저장해야 한다. `sid`는 채널 서버가 만들므로 브로커에서 받아와야 하는데, 테스트에서는 직접 알 수 없다. **대신 도구를 통해 검증한다** — 아래 테스트는 Task 4에서 도구가 생긴 뒤 완성되므로, 여기서는 브로커 단독 검증만 한다:

```python
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
```

`erin.sid`가 필요하므로 `Peer`가 자기 sid를 알아야 한다. 채널 서버가 stderr로 찍지 않으므로, **`Peer.__init__`에서 sid를 직접 만들어 환경변수로 넘긴다.** 채널 서버에 `PEERS_SID` 지원을 추가한다(아래 Step 3에서 함께).

```python
        self.sid = str(uuid.uuid4())
        self.token = token
```
그리고 Popen env에 `"PEERS_SID": self.sid` 를 추가한다. 파일 상단에 `import uuid` 를 더한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: FAIL — `/api/rooms` 가 404 (라우트 없음)

- [ ] **Step 3: 최소 구현**

채널 서버가 sid를 환경변수로 받게 한다. `marketplace/plugins/peers/server.py`의 `SID` 정의를 바꾼다:

```python
SID = env("PEERS_SID") or str(uuid.uuid4())
```

브로커에 이름 생성기와 핸들러 3개를 추가한다. `inbox()` 함수 뒤에 둔다:

```python
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
```

`Room` 데이터클래스에 `created_by` 필드를 더한다(Task 1에서 만든 것을 수정):

```python
@dataclass
class Room:
    subject: str | None = None
    reserved_until: int | None = None
    created_by: str | None = None
```

`make_app()`의 라우트에 세 줄을 더한다:

```python
            web.post("/api/rooms", guarded(create_room, needs_body=True)),
            web.post("/api/rooms/join", guarded(join_room, needs_body=True)),
            web.get("/api/rooms", guarded(list_rooms, needs_body=False)),
```

`healthz()`에 방 개수를 더한다:

```python
    return json_response(200, {"ok": True, "sessions": len(sessions), "rooms": len(rooms)})
```

`sweeper()` 루프 끝(`db.commit()` 뒤)에 `drop_empty_rooms()` 를 호출해 만료된 예약을 정리한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: PASS — 19개 통과

- [ ] **Step 5: 커밋**

```bash
git add broker/server.py broker/tests/e2e.py marketplace/plugins/peers/server.py
git commit -m "방 생성/이동/조회 API 추가"
```

---

## Task 4: 채널 서버 도구 3개와 방 헤더

에이전트가 방을 쓸 수 있게 한다.

**Files:**
- Modify: `marketplace/plugins/peers/server.py` (환경변수, 헤더, 도구 3개, instructions)
- Modify: `marketplace/plugins/peers/.claude-plugin/plugin.json`
- Test: `broker/tests/e2e.py`

**Interfaces:**
- Consumes: Task 3의 세 엔드포인트
- Produces: 도구 `create_room`, `join_room`, `list_rooms` (총 8개)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: FAIL — `AssertionError` (도구 목록이 5개)

- [ ] **Step 3: 최소 구현**

`marketplace/plugins/peers/server.py` 상단 환경변수(48행 부근)에 추가:

```python
ROOM = env("PEERS_ROOM") or env("PEERS_DEFAULT_ROOM") or "public"
ROOM_SUBJECT = (env("PEERS_ROOM_SUBJECT") or "")[:200]
```

WebSocket 헤더(245행 부근 `headers` dict)에 두 줄을 더한다:

```python
        "x-peers-room": ROOM,
        "x-peers-room-subject": ROOM_SUBJECT,
```

REST 호출 함수 3개를 `_set_status` 뒤에 추가:

```python
async def _create_room(a: dict) -> str:
    return await call_broker("POST", "/api/rooms",
                             {"subject": a.get("subject"), "name": a.get("name")})


async def _join_room(a: dict) -> str:
    return await call_broker("POST", "/api/rooms/join",
                             {"room": a.get("room"), "subject": a.get("subject")})


async def _list_rooms(a: dict) -> str:
    return await call_broker("GET", "/api/rooms")
```

`TOOLS` 리스트에 세 항목을 더한다(`_set_status` 항목 뒤):

```python
    (
        types.Tool(
            name="create_room",
            description=(
                "새 방을 만들고 이름을 돌려준다. 만들기만 하고 입장하지는 않으므로, "
                "들어가려면 join_room 을 부르거나 PEERS_ROOM 으로 세션을 다시 띄운다. "
                "방은 대화를 묶는 수단이지 접근 통제가 아니다 — 이름을 아는 사람은 누구나 "
                "들어올 수 있고 방 이름과 주제는 전원에게 보인다."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "방 설명 한 줄. 최대 200자"},
                    "name": {"type": "string", "description": "원하는 방 이름. 생략하면 브로커가 생성"},
                },
            },
        ),
        _create_room,
    ),
    (
        types.Tool(
            name="join_room",
            description=(
                "이 세션을 다른 방으로 옮긴다. 없는 방이면 새로 생긴다. "
                "public 으로 부르면 기본 공개 방으로 돌아온다. "
                "옮겨도 이미 받은 질문에는 계속 reply 할 수 있다."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "room": {"type": "string", "description": "옮겨갈 방 이름"},
                    "subject": {"type": "string", "description": "방이 새로 생길 때만 쓰이는 설명"},
                },
                "required": ["room"],
            },
        ),
        _join_room,
    ),
    (
        types.Tool(
            name="list_rooms",
            description="지금 열려 있는 방 목록. 이름, 주제, 인원을 보여준다. 이 도구만 방 경계를 넘는다.",
            inputSchema={"type": "object", "properties": {}},
        ),
        _list_rooms,
    ),
```

`INSTRUCTIONS` 문자열의 workspace 문장 뒤에 한 줄을 더한다:

```
이 세션이 속한 방은 "{ROOM}"이다. 같은 방 세션만 list_peers 에 보이고 질문할 수 있다. 방은 대화를 묶는 수단이지 접근 통제가 아니다 — 이름을 아는 사람은 누구나 들어올 수 있고 방 이름과 주제는 전원에게 보인다. 방을 근거로 민감한 내용을 공유하지 않는다.
```

f-string이므로 `{ROOM}`이 그대로 치환된다.

`plugin.json`의 `userConfig`에 `default_room`을 더한다:

```json
    "default_room": {
      "type": "string",
      "title": "기본 방",
      "description": "세션이 기본으로 들어갈 방. PEERS_ROOM 환경변수가 있으면 그것이 우선합니다. 비우면 public.",
      "required": false
    }
```

`mcpServers.peers.env`에 세 줄을 더한다:

```json
        "PEERS_DEFAULT_ROOM": "${user_config.default_room}",
        "PEERS_ROOM": "${PEERS_ROOM:-}",
        "PEERS_ROOM_SUBJECT": "${PEERS_ROOM_SUBJECT:-}"
```

- [ ] **Step 4: 통과를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: PASS — 20개 통과

- [ ] **Step 5: 커밋**

```bash
git add marketplace/plugins/peers/server.py marketplace/plugins/peers/.claude-plugin/plugin.json broker/tests/e2e.py
git commit -m "채널 서버에 방 도구 3개 추가"
```

---

## Task 5: messages.room 기록과 이동 후 reply

대화를 방에 영구히 묶고, 방을 옮겨도 답할 수 있음을 보장한다.

**Files:**
- Modify: `broker/server.py` (스키마, `COLS`, `create_message`, `ask`, `reply`)
- Test: `broker/tests/e2e.py`

**Interfaces:**
- Consumes: Task 1~4 전부
- Produces: `messages.room` 컬럼 (보낸 시점 고정)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: PASS (이미 통과한다 — 메시지가 `to_sid`로 배달되므로). 통과하면 이 테스트는 **회귀 방지용**이다. 만약 FAIL이면 설계 가정이 틀린 것이므로 멈추고 보고한다.

- [ ] **Step 3: room 컬럼을 더한다**

`db.executescript` 의 `CREATE TABLE` 에 컬럼을 더하고(새 DB용), 기존 DB용 마이그레이션을 그 아래에 넣는다:

```python
# 기존 DB 에는 컬럼이 없으므로 없을 때만 더한다
_cols = {r["name"] for r in db.execute("PRAGMA table_info(messages)")}
if "room" not in _cols:
    db.execute(f"ALTER TABLE messages ADD COLUMN room TEXT NOT NULL DEFAULT '{DEFAULT_ROOM}'")
db.execute("CREATE INDEX IF NOT EXISTS idx_room ON messages(room, created_at)")
db.commit()
```

`CREATE TABLE` 문 안에도 `room TEXT NOT NULL DEFAULT 'public',` 를 `status` 앞에 넣는다.

`COLS` 에 `room` 을 더한다:

```python
COLS = (
    "id, kind, from_user, from_ws, from_sid, to_user, to_ws, to_sid, "
    "reply_to, hops, body, context, room, status, created_at, updated_at"
)
```

`create_message` 의 기본값 dict 에 `"room": DEFAULT_ROOM,` 을 더하고, `INSERT` 의 placeholder 개수를 15 → 16 으로 바꾼다:

```python
        f"INSERT INTO messages ({COLS}) VALUES ({', '.join('?' * 16)})",
```

`ask()` 의 `create_message(...)` 호출에 `room=me.room` 을 더한다.

`reply()` 의 `create_message(...)` 호출에 `room=question["room"]` 을 더한다 — 답변은 **질문이 있던 방**에 묶인다. 답하는 사람이 방을 옮겼어도 대화는 원래 방에 남아야 한다.

`sweeper()` 의 만료 notice `create_message(...)` 에도 `room=question["room"]` 을 더한다.

- [ ] **Step 4: 방 기록을 검증하는 테스트를 더한다**

```python
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
```

Run: `cd broker && .venv/bin/python tests/e2e.py`
Expected: PASS — 22개 통과

- [ ] **Step 5: 커밋**

```bash
git add broker/server.py broker/tests/e2e.py
git commit -m "messages.room 기록 — 대화를 보낸 시점 방에 고정"
```

---

## Task 6: 문서 갱신

**Files:**
- Modify: `USAGE.md`, `ARCHITECTURE.md`, `INTERNALS.md`, `README.md`

**Interfaces:**
- Consumes: Task 1~5의 최종 동작
- Produces: 없음 (문서)

- [ ] **Step 1: USAGE.md 에 방 절을 더한다**

"2. 질문하기" 앞에 새 절을 넣는다. 담을 내용:
- 방을 지정해 세션 띄우기 (`PEERS_ROOM`, `PEERS_ROOM_SUBJECT` 예시)
- 방 안 고르면 `public` — 지금까지 쓰던 대로
- 방 만들기: Claude에게 "방 파자"라고 하면 `create_room`이 이름을 준다. 그 이름을 상대에게 전달
- `list_rooms`로 열려 있는 방 찾기
- 1:1은 둘만 아는 이름을 쓰면 된다
- **방은 격리가 아니다**: 이름을 알면 누구나 들어오고 목록에 이름·주제가 전원에게 보인다

- [ ] **Step 2: 도구 목록 표를 8개로 갱신한다**

USAGE.md 맨 아래 표에 `create_room`, `join_room`, `list_rooms` 세 줄을 더한다.

- [ ] **Step 3: ARCHITECTURE.md 와 INTERNALS.md 를 갱신한다**

- ARCHITECTURE: "정책을 브로커에 둔 이유" 뒤에 방이 조회·질문 범위를 정한다는 문단 추가. 방이 접근 통제가 아니라는 점 명시
- INTERNALS: "상태가 사는 곳" 표에 `rooms` 한 줄 추가(메모리, 재시작 시 세션 재접속으로 복원). "질문 한 건의 여정" 시퀀스의 대상 찾기 주석에 방 조건 추가. 데이터 모델 표에 `room` 컬럼 추가

- [ ] **Step 4: 브로커 정책 요약 표에 방을 더한다**

README.md의 "브로커 정책 요약" 표에 두 줄 추가:

| 다른 방 세션에 질문 | 404, 같은 방의 질문 가능한 대상 목록 반환 |
| 방을 옮긴 뒤 이전 질문에 답 | 허용. 답변은 질문이 있던 방에 묶인다 |

- [ ] **Step 5: 링크 검증과 커밋**

```bash
python3 - <<'PY'
import re, os
files = ['README.md','USAGE.md','ARCHITECTURE.md','OPERATIONS.md','INTERNALS.md','MARKETPLACE.md']
def anchors(p):
    return {re.sub(r'[^\w\s\-가-힣]','',m.group(2).strip().lower()).replace(' ','-')
            for line in open(p) if (m := re.match(r'^(#{1,6})\s+(.*)$', line))}
am = {f: anchors(f) for f in files}
bad = 0
for f in files:
    for m in re.finditer(r'\[([^\]]+)\]\(([^)]+)\)', open(f).read()):
        t = m.group(2)
        if t.startswith('http'): continue
        p, _, frag = t.partition('#'); p = p or f
        if not os.path.exists(p): print(f'✗ {f}: 파일 없음 {p}'); bad += 1
        elif frag and frag not in am.get(p, set()): print(f'✗ {f}: 앵커 없음 {t}'); bad += 1
print('링크 오류:', bad)
PY
git add -A && git commit -m "문서에 방 반영"
```

---

## Task 7: 실제 Claude Code 두 세션으로 통합 검증

E2E는 프로토콜을 검증하지만 Claude의 판단은 검증하지 않는다. 브로커 로그가 최종 판정 기준이다.

> **먼저 사용자에게 확인한다.** 이 태스크는 플러그인 설정을 바꾸고 브로커를 띄운다.
> 사용자가 별도로 시험 중인 브로커나 플러그인 설정이 살아 있으면 **실행하지 않는다.**
> `pgrep -fl "server.py"` 와 `claude plugin list` 로 확인하고, 돌고 있는 것이 있으면
> 멈추고 물어본다. 절대 `pkill -f server.py` 같은 패턴 종료를 쓰지 않는다 —
> 남의 프로세스를 죽인다.

**Files:** 없음 (검증만)

**Interfaces:**
- Consumes: Task 1~6 전부
- Produces: 없음

- [ ] **Step 1: 기존 환경을 기록하고 충돌이 없는지 본다**

```bash
pgrep -fl "server.py" || echo "(돌고 있는 브로커 없음)"
claude plugin list 2>&1 | grep -A3 peers || echo "(설치된 peers 없음)"
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/settings.json'))); print(d.get('pluginConfigs'))"
```

돌고 있는 브로커나 설치된 `peers` 플러그인이 있으면 **여기서 멈추고 사용자에게 묻는다.**
없으면 위 출력을 메모해 두고 다음으로 간다.

- [ ] **Step 2: 빈 포트에 브로커를 띄우고 PID 를 저장한다**

```bash
cd broker
for P in 18991 18992 18993; do lsof -i :$P >/dev/null 2>&1 || { PORT=$P; break; }; done
echo "PORT=$PORT"
PEERS_TOKENS=/tmp/rt.json .venv/bin/python issue_token.py alice   # 토큰 보관
PORT=$PORT HOST=127.0.0.1 PEERS_DB=/tmp/rt.db PEERS_TOKENS=/tmp/rt.json \
  .venv/bin/python server.py > /tmp/rt.log 2>&1 &
echo $! > /tmp/rt.pid        # 반드시 PID 를 남긴다. 정리는 이 PID 로만 한다
sleep 2 && curl -s http://127.0.0.1:$PORT/healthz
```

기대: `{"ok": true, "sessions": 0, "rooms": 1}`

- [ ] **Step 3: 로컬 마켓플레이스로 설치한다**

Step 1에서 `peers` 가 이미 설치돼 있었다면 이 단계를 건너뛰고 사용자에게 묻는다.

```bash
claude plugin marketplace add .
claude plugin install peers@claude-peers --config broker_url=http://127.0.0.1:$PORT --config token=<토큰>
claude mcp list | grep peers     # ✔ Connected
```

- [ ] **Step 4: 방을 지정해 응답 세션을 띄운다**

```bash
mkdir -p /tmp/rt/billing-api/src/webhooks
cat > /tmp/rt/billing-api/src/webhooks/retry.ts <<'TS'
export const MAX_ATTEMPTS = 5
export const BASE_DELAY_MS = 60_000
TS
cd /tmp/rt/billing-api
PEERS_ROOM=webhook-dup PEERS_ROOM_SUBJECT="취소 웹훅 중복 수신" PEERS_LISTEN=1 \
  claude --dangerously-load-development-channels plugin:peers@claude-peers \
  --allowedTools "mcp__plugin_peers_peers__check_inbox" "mcp__plugin_peers_peers__reply" "Read" "Grep" \
  -p "질문을 기다렸다가 이 레포 코드를 읽고 reply 로 답해라. 추측하지 마라." --max-turns 200 &
```

- [ ] **Step 5: 같은 방과 다른 방에서 각각 질문한다**

같은 방(`PEERS_ROOM=webhook-dup`)에서 질문하면 답이 와야 한다.
`PEERS_ROOM` 없이(즉 `public`) 띄운 세션에서 같은 대상에게 질문하면 **404** 가 나야 한다.

- [ ] **Step 6: 브로커 로그로 판정한다**

`/tmp/rt.log` 에 다음이 보여야 한다.

```
connect alice@billing-api ... room=webhook-dup
ask <id> ... -> ... hops=0
reply <id> for <id>
```

- [ ] **Step 7: 시작한 것만 정리한다**

```bash
kill "$(cat /tmp/rt.pid)"          # 패턴이 아니라 PID 로만 죽인다
claude plugin uninstall peers@claude-peers
claude plugin marketplace remove claude-peers
rm -f /tmp/rt.pid /tmp/rt.json /tmp/rt.db /tmp/rt.log
```

Step 1에서 기록한 상태로 돌아왔는지 확인한다. 사용자가 쓰던 설정이 있었다면 복원한다.

- [ ] **Step 8: 결과를 기록하고 커밋한다**

통합 검증 결과를 스펙 문서 하단에 `## 검증 기록` 절로 덧붙이고 커밋한다.

```bash
git add docs/ && git commit -m "실제 두 세션 통합 검증 기록"
```

---

## Self-Review

**스펙 커버리지**

| 스펙 항목 | 태스크 |
|---|---|
| 방 = 문자열, 멤버 관리 없음 | 1 |
| 방 이름 규칙·예약어 | 1, 3 |
| subject 첫 등록자만 | 1 (`touch_room`), 4 |
| `list_peers`/`ask_peer` 범위 제한 | 2 |
| `create_room` (예약, 409, 400, 한도) | 3, 4 |
| `join_room` | 3, 4 |
| `list_rooms` (방 경계 넘음) | 3, 4 |
| 방 이동 시 배달 영향 없음 | 5 |
| `messages.room` 고정 | 5 |
| `hops` 방 넘어 누적 | 기존 동작 유지 — 2에서 후보만 제한하고 hops 계산은 건드리지 않는다 |
| 설정 우선순위 | 4 |
| 기존 사용자 호환 | 1(기본 public), 5(ALTER 기본값) |
| 격리 아님 명시 | 4(instructions·도구설명), 6(문서) |
| 테스트 10개 | 1,2,3,4,5 에 분산 |

`check_inbox`/`set_status`/`reply` 무변경은 의도이므로 태스크 없음.

**플레이스홀더**: 없음. 모든 코드 단계에 실제 코드가 들어 있다.

**타입 일관성**: `Room`은 Task 1에서 2필드로 만들고 Task 3에서 `created_by`를 더한다 — Task 3 Step 3에 수정 지시를 명시했다. `Session.room`, `rooms`, `touch_room`, `room_peers`, `drop_empty_rooms`, `gen_room_name` 이름이 태스크 전반에서 일치한다. `PEERS_SID`는 Task 3에서 도입해 Task 3의 테스트가 쓴다.
