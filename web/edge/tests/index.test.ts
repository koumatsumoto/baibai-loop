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
