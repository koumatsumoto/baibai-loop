import { beforeEach, describe, expect, it, vi } from 'vitest'

import { handleRequest } from '../src/index'

const PASSWORD = '1234567890abcdefghijklmnopqrstuv'

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
  it.each([
    ['/api/dashboard', 'views/dashboard.json'],
    ['/api/tasks', 'views/tasks.json'],
    ['/api/daily-delta', 'views/daily-delta.json'],
    ['/api/screening/latest', 'views/screening_latest.json'],
    ['/api/screening/history/2026-07-23', 'history/candidate-views/2026-07-23.json'],
    ['/api/operations', 'views/operations.json'],
    ['/api/meta', 'views/meta.json'],
    ['/api/macro', 'views/macro.json'],
    ['/api/macro/series/us.10y', 'views/macro-series--us.10y.json'],
    [
      '/api/macro/context/macro-context-2026-07-01-example',
      'views/macro-context--macro-context-2026-07-01-example.json',
    ],
    ['/api/securities/7203', 'views/security--7203.json'],
    [
      '/api/capital-allocation-assessments/capital-allocation-assessment-20260728-example',
      'views/capital-allocation-assessment--capital-allocation-assessment-20260728-example.json',
    ],
  ])('maps %s to the fixed key %s', async (path, expectedKey) => {
    const get = vi.fn().mockResolvedValue(objectBody())
    const response = await handleRequest(request(path), environment(get))

    expect(response.status).toBe(200)
    expect(get).toHaveBeenCalledWith(expectedKey)
  })

  it.each([
    '/api/securities/7203/extra',
    '/api/securities/%2e%2e%2fhistory',
    '/api/screening/history/2026-07-23/extra',
    '/api/screening/history/not-a-date',
    '/api/macro/context/bad!id',
    '/api/macro/context/nested/id',
    '/api/macro/series/bad!id',
    '/api/macro/series/nested/id',
    '/api/capital-allocation-assessments/bad!id',
    '/api/capital-allocation-assessments/nested/id',
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

describe('raw L1 gateway', () => {
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
    ['/api/lake/current', 'lake/pointers/l1/current.json'],
    ['/api/lake/object?key=lake/manifests/releases/l1/r.json', 'lake/manifests/releases/l1/r.json'],
    ['/api/lake/object?key=lake/manifests/datasets/new/grain/d.json', 'lake/manifests/datasets/new/grain/d.json'],
    ['/api/lake/object?key=lake/l1/canonical/new/grain/p.parquet', 'lake/l1/canonical/new/grain/p.parquet'],
    ['/api/lake/object?key=lake%2Fl1%2Fcanonical%2Fd%2Fp%252Fname.parquet', 'lake/l1/canonical/d/p%252Fname.parquet'],
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
        expect(response.headers.get('Content-Disposition')).toBe('attachment; filename="partition.parquet"')
      } else {
        expect(response.headers.get('Content-Type')).toBe('application/json; charset=utf-8')
      }
      expect(await response.text()).toBe('raw bytes')
    } finally { fetchMock.mockRestore() }
  })

  it.each([
    '', '/lake/l1/canonical/p.parquet', 'market.sqlite', 'application/baibai.sqlite',
    'lake/staging/p.parquet', 'lake/pointers/l1/current.json', 'lake/l1/canonical/p.json',
    'https://other.test/lake/l1/canonical/p.parquet', 'lake/manifests/releases/other/r.json',
    'lake/l1/canonical/../../../application/x.parquet', 'lake/l1/canonical/./p.parquet',
    'lake/l1/canonical/a\\b.parquet', 'lake/l1/canonical/a\u0000.parquet',
    'lake/l1/canonical/a\u007f.parquet', 'lake/l1/canonical//p.parquet',
  ])('rejects key %s before fetching', async (key) => {
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
