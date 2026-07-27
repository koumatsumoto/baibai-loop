import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const uiRoot = resolve(import.meta.dirname, '..')

function source(path: string): string {
  return readFileSync(resolve(uiRoot, path), 'utf8')
}

interface Rgb {
  readonly red: number
  readonly green: number
  readonly blue: number
}

const styles = source('src/styles.css')
const rootBlock = styles.match(/:root\s*\{(?<body>[\s\S]*?)\n\}/)?.groups?.body ?? ''

function declarations(css: string): ReadonlyMap<string, string> {
  const entries = new Map<string, string>()
  for (const [, name, value] of css.matchAll(/(--[\w-]+):\s*([^;]+);/g)) {
    entries.set(name, value.trim())
  }
  return entries
}

const tokens = declarations(rootBlock)

// A token's value only means something once its var() chain is followed: --chart-1 reads
// `var(--primary-action)`, and it is the green at the end of that chain a viewer sees.
function tokenValue(name: string, seen: readonly string[] = []): string {
  const value = tokens.get(name)
  if (value === undefined) throw new Error(`token ${name} is not declared in :root`)
  if (seen.includes(name)) throw new Error(`token ${name} resolves in a cycle`)
  const reference = value.match(/^var\((--[\w-]+)\)$/)
  if (reference === null) return value
  return tokenValue(reference[1], [...seen, name])
}

function srgbToLinear(channel: number): number {
  const ratio = channel / 255
  return ratio <= 0.04045 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4
}

function linearToSrgb(channel: number): number {
  const clamped = Math.min(1, Math.max(0, channel))
  const encoded = clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * clamped ** (1 / 2.4) - 0.055
  return Math.round(encoded * 255)
}

// CSS Color 4 oklch() -> sRGB. Status colors are stated perceptually, so the checks below
// have to reach the same pixels the browser paints.
function oklchToRgb(lightness: number, chroma: number, hue: number): Rgb {
  const radians = (hue * Math.PI) / 180
  const a = chroma * Math.cos(radians)
  const b = chroma * Math.sin(radians)
  const long = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const medium = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const short = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
  return {
    red: linearToSrgb(4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short),
    green: linearToSrgb(-1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short),
    blue: linearToSrgb(-0.0041960863 * long - 0.7034186147 * medium + 1.707614701 * short),
  }
}

function parseColor(value: string): Rgb {
  const hex = value.match(/^#([0-9a-f]{6})$/i)
  if (hex !== null) {
    const channels = Number.parseInt(hex[1], 16)
    return { red: (channels >> 16) & 0xff, green: (channels >> 8) & 0xff, blue: channels & 0xff }
  }
  const oklch = value.match(/^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)$/i)
  if (oklch === null) throw new Error(`cannot read a color from "${value}"`)
  return oklchToRgb(Number(oklch[1]), Number(oklch[2]), Number(oklch[3]))
}

function relativeLuminance({ red, green, blue }: Rgb): number {
  return 0.2126 * srgbToLinear(red) + 0.7152 * srgbToLinear(green) + 0.0722 * srgbToLinear(blue)
}

function contrastRatio(foreground: string, background: string): number {
  const first = relativeLuminance(parseColor(foreground))
  const second = relativeLuminance(parseColor(background))
  const [lighter, darker] = first > second ? [first, second] : [second, first]
  return (lighter + 0.05) / (darker + 0.05)
}

describe('brand assets', () => {
  it('wires the favicon, home-screen icon and shared logo asset', () => {
    expect(source('index.html')).toContain('<link rel="icon" href="/favicon.ico" sizes="32x32" />')
    expect(source('index.html')).toContain('<link rel="apple-touch-icon" href="/apple-touch-icon.png" />')
    expect(source('index.html')).toContain('<link rel="manifest" href="/manifest.webmanifest" />')
    expect(source('src/components/BrandMark.tsx')).toContain('src="/logo.png"')
  })

  it('provides installable Android home-screen metadata', () => {
    const manifest = JSON.parse(source('public/manifest.webmanifest')) as {
      display: string
      start_url: string
      icons: { sizes: string }[]
    }
    expect(manifest.display).toBe('standalone')
    expect(manifest.start_url).toBe('/')
    expect(manifest.icons.map((icon) => icon.sizes)).toEqual(['192x192', '512x512'])
  })
})

describe('light-only appearance', () => {
  it('does not install theme persistence or dark palette overrides', () => {
    expect(source('index.html')).not.toContain('baibai-theme')
    expect(styles).not.toMatch(/\.dark\s*\{/)
  })
})

describe('brand palette', () => {
  // The two colors the mark itself owns. `tools/generate_brand_assets.py` measures them
  // from ui/brand/logo.png, so a logo whose lime or gold has moved fails here until the
  // palette follows the image it claims to come from.
  it.each([
    ['--brand-lime', '#d9ef37'],
    ['--accent-display', '#feae00'],
  ])('takes %s from the source logo', (token, value) => {
    expect(tokenValue(token)).toBe(value)
  })

  // What the palette has to hold whatever the hex values become. Each color is measured
  // against the surface it actually sits on, at the AA threshold for text (4.5) and the
  // non-text threshold for focus outlines and chart marks (3.0).
  it.each([
    ['--primary-action', '--surface', 4.5],
    ['--primary-hover', '--surface', 4.5],
    ['--warning', '--surface', 4.5],
    ['--positive', '--surface', 4.5],
    ['--destructive', '--surface', 4.5],
    ['--text-secondary', '--canvas', 4.5],
    ['--ring', '--canvas', 3],
    ['--chart-1', '--surface', 3],
    ['--text-tertiary', '--surface', 3],
  ])('keeps %s legible on %s', (token, background, minimum) => {
    expect(contrastRatio(tokenValue(token), tokenValue(background))).toBeGreaterThanOrEqual(minimum)
  })

  it('keeps status colors independent from brand and accent tokens', () => {
    for (const token of ['--positive', '--warning', '--destructive']) {
      const declaration = tokens.get(token)
      expect(declaration).toBeDefined()
      expect(declaration).not.toMatch(/var\(--(?:brand|accent)/)
    }
  })
})
