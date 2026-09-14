#!/usr/bin/env bash
# 사내 서버에 Claude Peers 브로커를 올린다. 여러 번 돌려도 안전하다.
#
#   sudo ./deploy/install.sh
#
# 하는 일: 전용 계정과 디렉터리 생성, venv 구성, systemd 유닛 설치.
# 하지 않는 일: nginx 설정(deploy/nginx-claude-peers.conf 참고), 인증서, 토큰 발급.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/claude-peers}
DATA_DIR=${DATA_DIR:-/var/lib/claude-peers}
SVC_USER=${SVC_USER:-peers}

[[ $EUID -eq 0 ]] || { echo "root 로 실행하세요 (sudo)"; exit 1; }
[[ -f "$APP_DIR/broker/server.py" ]] || {
  echo "$APP_DIR/broker/server.py 가 없습니다."
  echo "저장소를 $APP_DIR 에 먼저 클론하세요:"
  echo "  git clone <저장소> $APP_DIR"
  exit 1
}

command -v uv >/dev/null || {
  echo "uv 가 필요합니다: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
}

echo "==> 서비스 계정"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

echo "==> 데이터 디렉터리"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$DATA_DIR"

echo "==> 파이썬 환경"
cd "$APP_DIR/broker"
uv venv .venv
uv pip install --python .venv/bin/python -e .
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

echo "==> systemd 유닛"
install -m 644 "$APP_DIR/deploy/claude-peers.service" /etc/systemd/system/claude-peers.service
systemctl daemon-reload
systemctl enable claude-peers
systemctl restart claude-peers

sleep 2
echo "==> 확인"
if curl -fsS --max-time 5 http://127.0.0.1:8080/healthz; then
  echo
  echo "브로커가 떴습니다."
else
  echo "기동 실패. 로그를 보세요: journalctl -u claude-peers -n 50"
  exit 1
fi

cat <<EOF

다음 할 일

  1. 토큰 발급 (사람마다 하나씩)
       sudo -u $SVC_USER PEERS_TOKENS=$DATA_DIR/tokens.json \\
         $APP_DIR/broker/.venv/bin/python $APP_DIR/broker/issue_token.py <사번이나 계정ID>

  2. 리버스 프록시
       deploy/nginx-claude-peers.conf 를 참고해 도메인과 인증서를 채운다.
       WebSocket upgrade 와 proxy_read_timeout 을 빠뜨리지 말 것.

  3. 백업
       peers.db 만 복사하면 안 된다 (WAL 모드라 테이블조차 안 보인다).
       sqlite3 $DATA_DIR/peers.db ".backup '/backup/peers-\$(date +%F).db'"

  4. 개발자 안내
       README 의 "설치하기" 절을 전달한다. uv 가 필요하다.
EOF
