import { beforeEach, describe, expect, it, vi } from 'vitest'

import routeContract from '../../contracts/routes.json'
import { handleRequest } from '../src/index'

const PASSWORD = '1234567890abcdefghijklmnopqrstuv'
const materializedRoutes = Object.entries(routeContract.routes)

function environment(get: ReturnType<typeof vi.fn>) {
  return {
    BAIBAI_SERVING: { get } as unknown as R2Bucket,
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
  it.each(materializedRoutes)('serves %s from the contract key %s', async (path, expectedKey) => {
    const payload = { source: expectedKey, assessments: [], proposals: [] }
    const get = vi.fn().mockResolvedValue(objectBody(JSON.stringify(payload)))
    const response = await handleRequest(request(path), environment(get))

    expect(response.status).toBe(200)
    expect(get.mock.calls).toEqual([[expectedKey]])
    expect(response.headers.get('Cache-Control')).toBe('no-store')
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull()
    expect(await response.json()).toMatchObject(payload)
  })

  it.each(['/api/tasks', '/api/screening/latest', '/api/market-context', '/api/operations', '/api/daily-delta', '/api/lake/current', '/api/lake/object?key=x', '/api/stock-assessments', '/api/investment-proposals', '/api/securities/7203'])('retires %s without reading R2', async path => {
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

  it.each(materializedRoutes)('returns 404 when %s is absent at %s', async path => {
    const get = vi.fn().mockResolvedValue(null)
    const response = await handleRequest(request(path), environment(get))

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

  it.each(materializedRoutes)('protects %s before rejecting a write to %s', async path => {
    const get = vi.fn()
    const unauthorized = await handleRequest(
      request(path, null, 'POST'),
      environment(get),
    )
    const authorized = await handleRequest(request(path, PASSWORD, 'POST'), environment(get))

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

describe('removed shared access', () => {
  it('does not accept the former shared token in a query or bearer header', async () => {
    const get = vi.fn()
    const env = { ...environment(get), READ_ACCESS_TOKEN: 'old-shared-token' }
    expect((await handleRequest(request('/api/health?share=old-shared-token', null), env)).status).toBe(401)
    expect((await handleRequest(request('/api/health', 'old-shared-token'), env)).status).toBe(401)
    expect((await handleRequest(request('/api/health'), env)).status).toBe(200)
    expect(get).not.toHaveBeenCalled()
  })
})
