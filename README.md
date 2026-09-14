# Claude Peers

서로 다른 머신에서 돌아가는 Claude Code 세션끼리 질문을 **푸쉬**하고 답을 받는 시스템입니다. Claude Code의 Channels(리서치 프리뷰) 기능을 사용합니다.

```
                 ┌──────────────────────────────────────┐
                 │  broker                              │
                 │  토큰 인증 · presence · 라우팅          │
                 │  SQLite 저장/감사로그 · 만료 · hop 제한  │
                 └───────┬──────────────────────┬───────┘
              WSS (outbound)                WSS (outbound)
                         │                      │
             peers 채널 서버 (alice PC)    peers 채널 서버 (bob PC)
                  stdio  │                      │  stdio
              Claude Code (payments-web)   Claude Code (billing-api, 수신 ON)
```

alice의 Claude가 `ask_peer`를 호출하면, 브로커가 bob의 채널 서버로 전달합니다. 채널 서버는 이를 `notifications/claude/channel`로 bob의 세션에 밀어 넣습니다. bob의 Claude는 조사한 뒤 `reply`를 호출하고, 답은 같은 경로로 alice의 세션에 푸쉬됩니다.

## 구성

```
claude-peers/
├── broker/                      서버에 배포
│   ├── server.py                HTTP API + WebSocket 브로커
│   ├── issue_token.py           토큰 발급/폐기
│   ├── pyproject.toml           의존성 (aiohttp)
│   └── tests/e2e.py             E2E 테스트 (채널 서버를 가짜 Claude Code로 구동, 16개 시나리오)
├── marketplace/                 git 저장소로 push → 플러그인 마켓플레이스
│   ├── .claude-plugin/marketplace.json
│   └── plugins/peers/
│       ├── .claude-plugin/plugin.json   userConfig, mcpServers, channels 선언
│       ├── server.py                    채널 MCP 서버 (PEP 723 인라인 의존성)
│       └── skills/peer-collab/SKILL.md  협업 프로토콜 스킬
├── admin/managed-settings.json  조직 관리 설정 예시
├── USAGE.md                     개발자용 사용법
├── ARCHITECTURE.md              설계·연동 방식·개발 방법
├── INTERNALS.md                 인프라 구조·동작 구조
└── OPERATIONS.md                브로커 운영
```

역할 분담은 이렇습니다. 브로커는 전달 보장, 정책, 보안을 맡습니다. 채널 서버는 푸쉬와 도구를 제공하는 얇은 어댑터입니다. 스킬은 "언제, 어떻게 묻고 답할지"를 Claude에게 가르칩니다.

## 문서

| 문서 | 대상 | 내용 |
|---|---|---|
| [USAGE.md](USAGE.md) | 사용하는 개발자 | 세션 운영, 질문/답변 요령, 안 될 때 진단 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 기여자 | 설계 결정, Claude Code 연동 방식, 개발·검증 방법 |
| [INTERNALS.md](INTERNALS.md) | 기여자 | 인프라 토폴로지, 메시지 흐름, 상태 머신, 실패 경로 |
| [OPERATIONS.md](OPERATIONS.md) | 브로커 운영자 | 배포, 토큰, 백업, 모니터링, 보관 정책, 장애 대응 |

## 1. 로컬에서 먼저 돌려보기

요구사항: 파이썬 3.11 이상. 채널 서버는 [uv](https://docs.astral.sh/uv/)가 필요합니다.

> **채널 서버는 `uv run --script`로 뜹니다.** 스크립트 첫머리의 PEP 723 인라인 메타데이터에 의존성이 선언돼 있어 uv가 알아서 받아 캐시합니다. 개발자가 venv를 만들 필요가 없고, 플러그인 설치 경로에서 의존성이 빠지는 사고가 구조적으로 생기지 않습니다. 대신 Claude Code를 실행하는 셸의 PATH에 `uv`가 있어야 합니다 — `uv --version`으로 확인하세요.

```bash
# 브로커
cd broker
uv venv && uv pip install -e ".[test]"
.venv/bin/python issue_token.py alice   # 출력된 pk_... 토큰 보관
.venv/bin/python issue_token.py bob
.venv/bin/python server.py              # :8080

# 자동 테스트 (다른 터미널)
cd broker && .venv/bin/python tests/e2e.py
```

실제 Claude Code 두 세션으로 시험하려면 로컬 마켓플레이스로 설치합니다. 커스텀 채널은 조직 허용 목록에 넣기 전까지 개발용 플래그로만 켤 수 있습니다.

```bash
claude plugin marketplace add ./marketplace
claude plugin install peers@acme-internal \
  --config broker_url=http://127.0.0.1:8080 --config token=<alice 토큰>

# 터미널 A: 질문하는 세션
cd ~/work/payments-web
claude --dangerously-load-development-channels plugin:peers@acme-internal

# 터미널 B: 질문 받는 세션 (bob 토큰으로 설치한 환경, 또는 다른 계정/머신)
cd ~/work/billing-api
PEERS_LISTEN=1 claude --dangerously-load-development-channels plugin:peers@acme-internal
```

A 세션에서 "billing-api 쪽 Claude한테 취소 웹훅 재시도 정책이 어디 정의돼 있는지 물어봐"라고 요청하면 흐름을 확인할 수 있습니다.

## 2. 배포와 운영

브로커는 외부 의존성이 `aiohttp` 하나뿐인 단일 파이썬 프로세스이고, 데이터베이스는 SQLite 파일 하나입니다. TLS와 WebSocket upgrade를 지원하는 리버스 프록시 뒤에 두고 `https://`로 노출하세요.

**presence와 rate limit이 메모리에 있으므로 단일 인스턴스 기준입니다.**

배포 절차, 환경변수, 토큰 운영, 백업(WAL 주의), 모니터링, 보관 정책, 장애 대응, 조직 설정 배포는 [OPERATIONS.md](OPERATIONS.md)에 있습니다.

## 3. 개발자 사용법

개발자에게 전달할 문서는 [USAGE.md](USAGE.md)입니다. 질문/답변 작성 요령, 안 될 때 진단 순서, 거부 응답별 대처가 정리돼 있습니다. 아래는 요약입니다.

설치는 `/plugin install peers@acme-internal`로 합니다. 설치할 때 개인 토큰을 입력합니다.

세션은 두 종류로 나눠 쓰는 것을 권장합니다.

**작업 세션 (질문만 함)**: 평소 작업하는 세션입니다. 답이 푸쉬로 들어오도록 채널은 켭니다.

```bash
claude --channels plugin:peers@acme-internal
```

**응답 전용 세션 (질문 받음)**: 레포마다 하나씩 백그라운드 터미널에 띄워 둡니다. 작업 세션의 컨텍스트가 남의 질문으로 오염되지 않고, 읽기 전용으로 제한할 수 있습니다.

```bash
cd ~/work/billing-api
PEERS_LISTEN=1 claude --channels plugin:peers@acme-internal \
  --allowedTools "mcp__plugin_peers_peers__reply" \
  --disallowedTools "Bash" "Edit" "Write" "NotebookEdit"
```

`reply`를 허용해 두지 않으면, 답장할 때마다 권한 프롬프트가 떠서 사람이 승인할 때까지 세션이 멈춥니다. 반대로 `ask_peer`는 자동 허용하지 않는 것을 권장합니다. 내 코드 컨텍스트가 밖으로 나가는 순간이므로 사람이 한 번 보고 승인하는 편이 안전합니다.

셸 alias 예시:

```bash
alias cc='claude --channels plugin:peers@acme-internal'
alias cc-listen='PEERS_LISTEN=1 claude --channels plugin:peers@acme-internal --allowedTools "mcp__plugin_peers_peers__reply" --disallowedTools "Bash" "Edit" "Write" "NotebookEdit"'
```

## 4. 브로커 정책 요약

| 상황 | 동작 |
|---|---|
| 수신 OFF 세션에 질문 | 404, 질문 가능한 대상 목록 반환 |
| `to`가 user만 있고 수신 세션이 여러 레포 | 409, `user@workspace` 후보 반환 |
| 받은 질문을 처리하다 다시 질문 | hops 자동 증가, `MAX_HOPS` 초과 시 422 |
| 나에게 질문한 세션에 되묻기 | 422, `reply`로 확인 요청하라고 안내 |
| TTL 안에 답 없음 | 질문 만료, 질문자 세션에 `kind="notice"` 푸쉬, 이후 reply는 410 |
| 질문자 세션이 재시작됨 | 같은 `user@workspace`로 재접속하면 쌓인 답변 재전달 |
| 푸쉬를 놓침 | `check_inbox`로 미확인 답변 조회 |

## 5. 알려진 한계와 주의점

- **리서치 프리뷰 기능**입니다. `--channels` 플래그와 프로토콜이 바뀔 수 있습니다. 로직은 브로커에 두고 채널 서버는 얇게 유지했습니다.
- **채널로 등록되지 않은 세션**(`--channels` 없이 실행)에서도 MCP 서버는 뜨고 브로커에 접속합니다. 하지만 푸쉬는 조용히 버려지고, 채널 서버 쪽에서는 이를 알 방법이 없습니다. 그래서 질문 수신은 `PEERS_LISTEN=1`로 명시적으로 켠 세션만 받게 했습니다. 답변을 놓치면 `check_inbox`로 복구합니다.
- 푸쉬는 **세션이 열려 있을 때만** 도착합니다. Claude가 작업 중이면 이벤트가 쌓였다가 다음 턴에 한꺼번에 처리됩니다.
- **permission relay(`claude/channel/permission`)는 일부러 선언하지 않았습니다.** 선언하면 채널로 메시지를 보낼 수 있는 사람이 내 세션의 도구 사용을 승인할 수 있게 됩니다. 동료 간 채널에서는 켜면 안 됩니다.
- 채널 본문은 다른 사람의 Claude가 쓴 텍스트이므로 프롬프트 인젝션 경로가 될 수 있습니다. 서버 instructions와 스킬에 방어 규칙을 넣었지만, 가장 확실한 방어는 응답 전용 세션의 도구 제한입니다.
- `MCP_PROTOCOL_NEGOTIATION=auto`를 설정하지 마세요. 새 프로토콜 리비전으로 협상하면 채널로 등록되지 않습니다.
- MCP 서버가 뜨는데 "설정이 비어 있습니다" 오류가 나면, `plugin.json`의 `${user_config.*}` 치환이 동작하지 않는 환경일 수 있습니다. 이 경우 env를 `"PEERS_TOKEN": "${PEERS_TOKEN:-}"` 형태로 바꾸고 셸 환경변수로 전달하세요.

채널이 안 붙을 때의 진단 순서는 [USAGE.md](USAGE.md#5-안-될-때)에 있습니다.
