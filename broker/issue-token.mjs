#!/usr/bin/env node
// 사용법:
//   node issue-token.mjs <user>            새 토큰 발급 (토큰은 한 번만 출력, 서버에는 해시만 저장)
//   node issue-token.mjs --revoke <user>   해당 사용자의 모든 토큰 폐기
import { randomBytes, createHash } from 'node:crypto'
import { readFileSync, writeFileSync, existsSync } from 'node:fs'

const TOKENS_PATH = process.env.PEERS_TOKENS ?? './tokens.json'
const tokens = existsSync(TOKENS_PATH) ? JSON.parse(readFileSync(TOKENS_PATH, 'utf8')) : {}
const save = () => writeFileSync(TOKENS_PATH, JSON.stringify(tokens, null, 2), { mode: 0o600 })

const [a, b] = process.argv.slice(2)

if (a === '--revoke' && b) {
  let n = 0
  for (const entry of Object.values(tokens)) {
    if (entry.user === b && !entry.revoked) { entry.revoked = new Date().toISOString(); n++ }
  }
  save()
  console.error(`${b}: 토큰 ${n}개 폐기`)
} else if (a && /^[a-z0-9._-]{1,40}$/.test(a)) {
  const token = 'pk_' + randomBytes(24).toString('base64url')
  tokens[createHash('sha256').update(token).digest('hex')] = { user: a, created: new Date().toISOString() }
  save()
  console.log(token)
} else {
  console.error('usage: node issue-token.mjs <user>  |  node issue-token.mjs --revoke <user>')
  console.error('user는 소문자, 숫자, . _ - 만 사용 (예: 사내 계정 ID)')
  process.exit(1)
}
