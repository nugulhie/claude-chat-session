# 브로커 운영

브로커를 배포하고 굴리는 사람을 위한 문서입니다. 설계는 [ARCHITECTURE.md](ARCHITECTURE.md), 개발자 사용법은 [USAGE.md](USAGE.md)를 보세요.

브로커는 외부 의존성이 `aiohttp` 하나뿐인 단일 파이썬 프로세스입니다. 데이터베이스는 파일 하나(SQLite)입니다. 별도의 인프라가 필요 없습니다.

## 요구사항

- **파이썬 3.11 이상.** 의존성은 `aiohttp` 하나이고 나머지는 표준 라이브러리(`sqlite3`)입니다.
- TLS와 WebSocket upgrade를 지원하는 리버스 프록시 (nginx, ALB 등)
- 디스크: 메시지 한 건이 수 KB입니다. 보관 정책에 따라 다르지만 수 GB를 넘기 어렵습니다.

## 배포

### 리버스 프록시

`https://`로 노출하세요. 채널 서버는 URL의 `http`를 `ws`로 바꿔 `/stream`에 연결하므로 `https` → `wss`가 됩니다. **WebSocket upgrade가 통과해야 합니다.**

`deploy/nginx-claude-peers.conf` 에 파일로 있습니다. 도메인과 인증서만 채우면 됩니다.

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;

    # 세션이 오래 붙어 있으므로 idle timeout을 길게 잡는다.
    # 브로커가 30초마다 ping을 보내지만 프록시가 먼저 끊으면 소용없다.
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

ALB를 쓴다면 idle timeout을 기본값(60초)에서 늘리세요. 브로커의 ping 주기(30초)보다 짧으면 연결이 계속 끊깁니다.

### 단일 인스턴스

**presence와 rate limit이 메모리에 있습니다.** 두 대 이상 띄우면 서로 다른 인스턴스에 붙은 세션끼리는 보이지 않고, rate limit도 인스턴스마다 따로 셉니다. 로드밸런서 뒤에 한 대만 두세요.

이중화가 필요하면 presence와 푸쉬를 Redis pub/sub으로 옮겨야 합니다. DB는 이미 파일이므로 그것부터 공유 스토리지나 다른 엔진으로 바꿔야 합니다.

### systemd 예시

`deploy/claude-peers.service` 에 같은 내용이 파일로 있습니다. `deploy/install.sh` 가 설치해 줍니다.

```ini
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

`HOST=127.0.0.1`로 묶고 프록시만 붙게 하세요. 브로커 자체는 TLS를 하지 않습니다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PORT` / `HOST` | `8080` / `0.0.0.0` | 리슨 주소 |
| `PEERS_DB` | `./peers.db` | SQLite 파일 경로 |
| `PEERS_TOKENS` | `./tokens.json` | 토큰 해시 파일 경로 |
| `QUESTION_TTL_SEC` | `900` | 답을 기다리는 최대 시간. 지나면 질문자에게 만료 notice |
| `INBOX_TTL_SEC` | `86400` | 전달 못 한 답변 보관 기간 |
| `MAX_HOPS` | `1` | 받은 질문을 처리하다 다시 물을 수 있는 깊이 |
| `LIMIT_PER_USER` | `30` | 10분당 한 사용자의 질문 수 |
| `LIMIT_PER_PAIR` | `10` | 10분당 같은 상대에게 보내는 질문 수 |
| `SWEEP_MS` | `15000` | 만료 처리 주기 |
| `ROOM_RESERVE_SEC` | `1800` | `create_room`으로 만든 빈 방이 아무도 안 들어와도 남아 있는 시간 |
| `MAX_RESERVED_PER_USER` | `5` | 한 사용자가 동시에 예약해 둘 수 있는 **빈** 방 수. 초과하면 429 |

`QUESTION_TTL_SEC`을 늘릴 때는 신중하세요. 길수록 질문자가 오래 기다리고, 그동안 열린 질문이 `hops` 계산에 잡혀 그 세션의 다른 질문을 막습니다.

방은 메모리에만 있고 사람이 다 나가면 사라집니다. `ROOM_RESERVE_SEC`은 그 예외로, `create_room` 직후 아무도 없는 동안 방 이름을 잡아 두는 시간입니다. 예약은 브로커 재시작에 사라지지만 그동안 아무도 들어오지 않은 방이라 손실이 없습니다.

## 토큰 운영

발급하면 원본이 한 번만 출력되고, 서버에는 SHA-256 해시만 저장됩니다.

```bash
python issue_token.py alice          # pk_... 출력. 이때 받아서 전달
python issue_token.py --revoke alice # alice의 모든 토큰 폐기
```

`tokens.json`은 2초 간격으로 변경을 감지해 자동으로 다시 읽습니다. **폐기에 재시작이 필요 없습니다.** 퇴사자 처리는 `--revoke` 한 번이면 끝나고, 반영까지 몇 초 걸립니다.

파일 권한은 `0600`으로 만들어집니다. 해시만 들어 있지만 사용자 목록이 노출되므로 그대로 두세요.

### 운영에서는 SSO로 교체하세요

`server.py`의 `authenticate()`가 토큰 검증 지점입니다. 사내 SSO/OIDC 검증으로 갈아끼우면 토큰 파일 관리가 통째로 없어집니다. 함수 하나만 바꾸면 되도록 격리해 뒀습니다 — 반환값은 사용자 이름 문자열이거나 `null`입니다.

## 백업

### peers.db만 복사하면 안 됩니다

WAL 모드로 돌아가므로 최근 데이터가 `peers.db-wal`에 있습니다. 실제로 확인해 보면, 돌아가던 브로커의 `peers.db`만 복사한 파일은 **테이블조차 없습니다.**

```
peers.db     -> messages: 0        (wal/shm과 함께 있을 때)
only-db.db   -> 에러: no such table: messages   (peers.db만 복사)
```

백업은 둘 중 하나로 하세요.

```bash
# 권장: 돌아가는 중에도 안전한 온라인 백업
sqlite3 /var/lib/claude-peers/peers.db ".backup '/backup/peers-$(date +%F).db'"

# 또는 세 파일을 함께
cp peers.db peers.db-wal peers.db-shm /backup/
```

### 종료 시 동작

**SIGTERM으로 종료하면 WAL이 체크포인트되지 않고 남습니다.** 확인된 동작입니다. SQLite는 크래시 안전하므로 데이터가 날아가지는 않고, 다음 기동 때 WAL을 읽어 복구합니다. 다만 백업을 뜰 때 이 점을 알고 있어야 합니다.

깔끔한 체크포인트가 필요하면 중지한 뒤 한 번 돌려주세요.

```bash
systemctl stop claude-peers
sqlite3 /var/lib/claude-peers/peers.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

## 재시작할 때 일어나는 일

재시작은 비교적 안전합니다. 무엇이 남고 무엇이 사라지는지만 알아 두세요.

**사라지는 것** — presence(접속 중인 세션 목록)와 rate limit 카운터. 세션은 전부 끊기고, 질문 횟수 제한은 0부터 다시 셉니다.

**남는 것** — 모든 메시지와 그 상태. 전달하지 못한 질문과 답변이 DB에 그대로 있습니다.

**복구 과정** — 채널 서버가 1초부터 시작해 최대 30초까지 지수 백오프로 재연결합니다. 붙으면 브로커가 그 세션 앞으로 ack되지 않은 메시지를 다시 밀어 넣습니다. 세션이 새 sid로 붙어도 같은 `user@workspace`면 이전 세션 앞으로 쌓인 답변을 새 세션으로 옮겨서 전달합니다.

즉 **롤링 재시작 중에 오간 질문은 유실되지 않습니다.** 다만 그 사이 `ask_peer`를 호출한 세션은 브로커 연결이 없어 에러를 받습니다. 배포는 질문이 적은 시간대에 하세요.

## 모니터링

### 헬스체크

```bash
curl -s https://peers.example.com/healthz
# {"ok":true,"sessions":3,"rooms":2}
```

인증이 필요 없고, `sessions`로 현재 접속 세션 수를, `rooms`로 지금 살아 있는 방 수(`public` 포함)를 함께 알려줍니다. `sessions`가 갑자기 0이 되면 프록시의 WebSocket upgrade나 idle timeout을 의심하세요.

### 로그

한 줄에 하나씩 타임스탬프와 함께 stdout으로 나갑니다.

```
2026-09-11T07:46:02.675Z connect alice@billing-api sid=c25bc2b9 listening=true
2026-09-11T07:46:54.893Z ask c4ea4083 alice@payments-web -> alice@billing-api hops=0
2026-09-11T07:47:27.496Z reply 8c8169d4 for c4ea4083
2026-09-11T07:47:45.624Z disconnect alice@billing-api sid=c25bc2b9
2026-09-11T07:48:10.001Z expired c4ea4083
```

볼 만한 신호입니다.

- `expired`가 잦다 → 응답 세션을 띄워 두지 않았거나 TTL이 짧습니다
- `connect`/`disconnect`가 짧은 주기로 반복 → 프록시 timeout
- `internal error` → 버그. 스택이 함께 찍힙니다

### 감사 질의

`messages` 테이블을 직접 보면 됩니다.

```sql
-- 최근 오간 질문과 답
SELECT created_at, kind, from_user, to_user, status, substr(body,1,80)
FROM messages ORDER BY created_at DESC LIMIT 50;

-- 답을 못 받고 만료된 질문
SELECT from_user, to_user, substr(body,1,120)
FROM messages WHERE kind='question' AND status='expired';

-- 사용자별 질문 수 (최근 7일)
SELECT from_user, COUNT(*) FROM messages
WHERE kind='question' AND created_at > (strftime('%s','now')-604800)*1000
GROUP BY from_user ORDER BY 2 DESC;

-- 한 방에서 오간 대화 전체
SELECT created_at, kind, from_user, to_user, substr(body,1,80)
FROM messages WHERE room = 'webhook-dup' ORDER BY created_at;
```

`messages.room`은 **보낸 시점의 방**이고 이후 바뀌지 않습니다. 참여자가 나중에 방을 옮겨도 그 대화는 원래 방에 묶인 채로 남으므로, 방 단위 질의는 대화가 끝난 뒤에도 그대로 동작합니다. 방 자체는 메모리에만 있어 사람이 나가면 사라지지만 이 컬럼은 남습니다.

## 보관 정책

전달하지 못한 답변과 알림은 `INBOX_TTL_SEC`(기본 하루)이 지나면 `dropped`로 바뀝니다. 하지만 **행이 지워지지는 않습니다.** 질문·답변·만료 기록은 전부 영구히 남습니다.

이게 감사 로그로는 맞지만 디스크는 계속 자랍니다. 조직의 보관 기간을 정하고 주기적으로 정리하세요.

```sql
-- 180일 지난 기록 삭제 (보관 기간은 조직 정책에 맞게)
DELETE FROM messages WHERE created_at < (strftime('%s','now') - 180*86400)*1000;
```

지우기 전에 반드시 백업하세요. 이 테이블이 "누가 누구에게 무엇을 물었는지"의 유일한 기록입니다.

정리 후에는 파일 크기를 되돌리기 위해 한 번 정리해 주면 좋습니다.

```bash
systemctl stop claude-peers
sqlite3 /var/lib/claude-peers/peers.db "VACUUM;"
systemctl start claude-peers
```

## 장애 대응

| 증상 | 먼저 볼 곳 |
|---|---|
| 아무도 서로를 못 본다 | `/healthz`의 `sessions`가 0인지. 0이면 프록시의 WebSocket upgrade 설정 |
| 연결이 계속 끊겼다 붙는다 | 프록시 idle timeout이 30초보다 짧은지 |
| 특정 사용자만 401 | 토큰이 폐기됐는지. `tokens.json`에서 해당 `user`의 `revoked` 확인 |
| 한 사람만 모든 도구가 409 | 그 사람 WebSocket이 안 붙은 것입니다. 브로커 로그에 그 `user`의 `connect` 줄이 아예 없으면 확정입니다. `deploy/doctor.py`를 돌려 보게 하세요 |
| 질문이 전부 404 | 상대가 `PEERS_LISTEN=1`로 띄웠는지. 404 응답에 대체 가능한 대상 목록이 함께 옵니다 |
| 질문이 429 | 정상 동작입니다. 필요하면 `LIMIT_PER_PAIR`를 올리세요 |
| 기동하자마자 죽는다 | `python -V`가 3.11 이상인지, `aiohttp`가 설치돼 있는지 |

개발자 쪽 문제(채널이 안 붙음, "설정이 비어 있습니다")는 [USAGE.md의 진단 순서](USAGE.md#6-안-될-때)를 안내하세요. 대부분 브로커가 아니라 클라이언트 쪽 `uv` 설치 여부나 연결 실패 캐시입니다.

특정 개인이 안 붙는다는 제보를 받으면 **브로커 로그에서 그 사람의 `connect` 줄을 먼저 찾으세요.** 줄이 없으면 요청이 서버에 닿지도 않은 것이라 서버 쪽에서 더 볼 것이 없습니다. `deploy/doctor.py <브로커주소> <토큰>`을 그 사람 머신에서 돌리게 하면 원인이 한 번에 나옵니다.

> 액세스 로그의 클라이언트 IP는 사무실 전체가 하나의 NAT 주소로 찍힙니다. IP로는 누구인지 구분할 수 없으니 브로커 로그의 `user`를 보세요.

## 플러그인 배포

저장소를 push하면 그대로 마켓플레이스가 됩니다. 개발자는 경로를 받을 필요 없이 저장소 이름만 있으면 됩니다.

```bash
claude plugin marketplace add nugulhie/claude-chat-session
claude plugin install peers@claude-peers --config broker_url=<주소> --config token=<토큰>
```

저장소 레이아웃, 버전 올리기, 비공개 저장소, 갱신 흐름은 [MARKETPLACE.md](MARKETPLACE.md)에 정리돼 있습니다.

**개발자 머신에 `uv`가 필요합니다.** 채널 서버는 `uv run --script`로 뜨고, 의존성은 스크립트 첫머리의 PEP 723 메타데이터에 선언돼 있어 uv가 알아서 받아 캐시합니다. 배포 안내에 uv 설치를 함께 넣으세요.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

첫 설치자에게서 `claude mcp list`의 `plugin:peers:peers`가 `✔ Connected`인지 확인하면 됩니다.

## 조직 설정 배포

`admin/managed-settings.json`을 참고해 managed settings를 배포합니다.

- `allowedChannelPlugins`에 플러그인을 넣으면 개발자가 `--dangerously-load-development-channels` 없이 `--channels`로 켤 수 있습니다
- `extraKnownMarketplaces`의 source를 사내 git 호스팅에 맞게 바꿉니다
- `pluginConfigs`로 `broker_url`을 미리 채워 두면 개발자는 토큰만 입력하면 됩니다
- 읽기 계열 도구(`list_peers`, `check_inbox`, `set_status`)만 permission allow에 넣고, `ask_peer`는 넣지 마세요. 내 코드 컨텍스트가 밖으로 나가는 순간은 사람이 한 번 보는 편이 안전합니다

claude.ai Team/Enterprise 조직은 Owner가 **Admin settings → Claude Code → Channels**에서 채널을 켜야 합니다. 꺼져 있으면 도구는 동작하지만 푸쉬가 도착하지 않습니다.

## 보안 점검 목록

배포 전에 확인하세요.

- [ ] 브로커가 `127.0.0.1`에 묶여 있고 프록시만 외부에 노출된다
- [ ] `https`/`wss`로만 접근된다
- [ ] `tokens.json` 권한이 `0600`이고 백업에 평문 토큰이 없다
- [ ] `peers.db` 백업이 암호화된 곳에 저장된다 (질문·답변 본문이 그대로 들어 있다)
- [ ] 보관 기간이 정해져 있고 정리가 자동화돼 있다
- [ ] permission relay를 켜지 않았다 (채널 서버가 `claude/channel/permission`을 선언하지 않음)
- [ ] 개발자에게 응답 전용 세션의 도구 제한을 안내했다
- [ ] 운영 인증을 SSO로 교체할 계획이 있다
