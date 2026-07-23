const API_HEADERS = {
  'Cache-Control': 'no-store',
  'Content-Type': 'application/json; charset=utf-8',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
} as const
const HSTS_HEADER = API_HEADERS['Strict-Transport-Security']

const MACRO_PERIODS = new Set(['1y', '5y', '10y', 'max'])
const MACRO_GRANULARITIES = new Set(['daily', 'weekly', 'monthly', 'yearly'])
const TICKER_PATTERN = /^[0-9A-Z]{4}$/

type RouteResult =
  | { kind: 'health' }
  | { kind: 'view'; key: string }
  | { kind: 'error'; status: 404 | 422; detail: string }

export async function handleRequest(request: Request, env: Env): Promise<Response> {
  try {
    return await routeRequest(request, env)
  } catch (error) {
    console.error(
      JSON.stringify({
        message: 'request failed',
        path: new URL(request.url).pathname,
        error: error instanceof Error ? error.message : String(error),
      }),
    )
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
    return new Response(asset.body, {
      status: asset.status,
      statusText: asset.statusText,
      headers,
    })
  }

  if (!(await isAuthorized(request.headers.get('Authorization'), env.VIEW_PASSWORD))) {
    return jsonResponse({ detail: 'unauthorized' }, 401, {
      'WWW-Authenticate': 'Bearer',
    })
  }
  if (request.method !== 'GET') {
    return jsonResponse({ detail: 'method not allowed' }, 405, { Allow: 'GET' })
  }

  const route = resolveRoute(url)
  if (route.kind === 'error') {
    return jsonResponse({ detail: route.detail }, route.status)
  }
  if (route.kind === 'health') {
    return jsonResponse({ status: 'ok' }, 200)
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
    case '/api/screening/latest':
      return view('screening_latest.json')
    case '/api/operations':
      return view('operations.json')
    case '/api/meta':
      return view('meta.json')
    case '/api/macro':
      return resolveMacro(url.searchParams)
    default:
      return resolveSecurity(url.pathname)
  }
}

function resolveMacro(params: URLSearchParams): RouteResult {
  const period = params.get('period') ?? '1y'
  const granularity = params.get('granularity') ?? 'daily'
  if (!MACRO_PERIODS.has(period) || !MACRO_GRANULARITIES.has(granularity)) {
    return { kind: 'error', status: 422, detail: 'invalid macro period or granularity' }
  }
  return view(`macro--${period}-${granularity}.json`)
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

async function isAuthorized(authorization: string | null, expected: string): Promise<boolean> {
  const bearerPrefix = 'Bearer '
  const schemeIsValid = authorization?.startsWith(bearerPrefix) === true
  const supplied = schemeIsValid ? authorization.slice(bearerPrefix.length) : ''
  const [suppliedHash, expectedHash] = await Promise.all([
    sha256(supplied),
    sha256(expected),
  ])

  return (
    schemeIsValid &&
    expected.length > 0 &&
    crypto.subtle.timingSafeEqual(suppliedHash, expectedHash)
  )
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
