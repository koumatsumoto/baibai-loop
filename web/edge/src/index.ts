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
// Mirrors _MARKET_CONTEXT_ASSESSMENT_ID_FORMAT in baibai_web.materialize; excludes
// path separators so the id maps to exactly one serving key.
const MARKET_CONTEXT_ASSESSMENT_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/
const INDICATOR_SERIES_ID_PATTERN = /^[a-z0-9._-]{1,128}$/
const STOCK_ASSESSMENT_ID_PATTERN = /^stock-assessment-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const PROPOSAL_ID_PATTERN = /^investment-proposal-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

type RouteResult =
  | { kind: 'health' }
  | { kind: 'screening-history-index' }
  | { kind: 'view'; key: string }
  | { kind: 'research-index'; key: string; collection: 'assessments' | 'proposals' }
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
  if (route.kind === 'research-index') return researchIndex(object, route.collection, url)
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
    case '/api/stock-assessments':
      return { kind: 'research-index', key: 'views/stock-assessments.json', collection: 'assessments' }
    case '/api/investment-proposals':
      return { kind: 'research-index', key: 'views/investment-proposals.json', collection: 'proposals' }
    case '/api/tasks':
      return view('tasks.json')
    case '/api/meta':
      return view('meta.json')
    case '/api/market-context':
      return view('market-context.json')
    default:
      return (
        resolveScreeningHistory(url.pathname) ??
        resolveIndicatorSeries(url.pathname) ??
        resolveMarketContextAssessment(url.pathname) ??
        resolveStockAssessment(url.pathname) ??
        resolveInvestmentProposalPreview(url.pathname) ??
        resolveInvestmentProposal(url.pathname) ??
        resolveSecurity(url.pathname)
      )
  }
}

function resolveIndicatorSeries(pathname: string): RouteResult | null {
  const prefix = '/api/market-context/indicators/'
  if (!pathname.startsWith(prefix)) {
    return null
  }
  const seriesId = pathname.slice(prefix.length)
  if (!INDICATOR_SERIES_ID_PATTERN.test(seriesId)) {
    return { kind: 'error', status: 404, detail: 'unknown indicator series' }
  }
  return view(`indicator-series--${seriesId}.json`)
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
    .filter((object) => object.key.endsWith('.json'))
    .map((object) => object.key.slice(prefix.length, -'.json'.length))
    .filter((value) => SCREENING_DATE_PATTERN.test(value))
    .sort((left, right) => right.localeCompare(left))
  return jsonResponse({ dates }, 200)
}

function resolveMarketContextAssessment(pathname: string): RouteResult | null {
  const prefix = '/api/market-context/assessments/'
  if (!pathname.startsWith(prefix)) {
    return null
  }
  const contextId = pathname.slice(prefix.length)
  if (!MARKET_CONTEXT_ASSESSMENT_ID_PATTERN.test(contextId)) {
    return { kind: 'error', status: 404, detail: 'unknown market context assessment' }
  }
  return view(`market-context-assessment--${contextId}.json`)
}

function resolveStockAssessment(pathname: string): RouteResult | null {
  const prefix = '/api/stock-assessments/'
  if (!pathname.startsWith(prefix)) return null
  const identifier = pathname.slice(prefix.length)
  if (!STOCK_ASSESSMENT_ID_PATTERN.test(identifier)) {
    return { kind: 'error', status: 404, detail: 'unknown stock assessment' }
  }
  return view(`stock-assessment--${identifier}.json`)
}

function resolveInvestmentProposal(pathname: string): RouteResult | null {
  const prefix = '/api/investment-proposals/'
  if (!pathname.startsWith(prefix)) return null
  const identifier = pathname.slice(prefix.length)
  if (!PROPOSAL_ID_PATTERN.test(identifier)) {
    return { kind: 'error', status: 404, detail: 'unknown investment proposal' }
  }
  return view(`investment-proposal--${identifier}.json`)
}

function resolveInvestmentProposalPreview(pathname: string): RouteResult | null {
  const prefix = '/api/investment-proposals/'
  const suffix = '/current-preview'
  if (!pathname.startsWith(prefix) || !pathname.endsWith(suffix)) return null
  const identifier = pathname.slice(prefix.length, -suffix.length)
  if (!PROPOSAL_ID_PATTERN.test(identifier)) {
    return { kind: 'error', status: 404, detail: 'unknown investment proposal' }
  }
  return view(`investment-proposal-preview--${identifier}.json`)
}

async function researchIndex(object: R2ObjectBody, collection: 'assessments' | 'proposals', url: URL): Promise<Response> {
  const rawLimit = url.searchParams.get('limit') ?? '100'
  if (!/^[0-9]+$/.test(rawLimit) || Number(rawLimit) < 1 || Number(rawLimit) > 100) {
    return jsonResponse({ detail: 'limit must be between 1 and 100' }, 422)
  }
  const scope = url.searchParams.get('scope')
  if (collection === 'proposals' && scope !== null && !['discover_new', 'specified_stocks', 'review_positions'].includes(scope)) {
    return jsonResponse({ detail: 'unknown proposal scope' }, 422)
  }
  const payload: unknown = await new Response(object.body).json()
  if (payload === null || typeof payload !== 'object' || !Array.isArray((payload as Record<string, unknown>)[collection])) {
    throw new Error('invalid materialized research index')
  }
  const typed = payload as Record<string, unknown>
  const rows = typed[collection] as Record<string, unknown>[]
  const ticker = url.searchParams.get('ticker')
  const filtered = rows.filter(row => (collection === 'assessments'
    ? ticker === null || row.ticker === ticker
    : (scope === null || row.request_scope === scope) && (ticker === null || (Array.isArray(row.tickers) && row.tickers.includes(ticker)))))
  const identifier = collection === 'assessments' ? 'assessment_id' : 'proposal_id'
  const cursor = url.searchParams.get('cursor')
  let offset = 0
  if (cursor !== null) {
    const previous = filtered.findIndex(row => row[identifier] === cursor)
    if (previous < 0) {
      return jsonResponse({ detail: { code: 'invalid_cursor', message: 'cursor does not identify a publication in this result' } }, 400)
    }
    offset = previous + 1
  }
  const limit = Number(rawLimit)
  const page = filtered.slice(offset, offset + limit)
  const next = page.length > 0 && offset + limit < filtered.length ? page[page.length - 1][identifier] : null
  return jsonResponse({ ...typed, [collection]: page, next_cursor: next }, 200)
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
