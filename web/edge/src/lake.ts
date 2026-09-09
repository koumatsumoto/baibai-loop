/** Research工程でfixed releaseのmarket L1を変換せずread-only S3経由で見せる。 */
import { AwsClient } from 'aws4fetch'

function allowedObject(key: string): boolean {
  if (!key || /[\x00-\x1f\x7f\\]/.test(key) || key.startsWith('/')) return false
  // Reject dot segments before URL construction can normalize them out of the namespace.
  if (key.split('/').some((part) => !part || part === '.' || part === '..')) return false
  return (
    (key.startsWith('lake/manifests/releases/l1/') && key.endsWith('.json')) ||
    (key.startsWith('lake/manifests/datasets/') && key.endsWith('.json')) ||
    (key.startsWith('lake/l1/canonical/') && key.endsWith('.parquet'))
  )
}

export async function lakeResponse(
  url: URL,
  env: Env,
  headers: Record<string, string>,
): Promise<Response> {
  const error = (status: number, detail: string) =>
    new Response(JSON.stringify({ detail }), { status, headers })
  let key = 'lake/pointers/l1/current.json'
  if (url.pathname === '/api/lake/object') {
    const keys = url.searchParams.getAll('key')
    if (keys.length !== 1 || !allowedObject(keys[0])) return error(400, 'invalid lake key')
    key = keys[0]
  }
  if (!env.L1_R2_BASE_URL || !env.L1_R2_ACCESS_KEY_ID || !env.L1_R2_SECRET_ACCESS_KEY) {
    return error(503, 'lake unavailable')
  }
  try {
    const base = new URL(env.L1_R2_BASE_URL)
    if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash) {
      return error(503, 'lake unavailable')
    }
    const objectUrl = `${base.href.replace(/\/$/, '')}/${key.split('/').map(encodeURIComponent).join('/')}`
    const client = new AwsClient({
      accessKeyId: env.L1_R2_ACCESS_KEY_ID,
      secretAccessKey: env.L1_R2_SECRET_ACCESS_KEY,
      service: 's3',
      region: 'auto',
    })
    // Construct a fresh GET: caller headers, query credentials and Range never cross this boundary.
    const signed = await client.sign(objectUrl, { method: 'GET', redirect: 'manual' })
    const upstream = await fetch(signed)
    if (upstream.status !== 200) {
      await upstream.body?.cancel()
      return upstream.status === 404 ? error(404, 'lake object not found') : error(502, 'lake upstream unavailable')
    }
    const responseHeaders = new Headers(headers)
    if (key.endsWith('.parquet')) {
      responseHeaders.set('Content-Type', 'application/vnd.apache.parquet')
      responseHeaders.set('Content-Disposition', 'attachment; filename="partition.parquet"')
    }
    return new Response(upstream.body, { headers: responseHeaders })
  } catch {
    // Neither S3 error bodies nor signed URLs belong in logs or caller responses.
    return error(502, 'lake upstream unavailable')
  }
}
