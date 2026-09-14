# Claude Peers

서로 다른 머신에서 돌아가는 Claude Code 세션끼리 질문을 **푸쉬**하고 답을 받는 시스템입니다.

내 레포는 내가 잘 알고, 옆 팀 레포는 그쪽이 잘 압니다. 그 간극을 사람을 거치지 않고 메웁니다. "billing-api 쪽 재시도 정책이 어디 정의돼 있어?"라고 물으면, 그 레포에 띄워 둔 동료의 Claude가 코드를 직접 읽고 `파일:라인`까지 붙여 답합니다.

Claude Code의 Channels(리서치 프리뷰)를 사용합니다.

```
 개발자 머신 (N대)                          서버 (1대)
┌──────────────────────────┐
│ Claude Code 세션          │
│   └─ stdio ─┐            │              ┌──────────────────┐
│             ▼            │              │  리버스 프록시     │
│   peers 채널 서버 ────────┼── wss ──────▶│  nginx / ALB     │
│   (uv+python, 세션당 1개) │   outbound   │  :443            │
└──────────────────────────┘              └────────┬─────────┘
                                                   │ http (loopback)
┌──────────────────────────┐                       ▼
│ Claude Code 세션          │              ┌──────────────────┐
│   └─ stdio ─┐            │              │  broker          │
│             ▼            │              │  python, 단일     │
│   peers 채널 서버 ────────┼── wss ──────▶│  :8080           │
└──────────────────────────┘   outbound   └────────┬─────────┘
                                                   │
                                          ┌────────▼─────────┐
                                          │ peers.db (SQLite)│
                                          │ tokens.json      │
                                          └──────────────────┘
```

alice의 Claude가 `ask_peer`를 호출하면 브로커가 bob의 채널 서버로 전달하고, 채널 서버가 `notifications/claude/channel`로 bob의 세션에 밀어 넣습니다. bob의 Claude가 조사한 뒤 `reply`를 호출하면 답이 같은 경로로 돌아옵니다.

**개발자 머신에는 인바운드 포트가 필요 없습니다.** 양쪽 모두 브로커로 나가는 연결만 맺습니다. 노트북이 NAT 뒤에 있어도, VPN을 오가도 상관없습니다.

---

## 목차

1. [무엇이 필요한가](#1-무엇이-필요한가)
2. [인프라 세우기 — 브로커 배포](#2-인프라-세우기--브로커-배포)
3. [배포하기 — 플러그인을 팀에 전달](#3-배포하기--플러그인을-팀에-전달)
4. [설치하기 — 개발자 쪽](#4-설치하기--개발자-쪽)
5. [사용하기](#5-사용하기)
6. [방으로 대화 묶기](#6-방으로-대화-묶기)
7. [로컬에서 먼저 돌려보기](#7-로컬에서-먼저-돌려보기)
8. [브로커 정책 요약](#8-브로커-정책-요약)
9. [알려진 한계와 주의점](#9-알려진-한계와-주의점)

더 깊은 내용은 문서를 따로 두었습니다.

| 문서 | 대상 | 내용 |
|---|---|---|
| [USAGE.md](USAGE.md) | 사용하는 개발자 | 세션 운영, 질문/답변 요령, 안 될 때 진단 |
| [MARKETPLACE.md](MARKETPLACE.md) | 배포하는 사람 | 저장소 레이아웃, 배포, 설치, 갱신 |
| [OPERATIONS.md](OPERATIONS.md) | 브로커 운영자 | 배포, 토큰, 백업, 모니터링, 보관 정책, 장애 대응 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 기여자 | 설계 결정, Claude Code 연동 방식, 개발·검증 방법 |
| [INTERNALS.md](INTERNALS.md) | 기여자 | 인프라 토폴로지, 메시지 흐름, 상태 머신, 실패 경로 |

---

## 1. 무엇이 필요한가

**서버 1대.** 브로커가 돌 곳입니다. 외부 의존성이 `aiohttp` 하나뿐인 단일 파이썬 프로세스이고 데이터베이스는 SQLite 파일 하나입니다. 별도 인프라가 없습니다.

- 파이썬 3.11 이상
- TLS와 WebSocket upgrade를 지원하는 리버스 프록시 (nginx, ALB 등)
- 디스크는 수 GB면 충분합니다

**개발자 머신마다.**

- Claude Code
- [uv](https://docs.astral.sh/uv/) — 채널 서버가 `uv run --script`로 뜨고 의존성을 알아서 해결합니다

**git 저장소 1개.** 플러그인을 배포할 곳입니다. 공개든 비공개든 상관없습니다.

---

## 2. 인프라 세우기 — 브로커 배포

### 2.1 서버에 올리기

```bash
git clone <이 저장소> /opt/claude-peers
sudo /opt/claude-peers/deploy/install.sh
```

`deploy/install.sh` 가 전용 계정·데이터 디렉터리·venv·systemd 유닛까지 만들고 기동 확인까지 합니다. 여러 번 돌려도 안전합니다. 직접 하시려면 아래와 같습니다.

```bash
cd /opt/claude-peers/broker
uv venv && uv pip install -e .
```

### 2.2 토큰 발급

사용자마다 하나씩 발급합니다. 원본은 한 번만 출력되고 서버에는 SHA-256 해시만 남습니다.

```bash
PEERS_TOKENS=/var/lib/claude-peers/tokens.json .venv/bin/python issue_token.py alice
# pk_... 출력 — 이때 받아서 본인에게 전달
```

**사람마다 다른 토큰을 주세요.** 누가 무엇을 물었는지 사용자 단위로 기록되고, 문제가 생기면 그 사람 것만 폐기할 수 있습니다.

```bash
.venv/bin/python issue_token.py --revoke alice   # 재시작 불필요, 몇 초 내 반영
```

운영에서는 `server.py`의 `authenticate()`를 사내 SSO/OIDC 검증으로 교체하는 것을 권합니다. 함수 하나만 바꾸면 되도록 격리해 뒀습니다.

### 2.3 상시 가동

`deploy/claude-peers.service` 를 그대로 쓰면 됩니다(`install.sh` 가 설치합니다).

```ini
# /etc/systemd/system/claude-peers.service
[Unit]
Description=Claude Peers broker
After=network.target

[Service]
Type=simple
User=peers
WorkingDirectory=/opt/claude-peers/broker
ExecStart=/opt/claude-peers/broker/.venv/bin/python server.py
Restart=always
RestartSec=5

Environment=PORT=8080
Environment=HOST=127.0.0.1
Environment=PEERS_DB=/var/lib/claude-peers/peers.db
Environment=PEERS_TOKENS=/var/lib/claude-peers/tokens.json

[Install]
WantedBy=multi-user.target
```

`HOST=127.0.0.1`로 묶고 프록시만 외부에 노출하세요. 브로커 자체는 TLS를 하지 않습니다.

### 2.4 리버스 프록시

`deploy/nginx-claude-peers.conf` 에 도메인과 인증서만 채우면 됩니다.

`https://`로 노출합니다. 채널 서버가 URL의 `http`를 `ws`로 바꿔 `/stream`에 연결하므로 `https` → `wss`가 됩니다. **WebSocket upgrade가 통과해야 합니다.**

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;

    # 세션이 오래 붙어 있다. 브로커가 30초마다 ping 을 보내지만
    # 프록시가 먼저 끊으면 소용없다.
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

ALB를 쓴다면 idle timeout을 기본값(60초)에서 늘리세요.

### 2.5 확인

```bash
curl -s https://peers.example.com/healthz
# {"ok": true, "sessions": 0, "rooms": 1}
```

### 2.6 반드시 알아야 할 두 가지

**단일 인스턴스입니다.** presence와 rate limit이 메모리에 있습니다. 두 대 이상 띄우면 서로 다른 인스턴스에 붙은 세션끼리 보이지 않습니다. 로드밸런서 뒤에 한 대만 두세요.

**백업은 `peers.db`만 복사하면 안 됩니다.** WAL 모드라 최근 데이터가 `peers.db-wal`에 있습니다. 실제로 확인해 보면 돌아가던 브로커의 `peers.db`만 복사한 파일은 테이블조차 없습니다.

```bash
sqlite3 /var/lib/claude-peers/peers.db ".backup '/backup/peers-$(date +%F).db'"
```

환경변수 전체, 모니터링, 보관 정책, 장애 대응은 [OPERATIONS.md](OPERATIONS.md)에 있습니다.

---

## 3. 배포하기 — 플러그인을 팀에 전달

이 저장소를 git에 push하면 그대로 마켓플레이스가 됩니다. 빌드도 업로드도 없습니다.

**매니페스트가 저장소 루트의 `.claude-plugin/marketplace.json`에 있어야 합니다.** 하위 폴더에 두면 git으로 받을 때 실패합니다. 로컬 경로로는 동작하므로, 로컬에서 되던 게 git으로 바꾸는 순간 깨지는 함정이 있습니다.

플러그인을 고칠 때마다 `plugin.json`의 `version`을 올리세요. 버전이 캐시 디렉터리 이름이라 올리지 않으면 새 코드가 내려가지 않습니다.

조직 전체에 미리 깔아 두려면 [admin/managed-settings.json](admin/managed-settings.json)을 참고하세요. `broker_url`을 미리 채워 두면 개발자는 토큰만 입력하면 됩니다. 다만 **managed settings는 조직 전체 정책이라** 저장소와 브로커 주소를 반드시 조직이 통제하는 것으로 바꿔야 합니다 — 모든 개발자 머신이 그 저장소 코드를 자동 실행하고, 모든 토큰이 그 주소로 갑니다.

자세한 내용은 [MARKETPLACE.md](MARKETPLACE.md)에 있습니다.

---

## 4. 설치하기 — 개발자 쪽

**1) 브로커에 닿는지 먼저 확인합니다.** 이게 안 되면 아래는 볼 필요 없습니다.

```bash
curl -s https://peers.example.com/healthz
```

**2) uv를 설치합니다.** 이미 있으면 건너뜁니다 (`uv --version`).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**3) 플러그인을 설치합니다.**

```bash
claude plugin marketplace add <owner>/<repo>
claude plugin install peers@claude-peers \
  --config broker_url=https://peers.example.com \
  --config token=<발급받은 개인 토큰>
```

**4) 붙었는지 확인합니다.**

```bash
claude mcp list | grep peers
# plugin:peers:peers: uv run --script ... - ✔ Connected
```

토큰은 `sensitive`로 선언돼 있어 `settings.json`이 아니라 보안 저장소로 갑니다. 설정 파일을 열어봐도 안 보이는 것이 정상입니다.

---

## 5. 사용하기

세션을 두 종류로 나눠 쓰는 것이 핵심입니다.

**작업 세션** — 평소 코딩하는 세션입니다. 질문을 보내고 답을 받지만 남의 질문은 받지 않습니다.

```bash
claude --channels plugin:peers@claude-peers
```

**응답 전용 세션** — 담당 레포마다 하나씩 백그라운드 터미널에 띄워 둡니다. 남의 질문은 여기로만 들어옵니다.

```bash
cd ~/work/billing-api
PEERS_LISTEN=1 claude --channels plugin:peers@claude-peers \
  --allowedTools "mcp__plugin_peers_peers__reply" \
  --disallowedTools "Bash" "Edit" "Write" "NotebookEdit"
```

나누는 이유는 두 가지입니다. 작업 세션의 컨텍스트가 남의 질문으로 오염되지 않고, 응답 세션을 읽기 전용으로 묶어 둘 수 있습니다. **질문 본문은 결국 다른 사람의 Claude가 쓴 텍스트이므로, 도구 제한이 가장 확실한 방어입니다.**

`reply`를 미리 허용하지 않으면 답할 때마다 권한 프롬프트가 떠서 세션이 멈춥니다. 반대로 `ask_peer`는 자동 허용하지 마세요 — 내 코드 컨텍스트가 밖으로 나가는 순간이라 한 번 보고 승인하는 편이 안전합니다.

질문은 평소처럼 말하면 됩니다.

```
billing-api 쪽 Claude한테 취소 웹훅 재시도 정책이 어디 정의돼 있는지 물어봐
```

**보낸 뒤 기다리지 않습니다.** 답은 나중에 푸쉬로 도착하므로 Claude는 다른 작업을 계속합니다.

> 조직 managed settings에 `allowedChannelPlugins`가 아직 없으면 `--channels` 대신 `--dangerously-load-development-channels`를 씁니다.

질문·답변 작성 요령과 안 될 때 진단 순서는 [USAGE.md](USAGE.md)에 있습니다.

---

## 6. 방으로 대화 묶기

사람이 늘면 전원이 서로 보이는 게 번잡해지고, 한 주제로 오간 대화가 다른 대화와 섞입니다. 방은 이를 묶는 수단입니다.

세션이 뜰 때 방을 정합니다.

```bash
PEERS_ROOM=webhook-dup PEERS_ROOM_SUBJECT="취소 웹훅 중복 수신 조사" \
  PEERS_LISTEN=1 claude --channels plugin:peers@claude-peers
```

같은 방 세션끼리만 `list_peers`에 보이고 질문할 수 있습니다. **방을 지정하지 않으면 `public`이고, 방이 없던 때와 똑같이 동작합니다** — 기존 사용자는 아무것도 바꿀 필요가 없습니다.

Claude에게 방을 만들게 할 수도 있습니다.

```
minji랑 웹훅 건으로 따로 방 파자
→ create_room 이 이름을 돌려줍니다. 그 이름을 상대에게 전달하면 됩니다.
```

`list_rooms`로 열려 있는 방을 보고, `join_room`으로 옮깁니다. 1:1은 둘만 아는 이름을 쓰면 됩니다.

> **방은 대화를 묶는 수단이지 접근 통제가 아닙니다.** 이름을 아는 사람은 누구나 들어올 수 있고, 방 이름과 주제는 `list_rooms`로 전원에게 보입니다. 민감한 내용을 방으로 가릴 수 있다고 생각하면 안 됩니다.

---

## 7. 로컬에서 먼저 돌려보기

```bash
# 브로커
cd broker
uv venv && uv pip install -e ".[test]"
.venv/bin/python issue_token.py alice   # pk_... 보관
.venv/bin/python issue_token.py bob
.venv/bin/python server.py              # :8080

# 자동 테스트 (다른 터미널)
cd broker && .venv/bin/python tests/e2e.py
```

E2E는 브로커와 채널 서버를 실제로 띄우고 가짜 Claude Code로 전 경로를 검증합니다. 28개 시나리오가 몇 초 안에 돕니다.

실제 두 세션으로 시험하려면 이 저장소를 로컬 마켓플레이스로 등록합니다.

```bash
claude plugin marketplace add ./
claude plugin install peers@claude-peers \
  --config broker_url=http://127.0.0.1:8080 --config token=<alice 토큰>

# 터미널 A: 질문하는 세션
cd ~/work/payments-web
claude --dangerously-load-development-channels plugin:peers@claude-peers

# 터미널 B: 질문 받는 세션
cd ~/work/billing-api
PEERS_LISTEN=1 claude --dangerously-load-development-channels plugin:peers@claude-peers
```

로컬 경로로 등록하면 Claude Code가 캐시가 아니라 **원본 `server.py`를 직접 실행**하므로, 고치고 새 세션을 띄우면 바로 반영됩니다.

---

## 8. 브로커 정책 요약

| 상황 | 동작 |
|---|---|
| 수신 OFF 세션에 질문 | 404, 질문 가능한 대상 목록 반환 |
| 다른 방 세션에 질문 | 404, 같은 방의 질문 가능한 대상 목록 반환 |
| `to`가 user만 있고 수신 세션이 여러 레포 | 409, `user@workspace` 후보 반환 |
| 받은 질문을 처리하다 다시 질문 | hops 자동 증가, `MAX_HOPS` 초과 시 422 |
| 나에게 질문한 세션에 되묻기 | 422, `reply`로 확인 요청하라고 안내 |
| TTL(기본 15분) 안에 답 없음 | 질문 만료, 질문자에게 `kind="notice"` 푸쉬, 이후 reply는 410 |
| 방을 옮긴 뒤 이전 질문에 답 | 허용. 답변은 질문이 있던 방에 묶인다 |
| 질문자 세션이 재시작됨 | 같은 `user@workspace`로 재접속하면 쌓인 답변 재전달 |
| 푸쉬를 놓침 | `check_inbox`로 미확인 답변 조회 |
| 질문 폭주 | 10분당 사용자 30건 / 같은 상대 10건 초과 시 429 |

---

## 9. 알려진 한계와 주의점

- **리서치 프리뷰 기능**입니다. `--channels` 플래그와 프로토콜이 바뀔 수 있습니다. 로직은 브로커에 두고 채널 서버는 얇게 유지했습니다.
- **채널로 등록되지 않은 세션**(`--channels` 없이 실행)에서도 MCP 서버는 뜨고 브로커에 접속하지만 푸쉬는 조용히 버려집니다. 그래서 질문 수신은 `PEERS_LISTEN=1`로 명시적으로 켠 세션만 받게 했습니다.
- 푸쉬는 **세션이 열려 있을 때만** 도착합니다. Claude가 작업 중이면 이벤트가 쌓였다가 다음 턴에 처리됩니다.
- **비대화형(`-p`) 모드에서는 답을 기다리다 턴을 소진할 수 있습니다.** 자동화에는 `check_inbox` 폴백이 필요합니다.
- **permission relay(`claude/channel/permission`)는 일부러 선언하지 않았습니다.** 선언하면 채널로 메시지를 보낼 수 있는 사람이 내 세션의 도구 사용을 승인할 수 있게 됩니다.
- 채널 본문은 프롬프트 인젝션 경로가 될 수 있습니다. 서버 instructions와 스킬에 방어 규칙을 넣었지만 가장 확실한 방어는 응답 전용 세션의 도구 제한입니다.
- **방 이름·주제·워크스페이스 이름은 ASCII만 가능합니다.** HTTP 헤더로 전달되기 때문입니다. 한글 방 이름을 주면 `public`으로 떨어지고, 주제는 인코딩되어 값이 보존됩니다.
- **모든 질문과 답변은 브로커 DB에 남습니다.** 누가 누구에게 무엇을 묻고 답했는지 전부 기록되는 감사 로그입니다.
- `MCP_PROTOCOL_NEGOTIATION=auto`를 설정하지 마세요. 새 프로토콜 리비전으로 협상하면 채널로 등록되지 않습니다.

채널이 안 붙을 때의 진단 순서는 [USAGE.md](USAGE.md#6-안-될-때)에 있습니다.

---

## 라이선스

MIT — [LICENSE](LICENSE)
