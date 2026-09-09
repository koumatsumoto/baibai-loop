/** Research工程で既存viewとmarket L1を認証済みの利用者へ見せる。 */
import { lakeResponse } from './lake'

const API_HEADERS = {
  'Cache-Control': 'no-store',
  'Content-Type': 'application/json; charset=utf-8',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
} as const
const HSTS_HEADER = API_HEADERS['Strict-Transport-Security']

const TICKER_PATTERN = /^[0-9A-Z]{4}$/
const SCREENING_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/
// Mirrors _MACRO_CONTEXT_ID_FORMAT in baibai_web.materialize; excludes
// path separators so the id maps to exactly one serving key.
const MACRO_CONTEXT_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/
const MACRO_SERIES_ID_PATTERN = /^[a-z0-9._-]{1,128}$/
const ASSESSMENT_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/

type RouteResult =
  | { kind: 'health' }
  | { kind: 'screening-history-index' }
  | { kind: 'view'; key: string }
  | { kind: 'error'; status: 404 | 422; detail: string }

export async function handleRequest(request: Request, env: Env): Promise<Response> {
  try {
    return await routeRequest(request, env)
  } catch {
    // Exception messages can contain credential-bearing URLs.
    console.error('request failed')
    return jsonResponse({ detail: 'internal server error' }, 500)
  }
}

async function routeRequest(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url)
  if (url.protocol !== 'https:') {
    url.protocol = 'https:'
    return new Response(null, {
      status: 308,
      headers: {
        'Cache-Control': 'no-store',
        Location: url.toString(),
      },
    })
  }
  if (url.pathname !== '/api' && !url.pathname.startsWith('/api/')) {
    const asset = await env.ASSETS.fetch(request)
    const headers = new Headers(asset.headers)
    headers.set('Strict-Transport-Security', HSTS_HEADER)
    if (url.searchParams.has('share')) {
      headers.set('Cache-Control', 'no-store')
      headers.set('Referrer-Policy', 'no-referrer')
      headers.set('X-Robots-Tag', 'noindex, nofollow, noarchive')
    }
    return new Response(asset.body, {
      status: asset.status,
      statusText: asset.statusText,
      headers,
    })
  }

  if (url.searchParams.getAll('share').length > 1) {
    return jsonResponse({ detail: 'duplicate share parameter' }, 400)
  }
  if (!(await isAuthorized(request, url, env))) {
    return jsonResponse({ detail: 'unauthorized' }, 401, {
      'WWW-Authenticate': 'Bearer',
    })
  }
  if (request.method !== 'GET') {
    return jsonResponse({ detail: 'method not allowed' }, 405, { Allow: 'GET' })
  }

  if (url.pathname === '/api/lake/current' || url.pathname === '/api/lake/object') {
    return lakeResponse(url, env, API_HEADERS)
  }

  const route = resolveRoute(url)
  if (route.kind === 'error') {
    return jsonResponse({ detail: route.detail }, route.status)
  }
  if (route.kind === 'health') {
    return jsonResponse({ status: 'ok' }, 200)
  }
  if (route.kind === 'screening-history-index') {
    return screeningHistoryIndex(env)
  }

  const object = await env.BAIBAI_SERVING.get(route.key)
  if (object === null) {
    return jsonResponse({ detail: 'view not found' }, 404)
  }
  return new Response(object.body, { status: 200, headers: API_HEADERS })
}

function resolveRoute(url: URL): RouteResult {
  switch (url.pathname) {
    case '/api/health':
      return { kind: 'health' }
    case '/api/dashboard':
      return view('dashboard.json')
    case '/api/daily-delta':
      return view('daily-delta.json')
    case '/api/screening/latest':
      return view('screening_latest.json')
    case '/api/screening/history':
      return { kind: 'screening-history-index' }
    case '/api/operations':
      return view('operations.json')
    case '/api/tasks':
      return view('tasks.json')
    case '/api/meta':
      return view('meta.json')
    case '/api/macro':
      return view('macro.json')
    default:
      return (
        resolveScreeningHistory(url.pathname) ??
        resolveMacroSeries(url.pathname) ??
        resolveMacroContext(url.pathname) ??
        resolveAssessment(url.pathname) ??
        resolveSecurity(url.pathname)
      )
  }
}

function resolveMacroSeries(pathname: string): RouteResult | null {
  const prefix = '/api/macro/series/'
  if (!pathname.startsWith(prefix)) {
    return null
  }
  const seriesId = pathname.slice(prefix.length)
  if (!MACRO_SERIES_ID_PATTERN.test(seriesId)) {
    return { kind: 'error', status: 404, detail: 'unknown macro series' }
  }
  return view(`macro-series--${seriesId}.json`)
}

function resolveScreeningHistory(pathname: string): RouteResult | null {
  const prefix = '/api/screening/history/'
  if (!pathname.startsWith(prefix)) {
    return null
  }
  const asOf = pathname.slice(prefix.length)
  if (!SCREENING_DATE_PATTERN.test(asOf)) {
    return { kind: 'error', status: 404, detail: 'unknown screening history' }
  }
  return { kind: 'view', key: `history/candidate-views/${asOf}.json` }
}

async function screeningHistoryIndex(env: Env): Promise<Response> {
  const prefix = 'history/candidate-views/'
  let page = await env.BAIBAI_SERVING.list({ prefix, limit: 64 })
  const objects = [...page.objects]
  while (page.truncated) {
    page = await env.BAIBAI_SERVING.list({ prefix, limit: 64, cursor: page.cursor })
    objects.push(...page.objects)
  }

  const dates = objects
    .map((object) => object.key.slice(prefix.length, -'.json'.length))
    .filter((value) => SCREENING_DATE_PATTERN.test(value))
    .sort((left, right) => right.localeCompare(left))
  return jsonResponse({ dates }, 200)
}

function resolveMacroContext(pathname: string): RouteResult | null {
  const prefix = '/api/macro/context/'
  if (!pathname.startsWith(prefix)) {
    return null
  }
  const contextId = pathname.slice(prefix.length)
  if (!MACRO_CONTEXT_ID_PATTERN.test(contextId)) {
    return { kind: 'error', status: 404, detail: 'unknown macro context' }
  }
  return view(`macro-context--${contextId}.json`)
}

function resolveAssessment(pathname: string): RouteResult | null {
  const prefix = '/api/capital-allocation-assessments/'
  if (!pathname.startsWith(prefix)) {
    return null
  }
  const assessmentId = pathname.slice(prefix.length)
  if (!ASSESSMENT_ID_PATTERN.test(assessmentId)) {
    return { kind: 'error', status: 404, detail: 'unknown capital allocation assessment' }
  }
  return view(`capital-allocation-assessment--${assessmentId}.json`)
}

function resolveSecurity(pathname: string): RouteResult {
  const prefix = '/api/securities/'
  if (!pathname.startsWith(prefix)) {
    return { kind: 'error', status: 404, detail: 'unknown API endpoint' }
  }
  const ticker = pathname.slice(prefix.length)
  if (!TICKER_PATTERN.test(ticker)) {
    return { kind: 'error', status: 404, detail: 'unknown ticker' }
  }
  return view(`security--${ticker}.json`)
}

function view(filename: string): RouteResult {
  return { kind: 'view', key: `views/${filename}` }
}

async function isAuthorized(request: Request, url: URL, env: Env): Promise<boolean> {
  const authorization = request.headers.get('Authorization')
  if (authorization !== null) {
    if (!authorization.startsWith('Bearer ')) return false
    const supplied = authorization.slice('Bearer '.length)
    const [owner, shared] = await Promise.all([
      matchesToken(supplied, env.VIEW_PASSWORD),
      matchesToken(supplied, env.READ_ACCESS_TOKEN),
    ])
    return owner || shared
  }
  return matchesToken(url.searchParams.get('share') ?? '', env.READ_ACCESS_TOKEN)
}

async function matchesToken(supplied: string, expected: string | undefined): Promise<boolean> {
  const [suppliedHash, expectedHash] = await Promise.all([
    sha256(supplied),
    sha256(expected ?? ''),
  ])
  return Boolean(expected) && crypto.subtle.timingSafeEqual(suppliedHash, expectedHash)
}

async function sha256(value: string): Promise<Uint8Array> {
  const bytes = new TextEncoder().encode(value)
  return new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
}

function jsonResponse(
  body: unknown,
  status: number,
  extraHeaders: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...API_HEADERS, ...extraHeaders },
  })
}

export default {
  fetch: handleRequest,
} satisfies ExportedHandler<Env>
