import { beforeEach, describe, expect, it, vi } from 'vitest'

import routeContract from '../../contracts/routes.json'
import { handleRequest } from '../src/index'

const PASSWORD = '1234567890abcdefghijklmnopqrstuv'
const materializedRoutes = [
  ...Object.entries(routeContract.routes),
  ...Object.entries<string>(routeContract.edge_routes),
  ...Object.entries(routeContract.patterns),
].map(([path, key]) => [path, key] as const)

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

  it.each(['/api/screening/latest', '/api/market-context', '/api/operations', '/api/daily-delta', '/api/lake/current', '/api/lake/object?key=x', '/api/stock-assessments', '/api/investment-proposals', '/api/securities/7203'])('retires %s without reading R2', async path => {
    const get = vi.fn()
    const response = await handleRequest(request(path), environment(get))
    expect(response.status).toBe(404)
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

