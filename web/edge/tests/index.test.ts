import { beforeEach, describe, expect, it, vi } from 'vitest'

import routeContract from '../../contracts/routes.json'
import { handleRequest } from '../src/index'

const PASSWORD = '1234567890abcdefghijklmnopqrstuv'
const PROPOSAL_ID = 'investment-proposal-00000000-0000-4000-8000-000000000001'
const routeParameters: Record<string, string> = {
  as_of: '2026-07-23',
  series_id: 'us.10y',
  context_id: 'market-context-assessment-2026-07-01-current',
  ticker: '7203',
  assessment_id: 'stock-assessment-00000000-0000-4000-8000-000000000001',
  proposal_id: PROPOSAL_ID,
}

function expandRouteTemplate(template: string): string {
  return template.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    const value = routeParameters[name]
    if (value === undefined) throw new Error(`missing route-contract sample: ${name}`)
    return value
  })
}

const materializedRoutes = [
  ...Object.entries(routeContract.routes),
  ...Object.entries<string>(routeContract.edge_routes),
  ...Object.entries(routeContract.patterns),
].map(([path, key]) => [expandRouteTemplate(path), expandRouteTemplate(key)] as const)

function environment(
  get: ReturnType<typeof vi.fn>,
  list: ReturnType<typeof vi.fn> = vi.fn().mockResolvedValue({
    objects: [],
    delimitedPrefixes: [],
    truncated: false,
  }),
) {
  return {
    BAIBAI_SERVING: { get, list } as unknown as R2Bucket,
    ASSETS: { fetch: vi.fn().mockResolvedValue(new Response(null, { status: 204 })) } as unknown as Fetcher,
    VIEW_PASSWORD: PASSWORD,
    READ_ACCESS_TOKEN: '',
    L1_R2_BASE_URL: '',
    L1_R2_ACCESS_KEY_ID: '',
    L1_R2_SECRET_ACCESS_KEY: '',
  }
}

function request(path: string, password: string | null = PASSWORD, method = 'GET'): Request {
  const headers = password === null ? undefined : { Authorization: `Bearer ${password}` }
  return new Request(`https://example.test${path}`, { method, headers })
}

function objectBody(body = '{"ok":true}') {
  return { body: new Response(body).body } as unknown as R2ObjectBody
}

describe('API authentication', () => {
  let get: ReturnType<typeof vi.fn>

  beforeEach(() => {
    get = vi.fn().mockResolvedValue(objectBody())
  })

  it.each([
    ['missing credentials', null],
    ['wrong credentials', 'wrong-password'],
  ])('returns 401 for %s before reading R2', async (_label, password) => {
    const response = await handleRequest(request('/api/dashboard', password), environment(get))

    expect(response.status).toBe(401)
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull()
    expect(get).not.toHaveBeenCalled()
  })

  it('returns 200 for the exact bearer password', async () => {
    const response = await handleRequest(request('/api/dashboard'), environment(get))

    expect(response.status).toBe(200)
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull()
  })

  it('fails closed when VIEW_PASSWORD is absent', async () => {
    const env = environment(get)
    env.VIEW_PASSWORD = ''
    const response = await handleRequest(
      new Request('https://example.test/api/dashboard', {
        headers: { Authorization: 'Bearer ' },
      }),
      env,
    )

    expect(response.status).toBe(401)
    expect(get).not.toHaveBeenCalled()
  })

  it('redirects HTTP to HTTPS before authentication or R2 access', async () => {
    const response = await handleRequest(
      new Request('http://example.test/api/dashboard', {
        headers: { Authorization: `Bearer ${PASSWORD}` },
      }),
      environment(get),
    )

    expect(response.status).toBe(308)
    expect(response.headers.get('Location')).toBe('https://example.test/api/dashboard')
    expect(get).not.toHaveBeenCalled()
  })
})

describe('view routing', () => {
  it.each(materializedRoutes)('serves %s from the contract key %s', async (path, expectedKey) => {
    const payload = { source: expectedKey, assessments: [], proposals: [] }
    const get = vi.fn().mockResolvedValue(objectBody(JSON.stringify(payload)))
    const response = await handleRequest(request(path), environment(get))

    expect(response.status).toBe(200)
    expect(get.mock.calls).toEqual([[expectedKey]])
    expect(await response.json()).toMatchObject(payload)
  })

  it('keeps historical market-context identifiers addressable', async () => {
    const contextId = 'macro-context-2026-07-01-example'
    const get = vi.fn().mockResolvedValue(objectBody())
    const response = await handleRequest(
      request(`/api/market-context/assessments/${contextId}`),
      environment(get),
    )

    expect(response.status).toBe(200)
    expect(get.mock.calls).toEqual([[`views/market-context-assessment--${contextId}.json`]])
  })

  it.each([
    '/api/securities/7203/extra',
    '/api/securities/%2e%2e%2fhistory',
    '/api/screening/history/2026-07-23/extra',
    '/api/screening/history/not-a-date',
    '/api/market-context/assessments/bad!id',
    '/api/market-context/assessments/nested/id',
    '/api/market-context/indicators/bad!id',
    '/api/market-context/indicators/nested/id',
    '/api/stock-assessments/bad!id',
    '/api/investment-proposals/bad!id',
    '/api/stock-assessments/nested/id',
    '/api/investment-proposals/nested/id',
    '/api/investment-proposals/investment-proposal-00000000-0000-4000-8000-000000000001/current-preview/extra',
    '/api/capital-allocation-assessments',
    '/api/capital-allocation-assessments/retired',
    '/api/research-triages/bad!id',
    '/api/research-triages/nested/id',
    '/api/unknown',
    '/api',
  ])('does not let request input escape the view-key whitelist: %s', async (path) => {
    const get = vi.fn()
    const response = await handleRequest(request(path), environment(get))

    expect(response.status).toBe(404)
    expect(get).not.toHaveBeenCalled()
  })

  it('lists retained candidate dates without exposing arbitrary R2 keys', async () => {
    const get = vi.fn()
    const list = vi.fn().mockResolvedValue({
      objects: [
        { key: 'history/candidate-views/2026-07-22.json' },
        { key: 'history/candidate-views/not-a-date.json' },
        { key: 'history/candidate-views/2026-07-24.yaml' },
        { key: 'history/candidate-views/2026-07-25.json.bak' },
        { key: 'history/candidate-views/2026-07-23.json' },
      ],
      delimitedPrefixes: [],
      truncated: false,
    })
    const response = await handleRequest(
      request('/api/screening/history'),
      environment(get, list),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ dates: ['2026-07-23', '2026-07-22'] })
    expect(list).toHaveBeenCalledWith({ prefix: 'history/candidate-views/', limit: 64 })
    expect(get).not.toHaveBeenCalled()
  })

  it('continues listing so newer candidate dates on later R2 pages remain visible', async () => {
    const get = vi.fn()
    const list = vi
      .fn()
      .mockResolvedValueOnce({
        objects: [{ key: 'history/candidate-views/2026-07-22.json' }],
        delimitedPrefixes: [],
        truncated: true,
        cursor: 'next-page',
      })
      .mockResolvedValueOnce({
        objects: [{ key: 'history/candidate-views/2026-10-19.json' }],
        delimitedPrefixes: [],
        truncated: false,
      })
    const response = await handleRequest(
      request('/api/screening/history'),
      environment(get, list),
    )

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ dates: ['2026-10-19', '2026-07-22'] })
    expect(list.mock.calls).toEqual([
      [{ prefix: 'history/candidate-views/', limit: 64 }],
      [{ prefix: 'history/candidate-views/', limit: 64, cursor: 'next-page' }],
    ])
    expect(get).not.toHaveBeenCalled()
  })

  it('lets URL-normalized non-API paths reach static Assets without reading R2', async () => {
    const get = vi.fn()
    const response = await handleRequest(
      request('/api/securities/../../history'),
      environment(get),
    )

    expect(response.status).toBe(204)
    expect(get).not.toHaveBeenCalled()
  })

  it('returns 404 when the materialized view does not exist', async () => {
    const get = vi.fn().mockResolvedValue(null)
    const response = await handleRequest(request('/api/dashboard'), environment(get))

    expect(response.status).toBe(404)
    expect(response.headers.get('Cache-Control')).toBe('no-store')
  })

  it('fails closed with a generic no-store response when R2 throws', async () => {
    const get = vi.fn().mockRejectedValue(new Error('private R2 detail'))
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    const response = await handleRequest(request('/api/dashboard'), environment(get))

    expect(response.status).toBe(500)
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull()
    await expect(response.json()).resolves.toEqual({ detail: 'internal server error' })
    expect(error).toHaveBeenCalledOnce()
    error.mockRestore()
  })

  it('requires authentication before rejecting an unsupported method', async () => {
    const get = vi.fn()
    const unauthorized = await handleRequest(
      request('/api/dashboard', null, 'POST'),
      environment(get),
    )
    const authorized = await handleRequest(request('/api/dashboard', PASSWORD, 'POST'), environment(get))

    expect(unauthorized.status).toBe(401)
    expect(authorized.status).toBe(405)
    expect(get).not.toHaveBeenCalled()
  })

  it('serves health only after authentication and without reading R2', async () => {
    const get = vi.fn()
    const response = await handleRequest(request('/api/health'), environment(get))

    expect(response.status).toBe(200)
    expect(response.headers.get('Strict-Transport-Security')).toContain('max-age=31536000')
    await expect(response.json()).resolves.toEqual({ status: 'ok' })
    expect(get).not.toHaveBeenCalled()
  })

  it('adds HSTS when delegating an HTTPS navigation to static Assets', async () => {
    const get = vi.fn()
    const env = environment(get)
    const response = await handleRequest(request('/screening'), env)

    expect(response.status).toBe(204)
    expect(response.headers.get('Strict-Transport-Security')).toContain('max-age=31536000')
    expect(env.ASSETS.fetch).toHaveBeenCalledOnce()
    expect(get).not.toHaveBeenCalled()
  })
})

describe('shared read authentication', () => {
  it.each([
    [null, 'shared-secret', 200],
    ['shared-secret', null, 200],
    [PASSWORD, 'wrong', 200],
    ['wrong', 'shared-secret', 401],
    [null, PASSWORD, 401],
    [null, '', 401],
    [null, 'wrong', 401],
  ])('authenticates header %s and share %s', async (bearer, share, status) => {
    const env = { ...environment(vi.fn().mockResolvedValue(objectBody())), READ_ACCESS_TOKEN: 'shared-secret' }
    const query = share === null ? '' : `?share=${share}`
    expect((await handleRequest(request(`/api/dashboard${query}`, bearer), env)).status).toBe(status)
  })

  it('rejects duplicate share even with valid owner authentication', async () => {
    expect((await handleRequest(request('/api/health?share=a&share=b'), environment(vi.fn()))).status).toBe(400)
  })

  it.each(['', undefined])('disables only shared access when token is %s', async (value) => {
    const env = environment(vi.fn())
    if (value === undefined) Reflect.deleteProperty(env, 'READ_ACCESS_TOKEN')
    else env.READ_ACCESS_TOKEN = value
    expect((await handleRequest(request('/api/health?share=shared-secret', null), env)).status).toBe(401)
    expect((await handleRequest(request('/api/health'), env)).status).toBe(200)
  })

  it('does not rescue an empty or malformed Authorization header with share', async () => {
    const env = { ...environment(vi.fn()), READ_ACCESS_TOKEN: 'shared-secret' }
    for (const authorization of ['', 'Basic shared-secret']) {
      const req = new Request('https://example.test/api/health?share=shared-secret', { headers: { Authorization: authorization } })
      expect((await handleRequest(req, env)).status).toBe(401)
    }
  })

  it('protects shared bootstrap responses without changing ordinary static caching', async () => {
    const env = environment(vi.fn())
    const shared = await handleRequest(request('/?share=secret', null), env)
    expect(shared.headers.get('Cache-Control')).toBe('no-store')
    expect(shared.headers.get('Referrer-Policy')).toBe('no-referrer')
    expect(shared.headers.get('X-Robots-Tag')).toBe('noindex, nofollow, noarchive')
    const ordinary = await handleRequest(request('/', null), env)
    expect(ordinary.headers.get('Cache-Control')).toBeNull()
    expect(ordinary.headers.get('Referrer-Policy')).toBeNull()
  })
})

describe('market lake gateway', () => {
  function lakeEnv() {
    return {
      ...environment(vi.fn()),
      READ_ACCESS_TOKEN: 'shared-secret',
      L1_R2_BASE_URL: 'https://account.r2.cloudflarestorage.com/test-stores',
      L1_R2_ACCESS_KEY_ID: 'reader-id',
      L1_R2_SECRET_ACCESS_KEY: 'reader-secret',
    }
  }

  it.each([
    ['/api/lake/current', 'lake/pointers/market/current.json'],
    ['/api/lake/object?key=lake/manifests/releases/market/r.json', 'lake/manifests/releases/market/r.json'],
    ['/api/lake/object?key=lake/manifests/datasets/new/grain/d.json', 'lake/manifests/datasets/new/grain/d.json'],
    ['/api/lake/object?key=lake/l1/source/new/grain/p.parquet', 'lake/l1/source/new/grain/p.parquet'],
    ['/api/lake/object?key=lake/l2/processed/new/grain/p.parquet', 'lake/l2/processed/new/grain/p.parquet'],
    ['/api/lake/object?key=lake%2Fl2%2Fprocessed%2Fd%2Fp%252Fname.parquet', 'lake/l2/processed/d/p%252Fname.parquet'],
  ])('streams %s via a fresh signed GET', async (path, key) => {
    const upstream = new Response('raw bytes')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(upstream)
    try {
      const url = path + (path.includes('?') ? '&' : '?') + 'share=shared-secret'
      const req = request(url, 'shared-secret')
      req.headers.set('Range', 'bytes=0-1')
      const response = await handleRequest(req, lakeEnv())
      expect(response.status).toBe(200)
      expect(response.body).toBe(upstream.body)
      expect(response.headers.get('Cache-Control')).toBe('no-store')
      const sent = fetchMock.mock.calls[0][0] as Request
      expect(sent.method).toBe('GET')
      expect(sent.url).toBe(`https://account.r2.cloudflarestorage.com/test-stores/${key}`)
      expect(sent.redirect).toBe('manual')
      expect(sent.headers.get('Authorization')).toContain('AWS4-HMAC-SHA256 Credential=reader-id/')
      expect(sent.headers.get('Authorization')).not.toContain('shared-secret')
      expect(sent.headers.get('Range')).toBeNull()
      if (key.endsWith('.parquet')) {
        expect(response.headers.get('Content-Type')).toBe('application/vnd.apache.parquet')
        expect(response.headers.get('Content-Disposition')).toBe(
          key.includes('p%252Fname') ? 'attachment; filename="p_2Fname.parquet"' : 'attachment; filename="p.parquet"',
        )
      } else {
        expect(response.headers.get('Content-Type')).toBe('application/json; charset=utf-8')
      }
      expect(await response.text()).toBe('raw bytes')
    } finally { fetchMock.mockRestore() }
  })

  it.each([
    '', '/lake/l1/source/p.parquet', 'market.sqlite', 'application/baibai.sqlite',
    'lake/staging/p.parquet', 'lake/pointers/market/current.json', 'lake/l1/source/p.json',
    'https://other.test/lake/l2/processed/p.parquet', 'lake/manifests/releases/other/r.json',
    'lake/l1/source/../../../application/x.parquet', 'lake/l2/processed/./p.parquet',
    'lake/l1/source/a\\b.parquet', 'lake/l2/processed/a\u0000.parquet',
    'lake/l1/source/a\u007f.parquet', 'lake/l2/processed//p.parquet',
  ])('rejects key %s before fetching', async (key) => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    try {
      const response = await handleRequest(request(`/api/lake/object?key=${encodeURIComponent(key)}`), lakeEnv())
      expect(response.status).toBe(400)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally { fetchMock.mockRestore() }
  })

  it.each([
    'lake/l1/canonical/old.parquet',
    'lake/l2/canonical/old.parquet',
    'lake/manifests/releases/l1/r.json',
    'lake/pointers/l1/current.json',
  ])('rejects retired lake prefix %s', async (key) => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    try {
      const response = await handleRequest(request(`/api/lake/object?key=${encodeURIComponent(key)}`), lakeEnv())
      expect(response.status).toBe(400)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally { fetchMock.mockRestore() }
  })

  it.each(['/api/lake/object', '/api/lake/object?key=a&key=b'])('rejects missing or duplicate key: %s', async (path) => {
    expect((await handleRequest(request(path), lakeEnv())).status).toBe(400)
  })

  it.each(['POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS', 'PATCH'])('rejects %s without upstream access', async (method) => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    try {
      expect((await handleRequest(request('/api/lake/current?share=shared-secret', null, method), lakeEnv())).status).toBe(405)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally { fetchMock.mockRestore() }
  })

  it.each(['L1_R2_BASE_URL', 'L1_R2_ACCESS_KEY_ID', 'L1_R2_SECRET_ACCESS_KEY'] as const)('isolates missing %s to lake', async (key) => {
    const env = lakeEnv()
    env[key] = ''
    expect((await handleRequest(request('/api/lake/current'), env)).status).toBe(503)
    expect((await handleRequest(request('/api/health'), env)).status).toBe(200)
  })

  it.each([301, 302, 403, 404, 500, 503])('safely maps upstream %s and discards its body', async (status) => {
    const upstream = new Response('private upstream details', { status, headers: { Location: 'https://private.test/signed' } })
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(upstream)
    try {
      const response = await handleRequest(request('/api/lake/current'), lakeEnv())
      expect(response.status).toBe(status === 404 ? 404 : 502)
      expect(response.headers.get('Location')).toBeNull()
      expect(await response.text()).not.toContain('private')
      expect(upstream.bodyUsed).toBe(true)
    } finally { fetchMock.mockRestore() }
  })

  it('does not expose signed URLs from upstream exceptions', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('https://private.test/?signature=secret'))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const response = await handleRequest(request('/api/lake/current'), lakeEnv())
      expect(response.status).toBe(502)
      expect(await response.text()).not.toContain('secret')
      expect(log).not.toHaveBeenCalled()
    } finally { fetchMock.mockRestore(); log.mockRestore() }
  })
})


describe('Parquet download identity', () => {
  it('preserves different content-addressed basenames across partition downloads', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(new Uint8Array([80, 65, 82, 49])))
    try {
      const env = {
        ...environment(vi.fn()),
        L1_R2_BASE_URL: 'https://account.r2.cloudflarestorage.com/test-stores',
        L1_R2_ACCESS_KEY_ID: 'reader-id',
        L1_R2_SECRET_ACCESS_KEY: 'reader-secret',
      }
      for (const digest of ['a'.repeat(64), 'b'.repeat(64)]) {
        const filename = `part-${digest}.parquet`
        const key = `lake/l2/processed/daily-bars/2026-09/${filename}`
        const response = await handleRequest(request(`/api/lake/object?key=${encodeURIComponent(key)}`), env)
        expect(response.headers.get('Content-Disposition')).toBe(`attachment; filename="${filename}"`)
        expect(new Uint8Array(await response.arrayBuffer())).toEqual(new Uint8Array([80, 65, 82, 49]))
      }
      const key = 'lake/l1/source/d/悪い";name.parquet'
      const response = await handleRequest(request(`/api/lake/object?key=${encodeURIComponent(key)}`), env)
      expect(response.status).toBe(200)
      expect(response.headers.get('Content-Disposition')).toBe('attachment; filename="____name.parquet"')
    } finally { fetchMock.mockRestore() }
  })
})

describe('immutable research views and paginated indexes', () => {
  it('serves the saved proposal separately from its current preview', async () => {
    const saved = { proposal_id: PROPOSAL_ID, items: [{ ticker: '7203', action: 'buy' }] }
    const preview = { saved_proposal_id: PROPOSAL_ID, current_inputs: { cash_balance_yen: '0' } }
    const savedKey = `views/investment-proposal--${PROPOSAL_ID}.json`
    const previewKey = `views/investment-proposal-preview--${PROPOSAL_ID}.json`
    const get = vi.fn().mockImplementation(async (key: string) => {
      if (key === savedKey) return objectBody(JSON.stringify(saved))
      if (key === previewKey) return objectBody(JSON.stringify(preview))
      return null
    })
    const env = environment(get)

    const current = await handleRequest(request(`/api/investment-proposals/${PROPOSAL_ID}/current-preview`), env)
    expect(current.status).toBe(200)
    expect(await current.json()).toEqual(preview)
    const detail = await handleRequest(request(`/api/investment-proposals/${PROPOSAL_ID}`), env)
    expect(detail.status).toBe(200)
    expect(await detail.json()).toEqual(saved)
    expect(get.mock.calls).toEqual([[previewKey], [savedKey]])
  })

  it('filters ticker/scope before paging and validates the cursor in that result', async () => {
    const rows = [
      { proposal_id: 'p3', tickers: ['2331'], request_scope: 'specified_stocks' },
      { proposal_id: 'p2', tickers: ['7203'], request_scope: 'specified_stocks' },
      { proposal_id: 'p1', tickers: ['2331'], request_scope: 'specified_stocks' },
      { proposal_id: 'p0', tickers: ['2331'], request_scope: 'review_positions' },
    ]
    const get = vi.fn().mockImplementation(async () => objectBody(JSON.stringify({ proposals: rows, next_cursor: null })))
    const env = environment(get)
    const first = await handleRequest(request('/api/investment-proposals?ticker=2331&scope=specified_stocks&limit=1'), env)
    expect(await first.json()).toEqual({ proposals: [rows[0]], next_cursor: 'p3' })
    const last = await handleRequest(request('/api/investment-proposals?ticker=2331&scope=specified_stocks&limit=1&cursor=p3'), env)
    expect(await last.json()).toEqual({ proposals: [rows[2]], next_cursor: null })
    expect((await handleRequest(request('/api/investment-proposals?ticker=2331&cursor=p2'), env)).status).toBe(400)
    expect(rows).toHaveLength(4)
  })

  it('applies the same ticker and default maximum page to assessments', async () => {
    const rows = Array.from({ length: 101 }, (_, index) => ({ assessment_id: `a${index}`, ticker: '2331' }))
    const get = vi.fn().mockImplementation(async () => objectBody(JSON.stringify({ assessments: rows })))
    const response = await handleRequest(request('/api/stock-assessments?ticker=2331'), environment(get))
    const payload = await response.json() as { assessments: unknown[]; next_cursor: string }
    expect(payload.assessments).toHaveLength(100)
    expect(payload.next_cursor).toBe('a99')
  })

  it.each(['0', '101', '-1', 'nan'])('rejects invalid limit %s', async limit => {
    const get = vi.fn().mockImplementation(async () => objectBody('{"assessments":[]}'))
    expect((await handleRequest(request(`/api/stock-assessments?limit=${limit}`), environment(get))).status).toBe(422)
  })
})
