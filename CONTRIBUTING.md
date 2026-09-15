# 기여하기

브랜치 운영과 릴리스 절차입니다. 설계 배경은 [ARCHITECTURE.md](ARCHITECTURE.md)를 보세요.

## 브랜치

```
feature/*  ──┐
             ├──► develop ──► release-* ──► main ──► 릴리스 자동 생성
버그수정   ──┘                    │
                                  └─ 여기서 QA. 고칠 것이 있으면 release-* 에 직접 커밋
```

| 브랜치 | 쓰임 |
|---|---|
| `main` | 배포된 것. 여기 있는 것이 곧 릴리스입니다 |
| `develop` | 다음 릴리스에 들어갈 것들이 모이는 곳. 평소 작업의 기준 |
| `release-*` | 릴리스 후보. 버전 올리기와 막바지 수정만 합니다 |
| `feature/*` | 작업 단위. `develop`에서 따고 `develop`으로 돌아갑니다 |

## 평소 작업

```bash
git switch develop && git pull
git switch -c feature/방-나가기
# ... 작업, 커밋 ...
git push -u origin feature/방-나가기
gh pr create --base develop
```

PR을 열면 테스트가 돕니다. 초록불이어야 머지합니다.

## 릴리스

**1. 릴리스 브랜치를 판다**

```bash
git switch develop && git pull
git switch -c release-0.5.0
```

**2. 버전을 올린다**

```
marketplace/plugins/peers/.claude-plugin/plugin.json 의 "version"
```

**이 값이 릴리스 태그가 됩니다.** 올리지 않으면 태그가 이미 존재하므로 릴리스가 만들어지지 않습니다. 그리고 플러그인 캐시 디렉터리 이름이 이 버전이라, **올리지 않으면 사용자가 `plugin update`를 해도 새 코드를 받지 못합니다.**

**3. QA 하고, 고칠 것이 있으면 이 브랜치에 커밋한다**

```bash
git push -u origin release-0.5.0
```

push할 때마다 테스트가 돕니다.

**4. main으로 PR을 연다**

```bash
gh pr create --base main --title "release 0.5.0"
```

테스트가 실패하면 머지 버튼이 잠깁니다.

**5. 머지한다**

머지되면 `release` 워크플로가 테스트를 한 번 더 돌리고, 통과하면 `v0.5.0` 태그와 릴리스를 만듭니다. 릴리스 노트는 이전 태그 이후의 커밋 제목으로 자동 생성됩니다.

**6. develop으로 되돌린다**

릴리스 중에 고친 것이 `develop`에 없으면 다음 릴리스에서 되살아납니다.

```bash
git switch develop && git merge --no-ff release-0.5.0 && git push
```

## CI

| 워크플로 | 언제 | 하는 일 |
|---|---|---|
| `test` | `main`/`develop`으로 가는 PR, `release-*`·`develop` push | E2E 30개를 Python 3.11·3.12에서, 문서 링크 확인 |
| `release` | `main` push | 테스트 재확인 후 태그와 릴리스 생성 |

`test`가 3.11과 3.12를 같이 도는 이유가 있습니다. `pyproject.toml`이 3.11을 최소 버전으로 선언해 두었는데, 선언만 하고 시험하지 않으면 그 버전 사용자만 깨집니다. 이 프로젝트에서 실제로 겪은 일입니다 — 개발 머신의 Node가 최신이라 통과하던 코드가 README에 적힌 최소 버전에서는 기동조차 못 했습니다.

### 머지를 막으려면 branch protection이 필요합니다

**워크플로만으로는 머지가 막히지 않습니다.** 빨간불이 떠도 머지 버튼은 눌립니다. `main`에 branch protection을 걸고 `test` job들을 required status check로 지정해야 실제로 잠깁니다.

```bash
gh api -X PUT repos/<소유자>/<저장소>/branches/main/protection \
  --input .github/branch-protection.json
```

설정 내용은 [.github/branch-protection.json](.github/branch-protection.json)에 있습니다.

required check 이름은 `<job 이름> (<matrix 값>)` 형식입니다. **첫 CI 실행 뒤에 실제 이름을 확인하고 맞추세요** — 이름이 하나라도 틀리면 그 검사는 영원히 대기 상태로 남고 머지가 아예 불가능해집니다.

```bash
gh run view --json jobs --jq '.jobs[].name'
```

## 테스트 직접 돌리기

```bash
cd broker
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python "mcp>=2.0" "websockets>=13" "httpx>=0.27" certifi
.venv/bin/python tests/e2e.py
```

한 번에 브로커와 채널 서버를 띄우고 30개 시나리오를 돕니다. 50초쯤 걸립니다. 자세한 내용은 `broker/tests/e2e.py` 첫머리 주석에 있습니다.

문서 링크 확인:

```bash
python3 .github/scripts/check_links.py
```

## 테스트를 고칠 때

**버그를 고치면 회귀 테스트를 같이 넣으세요.** 그리고 그 테스트가 **수정 없이는 실패하는지 확인하세요.** 통과만 확인한 테스트는 아무것도 지키지 못합니다.

```bash
# 수정을 잠시 되돌리고 테스트가 실패하는지 본다
git stash -- <고친 파일>
.venv/bin/python tests/e2e.py    # 실패해야 정상
git stash pop
```
