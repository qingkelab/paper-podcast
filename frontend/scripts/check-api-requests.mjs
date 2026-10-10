/**
 * 前端纯逻辑的极小自检：**接口请求的形状**（node，无第三方依赖）。
 *
 * 为什么有这个东西：本周抓到一个只有真点一次才看得见的 bug ——
 * `updateCover` 的 fetch 少写了 `Content-Type: application/json`，
 * 于是 body 以 `text/plain` 发出去，FastAPI 只认 application/json，回的是：
 *
 *   {"type":"model_attributes_type","loc":["body"],
 *    "msg":"Input should be a valid dictionary or object to extract fields from",
 *    "input":"{\"headline\":\"…\"}"}
 *
 * 那个 `input` 回显的是**整段 JSON 字符串**，看着像前端双重序列化了 ——
 * 实际是缺一个头（实测：同一个 JSON 对象，带头 200、不带头报这个错）。
 * 顺着找一遍发现同类请求里 albums/batch 都带了，只有这一处漏了。
 *
 * 这类错误没有类型能挡（`RequestInit` 不强制 Content-Type），也没有测试框架能跑 fetch，
 * 但它是一个**纯静态事实**：带 `body: JSON.stringify(...)` 的请求必须有这个头。
 * 所以直接扫源码 —— 比造假 fetch 更能挡住回归，而且快。
 *
 *   cd frontend && pnpm test:units
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const REAL_TS = join(here, '..', 'src', 'api', 'real.ts')

/**
 * 找出所有「JSON 字符串 body 却没有 Content-Type」的请求。
 *
 * 做法：从 `body: JSON.stringify(` 那行往上找到这条请求的起点（`request<…>(` 或
 * `{ method: '…'`），把这些行合起来判 —— 这样 `headers` 写在 body 前面或后面都认得出。
 * `FormData` 的请求**必须不带**这个头（浏览器要自己加 multipart boundary），所以跳过。
 */
function findOffenders(source) {
  const lines = source.split('\n')
  const offenders = []
  for (let i = 0; i < lines.length; i += 1) {
    if (!lines[i].includes('body: JSON.stringify(')) continue

    let start = i
    while (start > 0 && !/request(<|\()|method:\s*'/.test(lines[start])) start -= 1
    let end = i
    while (end < lines.length - 1 && !/^\s{0,4}\}\s*,?\s*$/.test(lines[end])) end += 1

    const chunk = lines.slice(start, end + 1).join('\n')
    if (/body:\s*(form|formData)\b/.test(chunk)) continue
    if (!/Content-Type['"]?\s*:\s*['"]application\/json/.test(chunk)) {
      offenders.push(`第 ${i + 1} 行：${lines[i].trim()}`)
    }
  }
  return offenders
}

// --- 真实源码必须干净 ------------------------------------------------------

const source = readFileSync(REAL_TS, 'utf8')
const offenders = findOffenders(source)
assert.deepEqual(
  offenders,
  [],
  '这些 JSON 请求缺 Content-Type: application/json —— 后端会回「Input should be a ' +
    `valid dictionary…」：\n   ${offenders.join('\n   ')}`,
)

// --- 自检：把那个头抠掉之后必须**报出来**（否则这条检查本身是坏的） ----------

const mutated = source.replace(
  /(\n\s*)headers:\s*\{\s*'Content-Type':\s*'application\/json'\s*\},(\n\s*body:\s*JSON\.stringify\(body\),)/,
  '$2',
)
assert.notEqual(mutated, source, '自检前提变了：源码里已经找不到那个头，请更新这条自检')
assert.ok(
  findOffenders(mutated).length > 0,
  '把 Content-Type 抠掉之后这条检查没报错 —— 检查本身是坏的，修它',
)

console.log('api-request-shape: all assertions passed')
