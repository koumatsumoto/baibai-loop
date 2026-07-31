import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const uiRoot = resolve(import.meta.dirname, '..')
const srcRoot = resolve(uiRoot, 'src')
const allowedRouterImports = new Set([
  'BrowserRouter',
  'Link',
  'Outlet',
  'Route',
  'Routes',
  'useLocation',
  'useParams',
])

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) {
      return sourceFiles(path)
    }
    return entry.name.endsWith('.ts') || entry.name.endsWith('.tsx') ? [path] : []
  })
}

const sources = sourceFiles(srcRoot).map((path) => ({
  path,
  source: readFileSync(path, 'utf8'),
}))

describe('router dependency boundary', () => {
  it('uses only the plain SPA exports from the v8 unified package', () => {
    const modules = sources.flatMap(({ source }) =>
      [...source.matchAll(/(?:from\s*|import\s*\(\s*)['"](react-router[^'"]*)['"]/g)].map(
        ([, module]) => module,
      ),
    )
    const importClauses = sources.flatMap(({ source }) =>
      [...source.matchAll(/import\s+(?:type\s+)?{([^}]*)}\s+from\s+['"]react-router['"]/gs)].map(
        ([, names]) => names,
      ),
    )
    const importedNames = importClauses.flatMap((names) =>
      names
        .split(',')
        .map((name) => name.trim().replace(/^type\s+/, '').split(/\s+as\s+/)[0])
        .filter(Boolean),
    )

    expect(modules.length).toBeGreaterThan(0)
    expect(modules).toEqual(Array(modules.length).fill('react-router'))
    expect(importClauses.length).toBe(modules.length)
    expect(importedNames.filter((name) => !allowedRouterImports.has(name))).toEqual([])
    expect(
      sources.filter(({ source }) => source.includes('react-router-dom')).map(({ path }) => path),
    ).toEqual([])
  })

  it('does not opt the Vite application into RSC mode', () => {
    const packageJson = readFileSync(resolve(uiRoot, 'package.json'), 'utf8')
    const viteConfig = readFileSync(resolve(uiRoot, 'vite.config.ts'), 'utf8')

    expect(packageJson).not.toContain('@vitejs/plugin-rsc')
    expect(viteConfig).not.toContain('@vitejs/plugin-rsc')
  })
})
