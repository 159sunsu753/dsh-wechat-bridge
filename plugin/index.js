/**
 * Host half of @local/dsh-wechat-bridge.
 *
 * A loopback-only HTTP adapter inside the DSH host process. An external
 * "WeChat bridge" daemon injects messages into one fixed Session through
 * `sessionController.prompt` and reads that Session's events through a
 * long-poll ring buffer. The plugin performs no WeChat I/O and no business
 * decisions.
 *
 * Endpoints (see 工具\wechat-bridge\DEV-INTERFACES.md §5):
 *   GET  /health                 no auth, loopback only
 *   GET  /status                 Bearer
 *   POST /inbound                Bearer  {requestId,mode,content,clientTimeZone}
 *   GET  /events?since&timeout   Bearer  long poll
 *   GET  /attachment/:id         Bearer
 *
 * State files under %USERPROFILE%\.dsh\dsh-wechat-bridge\:
 *   token        32-byte CSPRNG hex, mode 0600 where supported
 *   runtime.json {pid,origin,pathPrefix,sessionId,sessionTitle,startedAt}
 */
import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { promises as fsp } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Readable } from 'node:stream'

export const name = 'dsh-wechat-bridge'
export const inject = ['webServer', 'sessionController', 'fileUploads']

const RING_MAX = 500
const DEFAULT_PREFIX = '/dsh-wechat-bridge'
const DEFAULT_SESSION_TITLE = 'WeChat Bridge'
const DEFAULT_SESSION_ID = 'session-wechat-bridge'
const DEFAULT_CWD = process.env.DSH_WECHAT_CWD || process.cwd()
const DEFAULT_TIME_ZONE = 'Asia/Shanghai'
const DEFAULT_MAX_BODY = 32 * 1024 * 1024
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])
const LOOPBACK_ADDRESSES = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1'])

class BridgeError extends Error {
  constructor(status, code, message, details = {}) {
    super(message)
    this.name = 'BridgeError'
    this.status = status
    this.code = code
    this.details = details
  }
}

function safeLog(...args) {
  try {
    console.info('[dsh-wechat-bridge]', ...args)
  } catch {
    /* logging must never break the host */
  }
}

function sleep(ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms)
    if (typeof timer.unref === 'function') timer.unref()
  })
}

function normalizePrefix(value) {
  const raw = typeof value === 'string' && value.trim() ? value.trim() : DEFAULT_PREFIX
  if (!raw.startsWith('/')) return `/${raw}`
  return raw.length > 1 ? raw.replace(/\/+$/, '') : raw
}

function hostnameOf(hostHeader) {
  const host = String(hostHeader || '').trim().toLowerCase()
  if (!host) return ''
  if (host.startsWith('[')) {
    const end = host.indexOf(']')
    return end === -1 ? host : host.slice(0, end + 1)
  }
  return host.split(':')[0]
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left ?? ''), 'utf8')
  const b = Buffer.from(String(right ?? ''), 'utf8')
  if (a.length === 0 || a.length !== b.length) return false
  try {
    return timingSafeEqual(a, b)
  } catch {
    return false
  }
}

function mediaTypeOfBase64(data) {
  try {
    const head = Buffer.from(data.slice(0, 32), 'base64')
    if (head.length >= 8 && head[0] === 0x89 && head[1] === 0x50) return 'image/png'
    if (head.length >= 3 && head[0] === 0xff && head[1] === 0xd8) return 'image/jpeg'
    if (head.length >= 3 && head[0] === 0x47 && head[1] === 0x49 && head[2] === 0x46) return 'image/gif'
    if (head.length >= 12 && head.slice(0, 4).toString('ascii') === 'RIFF' && head.slice(8, 12).toString('ascii') === 'WEBP') return 'image/webp'
  } catch {
    /* fall through to png */
  }
  return 'image/png'
}

export function apply(ctx, config = {}) {
  const prefix = normalizePrefix(config.pathPrefix)
  const sessionTitle = typeof config.sessionTitle === 'string' && config.sessionTitle.trim()
    ? config.sessionTitle.trim()
    : DEFAULT_SESSION_TITLE
  const cwd = typeof config.cwd === 'string' && config.cwd.trim() ? config.cwd.trim() : DEFAULT_CWD
  const defaultMode = config.defaultMode === 'steer' ? 'steer' : 'queue'
  const clientTimeZone = typeof config.clientTimeZone === 'string' && config.clientTimeZone.trim()
    ? config.clientTimeZone.trim()
    : DEFAULT_TIME_ZONE
  const maxBodyBytes = Number.isFinite(Number(config.maxBodyBytes)) && Number(config.maxBodyBytes) > 0
    ? Number(config.maxBodyBytes)
    : DEFAULT_MAX_BODY
  const eventTimeoutMs = Number.isFinite(Number(config.eventTimeoutMs)) && Number(config.eventTimeoutMs) > 0
    ? Number(config.eventTimeoutMs)
    : 25000
  const configuredSessionId = typeof config.sessionId === 'string' && config.sessionId.trim()
    ? config.sessionId.trim()
    : ''

  const stateDir = typeof config.stateDir === 'string' && config.stateDir.trim()
    ? config.stateDir.trim()
    : path.join(os.homedir(), '.dsh', 'dsh-wechat-bridge')
  const tokenFile = typeof config.tokenFile === 'string' && config.tokenFile.trim()
    ? config.tokenFile.trim()
    : path.join(stateDir, 'token')
  const runtimeFile = typeof config.runtimeFile === 'string' && config.runtimeFile.trim()
    ? config.runtimeFile.trim()
    : path.join(stateDir, 'runtime.json')

  const state = {
    disposed: false,
    startedAt: Date.now(),
    sessionId: configuredSessionId || null,
    title: sessionTitle,
    origin: null,
    ring: [],
    baseSeq: 0,
    waiters: new Set(),
    ready: null,
    readyError: null,
    token: null,
    tokenPromise: null,
    runtimeTimer: null,
    runtimeTries: 0,
    runtimeWritten: false,
  }

  const controller = new AbortController()

  async function hardenTokenFile(file) {
    if (process.platform === 'win32') return
    try {
      await fsp.chmod(file, 0o600)
    } catch {
      /* best effort */
    }
  }

  async function loadOrCreateToken() {
    if (typeof config.token === 'string' && config.token.trim()) return config.token.trim()
    try {
      const existing = (await fsp.readFile(tokenFile, 'utf8')).trim()
      if (/^[0-9a-f]{64}$/i.test(existing)) {
        await hardenTokenFile(tokenFile)
        return existing.toLowerCase()
      }
    } catch {
      /* missing or unreadable -> mint */
    }
    const token = randomBytes(32).toString('hex')
    await fsp.mkdir(path.dirname(tokenFile), { recursive: true })
    await fsp.writeFile(tokenFile, `${token}\n`, { mode: 0o600 })
    await hardenTokenFile(tokenFile)
    return token
  }

  state.tokenPromise = loadOrCreateToken()
    .then((token) => {
      state.token = token
      return token
    })
    .catch((error) => {
      safeLog('token initialization failed:', error?.message ?? error)
      throw error
    })
  state.tokenPromise.catch(() => {})

  function currentOrigin() {
    try {
      const origin = ctx.webServer.getOrigin()
      if (typeof origin === 'string' && origin) state.origin = origin.replace(/\/+$/, '')
    } catch {
      /* listener not bound yet */
    }
    return state.origin
  }

  async function writeFileAtomic(file, text) {
    await fsp.mkdir(path.dirname(file), { recursive: true })
    const tmp = `${file}.${process.pid}.${Date.now()}.tmp`
    await fsp.writeFile(tmp, text, 'utf8')
    await fsp.rename(tmp, file)
  }

  async function persistRuntime() {
    if (state.disposed) return
    const origin = currentOrigin()
    if (!origin) {
      scheduleRuntimeRetry()
      return
    }
    const doc = {
      pid: process.pid,
      origin,
      pathPrefix: prefix,
      sessionId: state.sessionId,
      title: state.title,
      tokenFile,
      startedAt: state.startedAt,
      updatedAt: Date.now(),
      ...(state.readyError ? { error: String(state.readyError?.message ?? state.readyError) } : {}),
    }
    try {
      await writeFileAtomic(runtimeFile, `${JSON.stringify(doc, null, 2)}\n`)
      state.runtimeWritten = true
    } catch (error) {
      safeLog('runtime.json write failed:', error?.message ?? error)
      scheduleRuntimeRetry()
    }
  }

  function scheduleRuntimeRetry() {
    if (state.disposed || state.runtimeTimer) return
    if (state.runtimeTries >= 120) return
    state.runtimeTries += 1
    state.runtimeTimer = setTimeout(() => {
      state.runtimeTimer = null
      void persistRuntime()
    }, 1000)
    if (typeof state.runtimeTimer.unref === 'function') state.runtimeTimer.unref()
  }

  function titleOfSummary(summary) {
    const value = summary?.projections?.values?.title
    return typeof value === 'string' && value.trim() ? value.trim() : null
  }

  async function listSessions() {
    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(), 15000)
    if (typeof timer.unref === 'function') timer.unref()
    try {
      const value = await ctx.sessionController.list({}, ac.signal)
      return Array.isArray(value?.items) ? value.items : []
    } finally {
      clearTimeout(timer)
    }
  }

  async function summaryFor(sessionId) {
    try {
      const items = await listSessions()
      return items.find((item) => item && item.sessionId === sessionId) ?? null
    } catch (error) {
      safeLog('session list failed:', error?.message ?? error)
      return null
    }
  }

  async function ensureSession() {
    let sessionId = configuredSessionId
    let created = false

    if (!sessionId) {
      let existing = null
      try {
        const items = await listSessions()
        existing = items.find((item) => item && item.sessionId === DEFAULT_SESSION_ID) ?? null
      } catch (error) {
        safeLog('session reuse probe failed:', error?.message ?? error)
      }
      if (existing) {
        sessionId = String(existing.sessionId)
        safeLog('reusing fixed session', sessionId)
      } else {
        try {
          const value = await ctx.sessionController.create({ sessionId: DEFAULT_SESSION_ID, cwd })
          sessionId = String(value.sessionId)
          created = true
          safeLog('created fixed session', sessionId)
        } catch (error) {
          safeLog('create with fixed id failed, letting DSH mint one:', error?.message ?? error)
          const value = await ctx.sessionController.create({ cwd })
          sessionId = String(value.sessionId)
          created = true
          safeLog('created generated session', sessionId)
        }
      }
    }

    state.sessionId = sessionId

    // Seed the ring cursor with the Session's committed sequence so /events
    // starts from a meaningful "since" value rather than a misleading 0.
    try {
      const projections = await ctx.sessionController.projections({ sessionId })
      const asOf = Number(projections?.asOfSeq)
      if (Number.isFinite(asOf) && asOf > state.baseSeq) state.baseSeq = asOf
      const projectedTitle = projections?.values?.title
      if (typeof projectedTitle === 'string' && projectedTitle.trim()) state.title = projectedTitle.trim()
    } catch (error) {
      safeLog('session projections failed:', error?.message ?? error)
    }

    if (created || state.title !== sessionTitle) {
      try {
        const renamed = await ctx.sessionController.rename({ sessionId, title: sessionTitle })
        state.title = typeof renamed?.title === 'string' && renamed.title.trim() ? renamed.title.trim() : sessionTitle
        safeLog('session titled', state.title)
      } catch (error) {
        safeLog('rename failed:', error?.message ?? error)
      }
    }

    await persistRuntime()
  }

  state.ready = ensureSession()
    .catch((error) => {
      state.readyError = error
      state.ready = Promise.resolve()
      safeLog('session initialization failed:', error?.message ?? error)
      return persistRuntime()
    })
  state.ready.catch(() => {})

  async function waitReady() {
    if (state.sessionId) return state.sessionId
    try {
      await Promise.race([state.ready, sleep(20000)])
    } catch {
      /* handled below */
    }
    if (state.sessionId) return state.sessionId
    throw new BridgeError(503, 'BRIDGE_DSH_UNAVAILABLE', 'fixed session is not ready', {
      reason: String(state.readyError?.message ?? 'session initialization pending'),
    })
  }

  function lastSeq() {
    let seq = state.baseSeq
    const tail = state.ring[state.ring.length - 1]
    if (tail && Number(tail.seq) > seq) seq = Number(tail.seq)
    return seq
  }

  function pushEvent(session, event) {
    if (state.disposed || !event || typeof event.seq !== 'number') return
    if (!state.sessionId || session?.id !== state.sessionId) return
    const wire = { seq: event.seq, type: event.type, time: event.time, data: event.data }
    if (event.ignorable === true) wire.ignorable = true
    state.ring.push(wire)
    if (state.ring.length > RING_MAX) state.ring.splice(0, state.ring.length - RING_MAX)
    const waiters = [...state.waiters]
    state.waiters.clear()
    for (const wake of waiters) wake()
  }

  function waitForChange(timeoutMs, req, res) {
    return new Promise((resolve) => {
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        state.waiters.delete(finish)
        req.off('close', finish)
        res.off('close', finish)
        resolve()
      }
      const timer = setTimeout(finish, Math.max(0, timeoutMs))
      if (typeof timer.unref === 'function') timer.unref()
      state.waiters.add(finish)
      req.once('close', finish)
      res.once('close', finish)
    })
  }

  function isAborted(req, res) {
    return Boolean(req.destroyed || res.destroyed || res.writableEnded)
  }

  function loopbackOk(req) {
    const remote = req.socket?.remoteAddress ?? ''
    if (!LOOPBACK_ADDRESSES.has(remote)) return false
    const host = String(req.headers?.host ?? '').trim()
    if (!host) return false
    if (!LOOPBACK_HOSTS.has(hostnameOf(host))) return false
    if (String(req.headers['sec-fetch-site'] ?? '').toLowerCase() === 'cross-site') return false
    const origin = req.headers?.origin
    if (origin) {
      try {
        const parsed = new URL(String(origin))
        if (!LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase())) return false
        const headerHost = host.toLowerCase()
        if (parsed.host.toLowerCase() !== headerHost && parsed.hostname.toLowerCase() !== hostnameOf(headerHost)) return false
      } catch {
        return false
      }
    }
    return true
  }

  function authorized(req) {
    const header = String(req.headers?.authorization ?? '')
    if (!header.toLowerCase().startsWith('bearer ')) return false
    return safeEqual(header.slice(7).trim(), state.token)
  }

  function sendJson(res, status, payload) {
    if (res.writableEnded || res.destroyed) return
    const body = JSON.stringify(payload)
    try {
      res.writeHead(status, {
        'content-type': 'application/json; charset=utf-8',
        'cache-control': 'no-store',
        'content-length': Buffer.byteLength(body),
      })
      res.end(body)
    } catch (error) {
      safeLog('response write failed:', error?.message ?? error)
      try {
        res.end()
      } catch {
        /* already gone */
      }
    }
  }

  function sendError(res, error) {
    if (error instanceof BridgeError) {
      sendJson(res, error.status, { ok: false, error: { code: error.code, message: error.message, details: error.details } })
      return
    }
    const code = typeof error?.code === 'string' ? error.code : null
    const table = {
      'gateway/bad-request': 400,
      'session/not-found': 404,
      'session/agent-busy': 409,
      'session/writer-held': 409,
      'session/attachment-invalid': 400,
      'session/invalid-time-zone': 400,
      'gateway/cancelled': 499,
      'gateway/internal': 502,
    }
    if (code && table[code]) {
      sendJson(res, table[code], {
        ok: false,
        error: {
          code,
          message: String(error?.message ?? code),
          details: error?.details && typeof error.details === 'object' ? error.details : {},
        },
      })
      return
    }
    sendJson(res, 500, {
      ok: false,
      error: {
        code: 'BRIDGE_INTERNAL',
        message: String(error?.message ?? error),
        details: {},
      },
    })
  }

  async function readJsonBody(req) {
    const chunks = []
    let total = 0
    for await (const chunk of req) {
      total += chunk.length
      if (total > maxBodyBytes) {
        throw new BridgeError(413, 'BRIDGE_PAYLOAD_TOO_LARGE', `request body exceeds ${maxBodyBytes} bytes`, { maxBodyBytes })
      }
      chunks.push(chunk)
    }
    const text = Buffer.concat(chunks).toString('utf8')
    if (!text.trim()) return {}
    try {
      return JSON.parse(text)
    } catch {
      throw new BridgeError(400, 'BRIDGE_BAD_REQUEST', 'request body must be valid JSON')
    }
  }

  function stripPrefix(pathname) {
    if (prefix !== '/' && (pathname === prefix || pathname.startsWith(`${prefix}/`))) {
      const rest = pathname.slice(prefix.length)
      return rest === '' ? '/' : rest
    }
    return pathname
  }

  async function handleHealth(req, res) {
    await persistRuntime().catch(() => {})
    sendJson(res, 200, {
      ok: true,
      pid: process.pid,
      origin: currentOrigin() ?? null,
      sessionId: state.sessionId,
      sessionTitle: state.title,
      pathPrefix: prefix,
      startedAt: state.startedAt,
      ready: Boolean(state.sessionId) && !state.readyError,
      ...(state.readyError ? { error: String(state.readyError?.message ?? state.readyError) } : {}),
    })
  }

  async function handleStatus(req, res) {
    const sessionId = await waitReady()
    const summary = await summaryFor(sessionId)
    const title = titleOfSummary(summary) ?? state.title
    sendJson(res, 200, {
      ok: true,
      sessionId,
      title,
      running: Boolean(summary?.running),
      lastSeq: lastSeq(),
      pid: process.pid,
      origin: currentOrigin() ?? null,
      startedAt: state.startedAt,
    })
  }

  async function handleInbound(req, res) {
    const sessionId = await waitReady()
    const body = await readJsonBody(req)
    const requestId = typeof body.requestId === 'string' && body.requestId.trim()
      ? body.requestId.trim()
      : `wx-${randomUUID()}`
    const mode = body.mode === 'steer' || body.mode === 'queue' ? body.mode : defaultMode
    const zone = typeof body.clientTimeZone === 'string' && body.clientTimeZone.trim()
      ? body.clientTimeZone.trim()
      : clientTimeZone

    const incoming = Array.isArray(body.content) ? body.content : []
    const content = []
    for (const part of incoming) {
      if (!part || typeof part !== 'object') continue
      if (part.type === 'text') {
        const text = typeof part.text === 'string' ? part.text : ''
        if (text.trim()) content.push({ type: 'text', text })
      } else if (part.type === 'image') {
        const data = typeof part.data === 'string' ? part.data.trim() : ''
        if (!data) throw new BridgeError(400, 'BRIDGE_BAD_REQUEST', 'image part requires base64 data')
        const mediaType = IMAGE_TYPES.has(part.mediaType) ? part.mediaType : mediaTypeOfBase64(data)
        const image = { type: 'image', mediaType, data }
        if (typeof part.name === 'string' && part.name.trim()) image.name = part.name.trim()
        content.push(image)
      } else if (part.type === 'file') {
        const raw = typeof part.data === 'string' ? part.data.trim() : ''
        if (!raw) throw new BridgeError(400, 'BRIDGE_BAD_REQUEST', 'file part requires base64 data')
        const bytes = Buffer.from(raw, 'base64')
        const uploadName = typeof part.name === 'string' && part.name.trim() ? part.name.trim() : 'upload.bin'
        const upload = await ctx.fileUploads.uploadStream({
          sessionId,
          data: Readable.from([bytes]),
          name: uploadName,
          signal: controller.signal,
        })
        content.push({ type: 'file', receiptId: upload.receiptId })
      }
    }
    if (content.length === 0) {
      throw new BridgeError(400, 'BRIDGE_BAD_REQUEST', 'content must contain at least one non-empty text, image or file part')
    }

    const signal = AbortSignal.timeout(30000)
    await ctx.sessionController.prompt({ requestId, sessionId, mode, content, clientTimeZone: zone }, signal)

    const summary = await summaryFor(sessionId)
    sendJson(res, 200, {
      ok: true,
      requestId,
      sessionId,
      accepted: true,
      mode,
      running: Boolean(summary?.running),
      queued: Boolean(summary?.running) && mode === 'queue',
      lastSeq: lastSeq(),
    })
  }

  async function handleEvents(req, res, url) {
    const sessionId = await waitReady()
    const sinceParsed = Number.parseInt(url.searchParams.get('since') ?? '0', 10)
    const since = Number.isFinite(sinceParsed) && sinceParsed > 0 ? sinceParsed : 0
    const timeoutParsed = Number.parseInt(url.searchParams.get('timeout') ?? '', 10)
    const timeout = Number.isFinite(timeoutParsed) && timeoutParsed >= 0
      ? Math.min(timeoutParsed, 60000)
      : Math.min(eventTimeoutMs, 60000)
    const deadline = Date.now() + timeout

    for (;;) {
      if (isAborted(req, res)) return
      const last = lastSeq()
      const oldest = state.ring.length > 0 ? Number(state.ring[0].seq) : last + 1
      const batch = state.ring.filter((event) => Number(event.seq) > since)
      const gap = since < oldest - 1 || since > last
      if (batch.length > 0 || gap) {
        sendJson(res, 200, {
          ok: true,
          sessionId,
          events: batch,
          lastSeq: last,
          ...(gap ? { resync: true, oldestSeq: oldest } : {}),
        })
        return
      }
      const remaining = deadline - Date.now()
      if (remaining <= 0) {
        sendJson(res, 200, { ok: true, sessionId, events: [], lastSeq: last })
        return
      }
      await waitForChange(Math.min(remaining, 1000), req, res)
    }
  }

  async function handleAttachment(req, res, attachmentId) {
    const sessionId = await waitReady()
    if (!attachmentId) throw new BridgeError(400, 'BRIDGE_BAD_REQUEST', 'attachment id is required')
    let id = attachmentId
    try {
      id = decodeURIComponent(attachmentId)
    } catch {
      /* keep raw */
    }
    const value = await ctx.sessionController.attachment({ sessionId, attachmentId: id })
    sendJson(res, 200, {
      ok: true,
      sessionId,
      attachment: value?.attachment ?? null,
      data: value?.data ?? null,
    })
  }

  async function handler(req, res) {
    try {
      if (!loopbackOk(req)) {
        sendJson(res, 403, { ok: false, error: { code: 'BRIDGE_FORBIDDEN', message: 'loopback origin required', details: {} } })
        return
      }
      const url = new URL(req.url || '/', 'http://127.0.0.1')
      const sub = stripPrefix(url.pathname)

      if (req.method === 'GET' && sub === '/health') {
        await handleHealth(req, res)
        return
      }

      if (!state.token) {
        try {
          await state.tokenPromise
        } catch {
          /* surfaced as 503 below */
        }
      }
      if (!authorized(req)) {
        sendJson(res, 401, { ok: false, error: { code: 'BRIDGE_UNAUTHORIZED', message: 'missing or invalid bearer token', details: {} } })
        return
      }
      if (!state.token) {
        throw new BridgeError(503, 'BRIDGE_DSH_UNAVAILABLE', 'bridge token is not available')
      }

      if (req.method === 'GET' && sub === '/status') {
        await handleStatus(req, res)
        return
      }
      if (req.method === 'POST' && sub === '/inbound') {
        await handleInbound(req, res)
        return
      }
      if (req.method === 'GET' && sub === '/events') {
        await handleEvents(req, res, url)
        return
      }
      if (req.method === 'GET' && sub.startsWith('/attachment/')) {
        await handleAttachment(req, res, sub.slice('/attachment/'.length))
        return
      }
      sendJson(res, 404, { ok: false, error: { code: 'BRIDGE_NOT_FOUND', message: `no route for ${req.method} ${sub}`, details: {} } })
    } catch (error) {
      if (error instanceof BridgeError && error.status === 413) {
        try {
          req.resume()
        } catch {
          /* ignore */
        }
      }
      sendError(res, error)
    }
  }

  ctx.effect(() => ctx.on('session/event', (session, event) => {
    try {
      pushEvent(session, event)
    } catch (error) {
      safeLog('event handler failed:', error?.message ?? error)
    }
  }))

  ctx.effect(() => ctx.webServer.register({ kind: 'prefix', path: prefix, handler }))

  ctx.effect(() => () => {
    state.disposed = true
    if (state.runtimeTimer) {
      clearTimeout(state.runtimeTimer)
      state.runtimeTimer = null
    }
    try {
      controller.abort()
    } catch {
      /* ignore */
    }
    for (const wake of [...state.waiters]) wake()
    state.waiters.clear()
    void fsp.rm(runtimeFile, { force: true }).catch(() => {})
  })

  safeLog(`listening on ${prefix}; state dir ${stateDir}`)
}
