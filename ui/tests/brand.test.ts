import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const uiRoot = resolve(import.meta.dirname, '..')

function source(path: string): string {
  return readFileSync(resolve(uiRoot, path), 'utf8')
}

describe('brand assets', () => {
  it('wires the favicon and shared logo asset', () => {
    expect(source('index.html')).toContain('<link rel="icon" href="/favicon.ico" sizes="32x32" />')
    expect(source('src/components/BrandMark.tsx')).toContain('src="/logo.png"')
  })
})

describe('light-only appearance', () => {
  it('does not install theme persistence or dark palette overrides', () => {
    expect(source('index.html')).not.toContain('baibai-theme')
    expect(source('src/styles.css')).not.toMatch(/\.dark\s*\{/)
  })
})

describe('brand palette', () => {
  const styles = source('src/styles.css')
  const root = styles.match(/:root\s*\{(?<body>[\s\S]*?)\n\}/)?.groups?.body ?? ''

  it.each([
    ['--brand-teal', '#0db79a'],
    ['--brand-green', '#12b76a'],
    ['--brand-lime', '#95d814'],
    ['--primary-action', '#008755'],
    ['--primary-hover', '#006b43'],
    ['--primary-soft', '#e8faf0'],
    ['--accent-display', '#fb5429'],
    ['--accent-action', '#db3b14'],
    ['--accent-hover', '#b62d0c'],
    ['--accent-soft', '#fff0eb'],
    ['--canvas', '#f8faf9'],
    ['--surface', '#ffffff'],
    ['--surface-subtle', '#f2f5f3'],
    ['--border', '#e2e8e5'],
    ['--border-strong', '#d4ddd8'],
    ['--text-primary', '#111815'],
    ['--text-secondary', '#56625c'],
    ['--text-tertiary', '#75817b'],
  ])('defines %s as %s', (token, value) => {
    expect(root).toMatch(new RegExp(`${token}:\\s*${value};`, 'i'))
  })

  it('keeps status colors independent from brand and accent tokens', () => {
    for (const token of ['--positive', '--warning', '--destructive']) {
      const declaration = root.match(new RegExp(`${token}:\\s*([^;]+);`))?.[1]
      expect(declaration).toBeDefined()
      expect(declaration).not.toMatch(/var\(--(?:brand|accent)/)
    }
  })
})
