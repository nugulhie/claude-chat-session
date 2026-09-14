// 브로커 + 채널 서버 4개를 띄우고 MCP 클라이언트로 Claude Code 역할을 흉내 내는 E2E 테스트
// 실행: (broker 디렉터리에서) npm test   ※ 플러그인 디렉터리에서 npm install 선행
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { spawn, execFileSync } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const here = dirname(fileURLToPath(import.meta.url))
// PATH의 `node`가 구버전일 수 있으므로 자식 프로세스도 이 테스트와 같은 인터프리터로 띄운다
const NODE = process.execPath
const BROKER_DIR = join(here, '..')
const PLUGIN_SERVER = join(here, '../../marketplace/plugins/peers/server.mjs')
const tmp = mkdtempSync(join(tmpdir(), 'peers-'))
const PORT = 18000 + Math.floor(Math.random() * 1000)
const BROKER_URL = `http://127.0.0.1:${PORT}`
const brokerEnv = {
  ...process.env, PORT: String(PORT), HOST: '127.0.0.1',
  PEERS_DB: join(tmp, 'peers.db'), PEERS_TOKENS: join(tmp, 'tokens.json'),
  QUESTION_TTL_SEC: '3', SWEEP_MS: '500', MAX_HOPS: '1', LIMIT_PER_PAIR: '5',
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function waitFor(fn, label, timeout = 8000) {
  const start = Date.now()
  for (;;) {
    const v = await fn()
    if (v) return v
    if (Date.now() - start > timeout) throw new Error(`timeout: ${label}`)
    await sleep(100)
  }
}

const token = (user) => execFileSync(NODE, ['issue-token.mjs', user], { cwd: BROKER_DIR, env: brokerEnv }).toString().trim()
const tokens = Object.fromEntries(['alice', 'bob', 'carol', 'dave'].map((u) => [u, token(u)]))

const broker = spawn(NODE, ['server.mjs'], { cwd: BROKER_DIR, env: brokerEnv, stdio: ['ignore', 'pipe', 'inherit'] })
broker.stdout.on('data', (d) => process.env.VERBOSE && process.stdout.write(`  [broker] ${d}`))
await waitFor(() => fetch(`${BROKER_URL}/healthz`).then((r) => r.ok).catch(() => false), 'broker up')

// 실패 경로에서도 전부 정리해야 한다. 살아 있는 채널 서버 자식 프로세스가 하나라도 남으면
// node가 종료되지 않아, 테스트가 실패 대신 멈춘 것처럼 보인다.
const openPeers = new Set()

async function peer(user, workspace, listen) {
  const transport = new StdioClientTransport({
    command: NODE, args: [PLUGIN_SERVER], stderr: 'pipe',
    env: { ...process.env, PEERS_BROKER_URL: BROKER_URL, PEERS_TOKEN: tokens[user], PEERS_LISTEN: listen ? '1' : '0', PEERS_WORKSPACE: workspace },
  })
  const client = new Client({ name: `fake-claude-code-${user}`, version: '0' }, { capabilities: {} })
  const events = []
  client.fallbackNotificationHandler = async (n) => {
    if (n.method === 'notifications/claude/channel') events.push(n.params)
  }
  await client.connect(transport)
  openPeers.add(client)
  const call = async (name, args = {}) => {
    const r = await client.callTool({ name, arguments: args })
    const text = r.content[0].text
    return { error: r.isError ? text : null, data: r.isError ? null : JSON.parse(text) }
  }
  await waitFor(async () => !(await call('list_peers')).error, `${user} connected`)
  return { user, client, events, call, instructions: client.getInstructions() }
}

let passed = 0
const ok = (label) => { passed++; console.log(`  ✓ ${label}`) }

try {
  const alice = await peer('alice', 'payments-web', false)
  const bob = await peer('bob', 'billing-api', true)
  const carol = await peer('carol', 'auth-api', true)
  const dave = await peer('dave', 'infra', true)

  // 1. 도구와 instructions
  const { tools } = await alice.client.listTools()
  assert.deepEqual(tools.map((t) => t.name).sort(), ['ask_peer', 'check_inbox', 'list_peers', 'reply', 'set_status'])
  assert.match(alice.instructions, /payments-web/)
  ok('도구 5개와 instructions 노출')

  // 2. presence
  const { data: { peers } } = await alice.call('list_peers')
  const byAddr = Object.fromEntries(peers.map((p) => [p.address, p]))
  assert.equal(byAddr['bob@billing-api'].listening, true)
  assert.equal(byAddr['alice@payments-web'].listening, false)
  assert.equal(byAddr['alice@payments-web'].you, true)
  ok('list_peers: 주소/수신 여부/본인 표시')

  await bob.call('set_status', { summary: '청구 배치 리팩터링 중' })
  const again = (await alice.call('list_peers')).data.peers.find((p) => p.address === 'bob@billing-api')
  assert.equal(again.summary, '청구 배치 리팩터링 중')
  ok('set_status: 작업 요약 공유')

  // 3. 질문 → 푸쉬 → 답변 → 푸쉬
  const asked = await alice.call('ask_peer', { to: 'bob', question: '취소 웹훅 재시도 정책 위치?', context: 'payments-web 중복 수신 버그' })
  assert.equal(asked.error, null)
  assert.equal(asked.data.to, 'bob@billing-api')
  const qEvent = await waitFor(() => bob.events.find((e) => e.meta.kind === 'question'), 'bob receives question')
  assert.equal(qEvent.meta.from, 'alice@payments-web')
  assert.equal(qEvent.meta.msg_id, asked.data.msg_id)
  assert.equal(qEvent.meta.hops, '0')
  assert.match(qEvent.content, /--- context ---/)
  assert.ok(Object.keys(qEvent.meta).every((k) => /^\w+$/.test(k)), 'meta 키는 식별자만')
  ok('ask_peer → 상대 세션에 notifications/claude/channel 푸쉬')

  const replied = await bob.call('reply', { msg_id: qEvent.meta.msg_id, text: 'src/webhooks/retry.ts:14, 최대 5회' })
  assert.equal(replied.data.delivered_live, true)
  const aEvent = await waitFor(() => alice.events.find((e) => e.meta.kind === 'answer'), 'alice receives answer')
  assert.equal(aEvent.meta.reply_to, asked.data.msg_id)
  assert.equal(aEvent.meta.from, 'bob@billing-api')
  ok('reply → 질문자 세션에 답변 푸쉬')

  const dup = await bob.call('reply', { msg_id: qEvent.meta.msg_id, text: '또 답함' })
  assert.match(dup.error, /409/)
  const notMine = await carol.call('reply', { msg_id: qEvent.meta.msg_id, text: '끼어들기' })
  assert.match(notMine.error, /403/)
  ok('중복 답변(409), 남의 질문 답변(403) 차단')

  // 4. 수신 꺼진 세션에는 질문 불가
  const toAlice = await bob.call('ask_peer', { to: 'alice', question: '?' })
  assert.match(toAlice.error, /404/)
  // 404 본문에는 대신 물어볼 수 있는 대상 목록이 담긴다 (자기 자신은 빠진다)
  const available = JSON.parse(toAlice.error.slice(toAlice.error.indexOf('{'))).available
  assert.ok(Array.isArray(available), 'available 목록이 배열')
  assert.ok(available.includes('carol@auth-api'), 'available에 수신 중인 동료 포함')
  assert.ok(!available.includes('bob@billing-api'), 'available에 본인 제외')
  ok('listening=false 세션으로 질문 차단(404) + available 목록')

  // 5. 재질문 깊이 제한과 핑퐁 차단
  const q1 = await alice.call('ask_peer', { to: 'bob@billing-api', question: 'Q1' })
  await waitFor(() => bob.events.find((e) => e.meta.msg_id === q1.data.msg_id), 'bob got Q1')
  const q2 = await bob.call('ask_peer', { to: 'carol', question: 'Q1 때문에 묻는 Q2' })
  assert.equal(q2.error, null)
  const q2Event = await waitFor(() => carol.events.find((e) => e.meta.msg_id === q2.data.msg_id), 'carol got Q2')
  assert.equal(q2Event.meta.hops, '1')
  const q3 = await carol.call('ask_peer', { to: 'dave', question: 'Q2 때문에 묻는 Q3' })
  assert.match(q3.error, /422.*재질문 한도/)
  const pingpong = await carol.call('ask_peer', { to: 'bob', question: '되묻기' })
  assert.match(pingpong.error, /422/)
  ok('hops 자동 계산, 한도 초과(422)와 되묻기(422) 차단')

  // 6. 만료 알림
  const n1 = await waitFor(() => alice.events.find((e) => e.meta.kind === 'notice' && e.meta.reply_to === q1.data.msg_id), 'alice expiry notice')
  assert.match(n1.content, /만료/)
  await waitFor(() => bob.events.find((e) => e.meta.kind === 'notice' && e.meta.reply_to === q2.data.msg_id), 'bob expiry notice')
  const late = await bob.call('reply', { msg_id: q1.data.msg_id, text: '늦은 답' })
  assert.match(late.error, /410/)
  ok('TTL 지난 질문 만료 → 질문자에게 notice, 늦은 답변 410')

  // 7. 오프라인 중 도착한 답변은 같은 workspace의 새 세션으로 재전달
  const q4 = await alice.call('ask_peer', { to: 'dave', question: 'Q4' })
  await waitFor(() => dave.events.find((e) => e.meta.msg_id === q4.data.msg_id), 'dave got Q4')
  await alice.client.close()
  openPeers.delete(alice.client)
  await sleep(300)
  const r4 = await dave.call('reply', { msg_id: q4.data.msg_id, text: 'A4' })
  assert.equal(r4.data.delivered_live, false)
  const alice2 = await peer('alice', 'payments-web', false)
  await waitFor(() => alice2.events.find((e) => e.meta.kind === 'answer' && e.meta.reply_to === q4.data.msg_id), 'alice2 gets A4 on reconnect')
  ok('질문자 세션 재시작 후 답변 재전달')

  // 8. check_inbox 폴백
  const inbox1 = await alice2.call('check_inbox')
  assert.ok(inbox1.data.messages.some((m) => m.reply_to === q4.data.msg_id))
  const inbox2 = await alice2.call('check_inbox')
  assert.equal(inbox2.data.messages.length, 0)
  ok('check_inbox: 미확인 메시지 반환 후 read 처리')

  // 9. 인증
  const bad = await fetch(`${BROKER_URL}/api/peers`, { headers: { authorization: 'Bearer pk_wrong' } })
  assert.equal(bad.status, 401)
  ok('잘못된 토큰 401')

  // 10. 같은 user가 두 워크스페이스에서 수신 중이면 user만으로는 지정할 수 없다
  const bob2 = await peer('bob', 'ledger-api', true)
  const ambiguous = await alice2.call('ask_peer', { to: 'bob', question: '모호한 대상' })
  assert.match(ambiguous.error, /409/)
  const cands = JSON.parse(ambiguous.error.slice(ambiguous.error.indexOf('{'))).candidates
  assert.deepEqual(cands.sort(), ['bob@billing-api', 'bob@ledger-api'])
  await bob2.client.close()
  openPeers.delete(bob2.client)
  ok('수신 세션이 여러 워크스페이스면 409 + user@workspace 후보 반환')

  // 11. 크기 제한
  const tooLong = await alice2.call('ask_peer', { to: 'bob@billing-api', question: 'x'.repeat(4001) })
  assert.match(tooLong.error, /413/)
  ok('question 4000자 초과 413')

  // 12. 같은 상대에게 반복 질문하면 rate limit (LIMIT_PER_PAIR=5)
  const codes = []
  for (let i = 0; i < 7; i++) {
    const r = await dave.call('ask_peer', { to: 'carol@auth-api', question: `rate${i}` })
    codes.push(r.error ? r.error.slice(0, 11) : 'ok')
  }
  assert.equal(codes.filter((c) => c === 'ok').length, 5, `5건만 통과해야 함: ${codes}`)
  assert.ok(codes.slice(5).every((c) => /429/.test(c)), `6번째부터 429: ${codes}`)
  ok('같은 상대 반복 질문 rate limit 429')

  // 13. 폐기된 토큰은 더 이상 통하지 않는다 (tokens.json 자동 리로드)
  execFileSync(NODE, ['issue-token.mjs', '--revoke', 'dave'], { cwd: BROKER_DIR, env: brokerEnv, stdio: 'ignore' })
  await waitFor(async () => /401/.test((await dave.call('list_peers')).error ?? ''), 'dave 토큰 폐기 반영', 10000)
  ok('--revoke 후 401 (tokens.json 자동 리로드)')

  console.log(`\n${passed}개 통과`)
} catch (e) {
  console.error('\n실패:', e)
  process.exitCode = 1
} finally {
  for (const c of openPeers) await c.close().catch(() => {})
  broker.kill()
}
