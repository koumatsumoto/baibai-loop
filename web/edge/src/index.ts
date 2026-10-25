/** 保有・運用成績・タスクの読取専用配信。認証方式は既存のまま。 */

const API_HEADERS = {
  'Cache-Control': 'no-store',
  'Content-Type': 'application/json; charset=utf-8',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
} as const
const HSTS_HEADER = API_HEADERS['Strict-Transport-Security']

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

  if (url.pathname === '/api/health') return jsonResponse({ status: 'ok' }, 200)
  const keys: Record<string, string> = {
    '/api/dashboard': 'views/portfolio-dashboard.json',
    '/api/tasks': 'views/tasks.json',
    '/api/outcomes': 'views/outcomes.json',
  }
  const key = keys[url.pathname]
  if (!key) return jsonResponse({ detail: 'unknown API endpoint' }, 404)
  const object = await env.BAIBAI_SERVING.get(key)
  if (object === null) return jsonResponse({ detail: 'view not found' }, 404)
  return new Response(object.body, { status: 200, headers: API_HEADERS })
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
