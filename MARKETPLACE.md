# 마켓플레이스로 배포하기

플러그인을 사람들에게 전달하는 방법입니다. 파일을 압축해 보내는 대신 **저장소 이름 하나만 알려주면** 됩니다.

```bash
claude plugin marketplace add nugulhie/claude-chat-session
claude plugin install peers@claude-peers
```

이 문서는 배포하는 쪽과 설치하는 쪽을 모두 다룹니다. 브로커 운영은 [OPERATIONS.md](OPERATIONS.md)를 보세요.

## 마켓플레이스가 뭔가

플러그인 목록을 담은 git 저장소입니다. 별도의 레지스트리 서버나 계정 등록이 없습니다. `.claude-plugin/marketplace.json` 파일 하나가 있는 저장소면 그게 곧 마켓플레이스입니다.

Claude Code는 저장소를 클론해 `~/.claude/plugins/marketplaces/<이름>/`에 두고, 거기서 플러그인을 읽어 `~/.claude/plugins/cache/<마켓플레이스>/<플러그인>/<버전>/`으로 설치합니다.

## 저장소 레이아웃

**매니페스트는 반드시 저장소 루트의 `.claude-plugin/marketplace.json`이어야 합니다.**

```
claude-peers/                          ← 저장소 루트
├── .claude-plugin/
│   └── marketplace.json               ← 여기여야 한다
└── marketplace/
    └── plugins/
        └── peers/
            ├── .claude-plugin/
            │   └── plugin.json
            ├── server.py
            └── skills/
```

이걸 하위 폴더에 두면 git으로 받을 때 이렇게 실패합니다.

```
✘ Failed to add marketplace: Marketplace file not found at
  ~/.claude/plugins/marketplaces/<이름>/.claude-plugin/marketplace.json
```

로컬 경로로 등록할 때는 그 경로를 루트로 치기 때문에 하위 폴더에 있어도 동작합니다. **그래서 로컬에서는 되는데 git으로 바꾸면 깨지는 일이 생깁니다.** 이 저장소도 그 문제를 한 번 겪고 매니페스트를 루트로 옮겼습니다.

플러그인 실물은 아무 데나 둬도 됩니다. 매니페스트의 `source`가 루트 기준 상대 경로로 가리키기만 하면 됩니다.

## marketplace.json

```json
{
  "$schema": "https://json.schemastore.org/claude-code-marketplace.json",
  "name": "claude-peers",
  "description": "Claude Code 세션 간 질문/답변 채널 플러그인",
  "owner": { "name": "nugulhie", "url": "https://github.com/nugulhie" },
  "plugins": [
    {
      "name": "peers",
      "source": "./marketplace/plugins/peers",
      "description": "동료 Claude Code 세션에 질문을 푸쉬하고 답을 받는 채널"
    }
  ]
}
```

`name`이 설치할 때 쓰는 이름입니다. `claude plugin install peers@claude-peers`의 `@` 뒤가 이것입니다. **저장소 이름과 무관하게 이 값이 쓰이므로**, 공개 배포할 거면 저장소 성격에 맞게 정하세요. 이 값을 바꾸면 기존 설치자의 설치 명령도 바뀝니다.

플러그인 하나에 `plugin.json`이 하나씩 따로 있습니다. 둘의 역할이 다릅니다.

| 파일 | 무엇을 정하나 |
|---|---|
| `marketplace.json` | 이 저장소가 어떤 플러그인들을 제공하는지, 각각 어디 있는지 |
| `plugin.json` | 그 플러그인이 무엇을 하는지 — MCP 서버, 채널, userConfig, 버전 |

## 배포하기

push하면 끝입니다. 빌드도 업로드도 없습니다.

```bash
git add -A && git commit -m "..." && git push
```

**고칠 때마다 `plugin.json`의 `version`을 올리세요.** 버전이 캐시 디렉터리 이름이 됩니다.

```
~/.claude/plugins/cache/claude-peers/peers/0.1.0/
                                            ^^^^^
```

올리지 않으면 기존 설치자가 `claude plugin update`를 해도 같은 디렉터리를 보므로 새 코드가 내려가지 않을 수 있습니다.

배포 전에 검증할 수 있습니다.

```bash
claude plugin validate .                              # 마켓플레이스 매니페스트
claude plugin validate ./marketplace/plugins/peers    # 플러그인 매니페스트
```

스키마만 보는 검사라 통과해도 런타임이 동작한다는 보장은 아닙니다. 실제 확인은 설치해서 `claude mcp list`에 `✔ Connected`가 뜨는지로 합니다.

## 설치하기

소스는 세 가지 형태를 받습니다.

```bash
# GitHub 저장소 — 배포된 상태 그대로. 동료에게 안내할 방법
claude plugin marketplace add nugulhie/claude-chat-session

# 로컬 경로 — 원본을 직접 실행하므로 개발 중에 편하다
claude plugin marketplace add .

# URL
claude plugin marketplace add https://github.com/nugulhie/claude-chat-session.git
```

그다음 플러그인을 설치합니다. `userConfig`가 있으면 `--config`로 넘깁니다.

```bash
claude plugin install peers@claude-peers \
  --config broker_url=https://peers.example.com \
  --config token=<개인 토큰>
```

필수 설정을 빠뜨리면 알려줍니다.

```
1 userConfig option not yet set (1 required) — run /plugin configure peers@claude-peers
```

`sensitive: true`인 값(토큰 등)은 `settings.json`이 아니라 보안 저장소로 갑니다. 설정 파일을 열어봐도 안 보이는 게 정상입니다.

### 어디에 등록할지

```bash
claude plugin marketplace add <소스> --scope project
```

| scope | 쓰임 |
|---|---|
| `user` (기본) | 내 계정 전체에서 쓴다 |
| `project` | 프로젝트 설정에 선언한다. 저장소에 커밋하면 팀원이 클론만 해도 마켓플레이스가 잡힌다 |
| `local` | 이 머신의 이 프로젝트에만 |

팀 전체가 같은 플러그인을 쓴다면 `--scope project`로 넣고 커밋해 두는 쪽이 각자 등록하게 하는 것보다 낫습니다.

### 모노레포

큰 저장소의 일부만 필요하면 체크아웃을 줄일 수 있습니다.

```bash
claude plugin marketplace add nugulhie/claude-chat-session --sparse .claude-plugin plugins
```

## 갱신

두 단계입니다. 마켓플레이스를 새로고침하고, 플러그인을 올립니다.

```bash
claude plugin marketplace update claude-peers   # 저장소 다시 받기 (이름 생략 시 전부)
claude plugin update peers@claude-peers         # 플러그인 갱신 — 재시작해야 적용
```

## 비공개 저장소

사내용이면 저장소를 private으로 두면 됩니다. Claude Code가 git으로 클론하므로 **설치하는 사람에게 그 저장소 git 접근 권한이 있어야 합니다.** SSH 키나 `gh auth`가 이미 돼 있으면 따로 할 일은 없습니다.

권한이 없으면 `marketplace add`가 클론 단계에서 실패합니다. 이때는 저장소 접근부터 해결해야 하고, 플러그인 설정 문제가 아닙니다.

## 조직 전체에 미리 깔아 두기

managed settings로 배포하면 개발자가 `marketplace add`를 칠 필요도 없습니다. [admin/managed-settings.json](admin/managed-settings.json)을 참고하세요.

```json
{
  "extraKnownMarketplaces": {
    "claude-peers": {
      "source": { "source": "github", "repo": "<your-org>/<your-fork>" }
    }
  },
  "enabledPlugins": { "peers@claude-peers": true },
  "pluginConfigs": {
    "peers@claude-peers": { "options": { "broker_url": "https://peers.<your-org>.internal" } }
  }
}
```

`broker_url`처럼 모두에게 같은 값은 미리 채워 두고, 토큰만 개인이 입력하게 하면 됩니다.

**이 두 값은 반드시 조직이 통제하는 것으로 바꾸세요.** managed settings는 조직 전체에 적용되는 정책이라 무게가 다릅니다.

- `extraKnownMarketplaces` + `enabledPlugins`를 함께 쓰면 **모든 개발자 머신이 그 저장소의 기본 브랜치 코드를 자동으로 실행합니다.** 남의 저장소를 그대로 가리키면 그쪽이 바뀔 때마다 사내 전 머신에 반영됩니다. 포크해서 조직 저장소를 가리키고, 검토한 뒤 반영하세요.
- `broker_url`은 **모든 개발자의 토큰과 질문·답변 본문이 도착할 주소**입니다. 조직이 운영하는 브로커의 `https` 주소여야 합니다.

## 문제 해결

| 증상 | 원인 |
|---|---|
| `Marketplace file not found` | 매니페스트가 저장소 루트에 없습니다. `.claude-plugin/marketplace.json`을 루트로 옮기세요 |
| 클론 단계에서 실패 | 비공개 저장소인데 git 접근 권한이 없습니다 |
| 설치는 됐는데 `CONNECTION_CLOSED` | 플러그인 런타임 문제입니다. [USAGE.md의 진단 순서](USAGE.md#6-안-될-때)를 따르세요 |
| 고쳤는데 반영이 안 됨 | `plugin.json`의 `version`을 올렸는지 확인하고, `marketplace update` → `plugin update` 순서로 갱신하세요 |
| `plugin details`에 `MCP servers (0)` | 표시상의 한계입니다. 인라인 `mcpServers` 선언을 인벤토리가 세지 않을 뿐, 실제로는 동작합니다 |

현재 등록된 마켓플레이스와 그 소스는 이걸로 봅니다.

```bash
claude plugin marketplace list
```
