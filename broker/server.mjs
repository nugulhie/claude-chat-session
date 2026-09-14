#!/usr/bin/env node
// Claude Peers 브로커
// - 토큰 인증, 세션 presence, 질문/답변 라우팅
// - SQLite에 모든 메시지 저장 (감사 로그 겸용)
// - 질문 만료, 재질문(hops) 제한, rate limit
// Node 22.13+ 필요 (node:sqlite)

import http from 'node:http'
import { randomUUID, createHash } from 'node:crypto'
import { readFileSync, existsSync, watchFile } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'
import { WebSocketServer } from 'ws'

// ─── 설정 ────────────────────────────────────────────────────────────────
const PORT = Number(process.env.PORT ?? 8080)
const HOST = process.env.HOST ?? '0.0.0.0'
const DB_PATH = process.env.PEERS_DB ?? './peers.db'
const TOKENS_PATH = process.env.PEERS_TOKENS ?? './tokens.json'
const QUESTION_TTL_MS = Number(process.env.QUESTION_TTL_SEC ?? 900) * 1000 // 답을 기다리는 최대 시간
const INBOX_TTL_MS = Number(process.env.INBOX_TTL_SEC ?? 86400) * 1000 // 못 받은 답변 보관 기간
const MAX_HOPS = Number(process.env.MAX_HOPS ?? 1) // 받은 질문 때문에 다시 묻는 깊이
const SWEEP_MS = Number(process.env.SWEEP_MS ?? 15000)
const LIMIT_WINDOW_MS = 10 * 60 * 1000
const LIMIT_PER_USER = Number(process.env.LIMIT_PER_USER ?? 30) // 10분당 질문 수
const LIMIT_PER_PAIR = Number(process.env.LIMIT_PER_PAIR ?? 10) // 10분당 같은 상대에게
const MAX_QUESTION = 4000
const MAX_CONTEXT = 12000
const MAX_ANSWER = 16000
const MAX_SUMMARY = 300

const log = (...a) => console.log(new Date().toISOString(), ...a)
const now = () => Date.now()

// ─── 인증 (tokens.json: { sha256(token): { user, revoked? } }) ─────────────
// 운영에서는 이 부분을 사내 SSO/OIDC 검증으로 교체하세요.
let tokens = {}
function loadTokens() {
  try {
    tokens = existsSync(TOKENS_PATH) ? JSON.parse(readFileSync(TOKENS_PATH, 'utf8')) : {}
  } catch (e) {
    log('tokens.json 파싱 실패:', e.message)
  }
}
loadTokens()
watchFile(TOKENS_PATH, { interval: 2000 }, loadTokens)

const sha256 = (s) => createHash('sha256').update(s).digest('hex')
function authenticate(req) {
  const m = /^Bearer\s+(\S+)$/.exec(req.headers.authorization ?? '')
  const entry = m && tokens[sha256(m[1])]
  return entry && !entry.revoked ? entry.user : null
}

// ─── DB ──────────────────────────────────────────────────────────────────
const db = new DatabaseSync(DB_PATH)
// db를 이후에 참조하지 않으면 GC가 DatabaseSync를 수거하면서 아래 prepare된 문장들까지
// finalize한다(Node 22.x). 종료 시 닫아 주면서 참조도 함께 유지한다.
process.on('exit', () => { try { db.close() } catch {} })
db.exec(`
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
    status     TEXT NOT NULL,              -- queued | pushed | answered | expired | read | dropped
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_to_sid  ON messages(to_sid, status);
  CREATE INDEX IF NOT EXISTS idx_to_user ON messages(to_user, kind, status);
`)

const q = {
  insert: db.prepare(`INSERT INTO messages
    (id, kind, from_user, from_ws, from_sid, to_user, to_ws, to_sid, reply_to, hops, body, context, status, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`),
  byId: db.prepare(`SELECT * FROM messages WHERE id = ?`),
  setStatus: db.prepare(`UPDATE messages SET status = ?, updated_at = ? WHERE id = ?`),
  ack: db.prepare(`UPDATE messages SET status = 'pushed', updated_at = ? WHERE id = ? AND to_sid = ? AND status = 'queued'`),
  unackedFor: db.prepare(`SELECT * FROM messages WHERE to_sid = ? AND status = 'queued' ORDER BY created_at`),
  orphanInbox: db.prepare(`SELECT * FROM messages
    WHERE to_user = ? AND to_ws = ? AND kind IN ('answer','notice') AND status IN ('queued','pushed') ORDER BY created_at`),
  readdress: db.prepare(`UPDATE messages SET to_sid = ?, status = 'queued', updated_at = ? WHERE id = ?`),
  maxOpenHops: db.prepare(`SELECT MAX(hops) AS h FROM messages
    WHERE kind = 'question' AND to_sid = ? AND status IN ('queued','pushed')`),
  openFrom: db.prepare(`SELECT id FROM messages
    WHERE kind = 'question' AND to_sid = ? AND from_sid = ? AND status IN ('queued','pushed') LIMIT 1`),
  inbox: db.prepare(`SELECT * FROM messages
    WHERE to_user = ? AND kind IN ('answer','notice') AND status IN ('queued','pushed') ORDER BY created_at`),
  openQuestionsTo: db.prepare(`SELECT * FROM messages
    WHERE kind = 'question' AND to_user = ? AND status IN ('queued','pushed') ORDER BY created_at`),
  stale: db.prepare(`SELECT * FROM messages
    WHERE kind = 'question' AND status IN ('queued','pushed') AND created_at < ?`),
  dropOld: db.prepare(`UPDATE messages SET status = 'dropped', updated_at = ?
    WHERE kind IN ('answer','notice') AND status IN ('queued','pushed') AND created_at < ?`),
}

function createMessage(m) {
  const t = now()
  const row = {
    id: randomUUID(), hops: 0, reply_to: null, context: null,
    from_user: null, from_ws: null, from_sid: null, status: 'queued',
    ...m, created_at: t, updated_at: t,
  }
  q.insert.run(row.id, row.kind, row.from_user, row.from_ws, row.from_sid, row.to_user, row.to_ws,
    row.to_sid, row.reply_to, row.hops, row.body, row.context, row.status, row.created_at, row.updated_at)
  return row
}

// ─── Presence ────────────────────────────────────────────────────────────
/** sid -> { sid, user, workspace, listening, summary, ws, connectedAt } */
const sessions = new Map()
const addressOf = (s) => `${s.user}@${s.workspace}`

function wire(m) {
  return {
    type: 'message', id: m.id, kind: m.kind,
    from: m.from_user ? `${m.from_user}@${m.from_ws}` : 'broker',
    reply_to: m.reply_to, hops: m.hops, body: m.body, context: m.context,
  }
}

function push(m) {
  const s = sessions.get(m.to_sid)
  if (s && s.ws.readyState === s.ws.OPEN) s.ws.send(JSON.stringify(wire(m)))
}

// ─── Rate limit (메모리, 단일 인스턴스 기준) ─────────────────────────────
const hits = new Map()
function allow(key, limit) {
  const t = now()
  const arr = (hits.get(key) ?? []).filter((x) => t - x < LIMIT_WINDOW_MS)
  const ok = arr.length < limit
  if (ok) arr.push(t)
  hits.set(key, arr)
  return ok
}

// ─── HTTP 유틸 ───────────────────────────────────────────────────────────
class HttpError extends Error {
  constructor(status, message, extra = {}) { super(message); this.status = status; this.extra = extra }
}
const str = (v, name, max, { required = true } = {}) => {
  if (v == null || v === '') {
    if (required) throw new HttpError(400, `${name}이(가) 필요합니다`)
    return ''
  }
  if (typeof v !== 'string') throw new HttpError(400, `${name}은(는) 문자열이어야 합니다`)
  if (v.length > max) throw new HttpError(413, `${name}이(가) 너무 깁니다 (최대 ${max}자)`)
  return v
}

async function readJson(req) {
  let size = 0
  const chunks = []
  for await (const c of req) {
    size += c.length
    if (size > 64 * 1024) throw new HttpError(413, '요청 본문이 너무 큽니다')
    chunks.push(c)
  }
  if (!chunks.length) return {}
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { throw new HttpError(400, 'JSON 형식 오류') }
}

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' })
  res.end(JSON.stringify(body))
}

function mySession(req, user) {
  const sid = req.headers['x-peers-session']
  const s = sid && sessions.get(sid)
  if (!s || s.user !== user) throw new HttpError(409, '브로커에 연결된 세션이 없습니다. 잠시 후 다시 시도하세요.')
  return s
}

// ─── API 핸들러 ──────────────────────────────────────────────────────────
function listPeers(me) {
  const byAddr = new Map()
  for (const s of sessions.values()) {
    const a = addressOf(s)
    const cur = byAddr.get(a) ?? { address: a, user: s.user, workspace: s.workspace, listening: false, summary: '', sessions: 0, you: false }
    cur.sessions += 1
    cur.listening ||= s.listening
    if (s.summary && (s.listening || !cur.summary)) cur.summary = s.summary
    cur.you ||= s.sid === me.sid
    byAddr.set(a, cur)
  }
  return [...byAddr.values()].sort((x, y) => Number(y.listening) - Number(x.listening) || x.address.localeCompare(y.address))
}

function ask(me, body) {
  const to = str(body.to, 'to', 200).trim()
  const question = str(body.question, 'question', MAX_QUESTION)
  const context = str(body.context, 'context', MAX_CONTEXT, { required: false })

  const candidates = [...sessions.values()].filter((s) =>
    s.listening && s.sid !== me.sid && (to.includes('@') ? addressOf(s) === to : s.user === to))
  if (!candidates.length) {
    const available = listPeers(me).filter((p) => p.listening && !p.you).map((p) => p.address)
    throw new HttpError(404, `${to}: 지금 질문을 받을 수 있는 세션이 없습니다`, { available })
  }
  const addrs = [...new Set(candidates.map(addressOf))]
  if (addrs.length > 1) throw new HttpError(409, '대상이 여러 곳입니다. user@workspace 형식으로 지정하세요', { candidates: addrs })
  const target = candidates.sort((a, b) => b.connectedAt - a.connectedAt)[0]

  // 받은 질문을 처리하다가 다시 묻는 경우 hops가 늘어난다
  const open = q.maxOpenHops.get(me.sid)
  const hops = open?.h == null ? 0 : open.h + 1
  if (hops > MAX_HOPS) {
    throw new HttpError(422, `재질문 한도(${MAX_HOPS}) 초과: 받은 질문에는 알고 있는 범위에서 reply로 답하세요`)
  }
  // 나에게 질문한 세션에게 되묻는 핑퐁 방지
  if (q.openFrom.get(me.sid, target.sid)) {
    throw new HttpError(422, `${addressOf(target)}이(가) 보낸 질문이 열려 있습니다. 되묻지 말고 reply로 답하거나 확인 요청을 담아 reply 하세요`)
  }
  if (!allow(`u:${me.user}`, LIMIT_PER_USER) || !allow(`p:${me.user}>${target.user}`, LIMIT_PER_PAIR)) {
    throw new HttpError(429, '질문 빈도 제한에 걸렸습니다. 잠시 후 다시 시도하세요')
  }

  const msg = createMessage({
    kind: 'question', from_user: me.user, from_ws: me.workspace, from_sid: me.sid,
    to_user: target.user, to_ws: target.workspace, to_sid: target.sid,
    hops, body: question, context: context || null,
  })
  push(msg)
  log(`ask ${msg.id} ${addressOf(me)} -> ${addressOf(target)} hops=${hops}`)
  return { msg_id: msg.id, to: addressOf(target), expires_in_sec: QUESTION_TTL_MS / 1000 }
}

function reply(me, body) {
  const msgId = str(body.msg_id, 'msg_id', 100)
  const text = str(body.text, 'text', MAX_ANSWER)
  const question = q.byId.get(msgId)
  if (!question || question.kind !== 'question') throw new HttpError(404, '해당 질문이 없습니다')
  if (question.to_user !== me.user) throw new HttpError(403, '나에게 온 질문이 아닙니다')
  if (question.status === 'answered') throw new HttpError(409, '이미 답한 질문입니다')
  if (question.status === 'expired') throw new HttpError(410, '만료된 질문입니다. 질문자가 더 이상 기다리지 않습니다')

  const answer = createMessage({
    kind: 'answer', from_user: me.user, from_ws: me.workspace, from_sid: me.sid,
    to_user: question.from_user, to_ws: question.from_ws, to_sid: question.from_sid,
    reply_to: question.id, body: text,
  })
  q.setStatus.run('answered', now(), question.id)
  push(answer)
  log(`reply ${answer.id} for ${question.id}`)
  return { ok: true, delivered_live: sessions.has(question.from_sid) }
}

function setStatus(me, body) {
  if (body.listening !== undefined) me.listening = Boolean(body.listening)
  if (body.summary !== undefined) me.summary = str(body.summary, 'summary', MAX_SUMMARY, { required: false })
  return { address: addressOf(me), listening: me.listening, summary: me.summary }
}

function inbox(me) {
  const rows = q.inbox.all(me.user)
  for (const r of rows) q.setStatus.run('read', now(), r.id)
  return {
    messages: rows.map(wire),
    open_questions_to_me: q.openQuestionsTo.all(me.user).map(wire),
  }
}

const routes = {
  'GET /api/peers': (me) => ({ peers: listPeers(me) }),
  'POST /api/ask': ask,
  'POST /api/reply': reply,
  'POST /api/status': setStatus,
  'GET /api/inbox': inbox,
}

const server = http.createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://x').pathname
    if (path === '/healthz') return send(res, 200, { ok: true, sessions: sessions.size })
    const handler = routes[`${req.method} ${path}`]
    if (!handler) throw new HttpError(404, 'not found')
    const user = authenticate(req)
    if (!user) throw new HttpError(401, '인증 실패')
    const me = mySession(req, user)
    const body = req.method === 'POST' ? await readJson(req) : {}
    send(res, 200, handler(me, body))
  } catch (e) {
    if (e instanceof HttpError) return send(res, e.status, { error: e.message, ...e.extra })
    log('internal error', e)
    send(res, 500, { error: 'internal error' })
  }
})

// ─── WebSocket: 세션 연결과 푸쉬 ─────────────────────────────────────────
const wss = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024 })

server.on('upgrade', (req, socket, head) => {
  const path = new URL(req.url, 'http://x').pathname
  const user = authenticate(req)
  const sid = req.headers['x-peers-session']
  if (path !== '/stream' || !user || !/^[0-9a-f-]{36}$/.test(sid ?? '')) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n')
    return socket.destroy()
  }
  wss.handleUpgrade(req, socket, head, (ws) => onConnection(ws, req, user, sid))
})

function onConnection(ws, req, user, sid) {
  const workspace = /^[\w.-]{1,64}$/.test(req.headers['x-peers-workspace'] ?? '') ? req.headers['x-peers-workspace'] : 'unknown'
  const prev = sessions.get(sid)
  if (prev) prev.ws.close(4001, 'replaced')

  const s = { sid, user, workspace, listening: req.headers['x-peers-listen'] === '1', summary: '', ws, connectedAt: now(), alive: true }
  sessions.set(sid, s)
  log(`connect ${addressOf(s)} sid=${sid.slice(0, 8)} listening=${s.listening}`)

  // 1) 이 세션으로 보냈지만 ack 못 받은 메시지 재전송
  for (const m of q.unackedFor.all(sid)) push(m)
  // 2) 같은 user@workspace의 끊긴 이전 세션 앞으로 쌓인 답변을 새 세션으로 옮김
  for (const m of q.orphanInbox.all(user, workspace)) {
    if (m.to_sid !== sid && !sessions.has(m.to_sid)) {
      q.readdress.run(sid, now(), m.id)
      push({ ...m, to_sid: sid })
    }
  }

  ws.on('pong', () => { s.alive = true })
  ws.on('message', (raw) => {
    let m
    try { m = JSON.parse(String(raw)) } catch { return }
    if (m.type === 'ack' && typeof m.id === 'string') q.ack.run(now(), m.id, sid)
  })
  ws.on('close', () => {
    if (sessions.get(sid)?.ws === ws) sessions.delete(sid)
    log(`disconnect ${addressOf(s)} sid=${sid.slice(0, 8)}`)
  })
}

// 끊긴 연결 정리
setInterval(() => {
  for (const s of sessions.values()) {
    if (!s.alive) { s.ws.terminate(); continue }
    s.alive = false
    s.ws.ping()
  }
}, 30000).unref()

// ─── 만료 처리 ───────────────────────────────────────────────────────────
setInterval(() => {
  const t = now()
  for (const question of q.stale.all(t - QUESTION_TTL_MS)) {
    q.setStatus.run('expired', t, question.id)
    const notice = createMessage({
      kind: 'notice', to_user: question.from_user, to_ws: question.from_ws, to_sid: question.from_sid,
      reply_to: question.id,
      body: `${question.to_user}@${question.to_ws}에게 보낸 질문이 ${Math.round(QUESTION_TTL_MS / 60000)}분 안에 답을 받지 못해 만료되었습니다. 질문: ${question.body.slice(0, 200)}`,
    })
    push(notice)
    log(`expired ${question.id}`)
  }
  q.dropOld.run(t, t - INBOX_TTL_MS)
}, SWEEP_MS).unref()

server.listen(PORT, HOST, () => log(`claude-peers broker listening on ${HOST}:${PORT}`))
