import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// PageShell and AppLayout only unify the pages that actually use them, and nothing in
// the type system says a new page must. These read the source the way brand.test.ts
// does, so the convention fails here instead of drifting one page at a time.

const uiRoot = resolve(import.meta.dirname, '..')
const pagesDir = resolve(uiRoot, 'src/pages')

const pages = readdirSync(pagesDir)
  .filter((name) => name.endsWith('Page.tsx'))
  .map((name) => ({ name, source: readFileSync(resolve(pagesDir, name), 'utf8') }))

describe('page skeleton', () => {
  it('finds every page', () => {
    expect(pages.length).toBeGreaterThanOrEqual(8)
  })

  it.each(pages)('$name renders through PageShell', ({ source }) => {
    expect(source).toContain('<PageShell')
  })

  // The shell is mounted once by the layout route. A page that mounts its own would
  // draw two headers, and remount the shell on every state it passes through.
  it.each(pages)('$name does not mount the app shell itself', ({ source }) => {
    expect(source).not.toContain('<AppShell')
  })

  // The page container carries the measure, the padding and the gap between sections,
  // so forbidding a second one is what keeps those three in one place. `max-w-*` on a
  // cell or a chart stays the element's own business.
  it.each(pages)('$name does not open its own main element', ({ source }) => {
    expect(source).not.toContain('<main')
  })
})

describe('app shell ownership', () => {
  const layout = readFileSync(resolve(uiRoot, 'src/components/AppLayout.tsx'), 'utf8')
  const app = readFileSync(resolve(uiRoot, 'src/App.tsx'), 'utf8')

  it('mounts the shell in the layout route', () => {
    expect(layout).toContain('<AppShell />')
    expect(layout).toContain('<Outlet />')
    expect(app).toContain('<Route element={<AppLayout />}>')
  })

  // Loading and error states render under the shell, not beside it.
  it.each(['src/components/LoadingIndicator.tsx', 'src/components/PageState.tsx', 'src/components/PageShell.tsx'])(
    '%s does not mount the shell',
    (path) => {
      expect(readFileSync(resolve(uiRoot, path), 'utf8')).not.toContain('<AppShell')
    },
  )
})
