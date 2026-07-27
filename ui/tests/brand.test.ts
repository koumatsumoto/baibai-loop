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
const themeMappings = declarations(styles.match(/@theme inline\s*\{(?<body>[\s\S]*?)\n\}/)?.groups?.body ?? '')

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

interface Oklab {
  readonly lightness: number
  readonly a: number
  readonly b: number
  readonly chroma: number
  readonly hue: number
}

// Perceptual coordinates, where equal distances look equally different. Hex arithmetic
// does not: #9f6800 and #b64e10 differ by little in RGB and are plainly two colors.
function oklab({ red, green, blue }: Rgb): Oklab {
  const r = srgbToLinear(red)
  const g = srgbToLinear(green)
  const bl = srgbToLinear(blue)
  const long = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * bl)
  const medium = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * bl)
  const short = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * bl)
  const a = 1.9779984951 * long - 2.428592205 * medium + 0.4505937099 * short
  const b = 0.0259040371 * long + 0.7827717662 * medium - 0.808675766 * short
  return {
    lightness: 0.2104542553 * long + 0.793617785 * medium - 0.0040720468 * short,
    a,
    b,
    chroma: Math.hypot(a, b),
    hue: ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360,
  }
}

function perceptualDistance(first: string, second: string): number {
  const one = oklab(parseColor(first))
  const two = oklab(parseColor(second))
  return Math.hypot(one.lightness - two.lightness, one.a - two.a, one.b - two.b) * 100
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

// The colors a bare number can wear, and the colors a chart series can. Every check below
// derives its cases from these two lists, so a color added to the palette cannot be added
// to some checks and forgotten by the rest.
const VALUE_COLORS = ['--positive', '--destructive', '--profit', '--loss'] as const
const SERIES_COLORS = ['--chart-1', '--chart-2', '--chart-3'] as const

function pairs<T>(items: readonly T[]): [T, T][] {
  return items.flatMap((first, index) => items.slice(index + 1).map((second): [T, T] => [first, second]))
}

describe('brand palette', () => {
  // The two colors the mark itself owns, checked against what was measured from
  // ui/brand/logo.png rather than against a value typed in twice. Regenerating the brand
  // assets rewrites that measurement, so a logo whose lime or gold has moved fails here
  // until the palette follows the image it claims to come from.
  it.each([
    ['--brand-lime', 'lime'],
    ['--accent-display', 'gold'],
  ])('takes %s from the source logo', (token, measurement) => {
    const measured = JSON.parse(source('brand/measured-colors.json')) as Record<string, string>
    expect(measured[measurement]).toMatch(/^#[0-9a-f]{6}$/i)
    expect(tokenValue(token)).toBe(measured[measurement].toLowerCase())
  })

  // What the palette has to hold whatever the hex values become. Each color is measured
  // against the surface it actually sits on, at the AA threshold for text (4.5) and the
  // non-text threshold for focus outlines and chart marks (3.0).
  it.each([
    ...VALUE_COLORS.map((token): [string, string, number] => [token, '--surface', 4.5]),
    ['--primary-action', '--surface', 4.5],
    ['--primary-hover', '--surface', 4.5],
    ['--warning', '--surface', 4.5],
    ['--text-secondary', '--canvas', 4.5],
    ['--ring', '--canvas', 3],
    ['--chart-1', '--surface', 3],
    ['--text-tertiary', '--surface', 3],
  ])('keeps %s legible on %s', (token, background, minimum) => {
    expect(contrastRatio(tokenValue(token), tokenValue(background))).toBeGreaterThanOrEqual(minimum)
  })

  // Tailwind only emits `text-profit` / `bg-chart-2` for tokens re-exported through
  // `@theme inline`. A missing line there costs no error — the class is simply never
  // generated and the element keeps whatever color it inherited.
  it.each([...VALUE_COLORS, ...SERIES_COLORS])('exposes %s as a utility', (token) => {
    expect(themeMappings.get(`--color${token.slice(1)}`)).toBe(`var(${token})`)
  })

  it('maps no utility onto a token that no longer exists', () => {
    for (const [name, value] of themeMappings) {
      const reference = value.match(/^var\((--[\w-]+)\)$/)
      if (reference === null) continue
      const declared = tokens.has(reference[1]) || themeMappings.has(reference[1])
      expect(declared, `${name} points at a missing token`).toBe(true)
    }
  })

  it('keeps status colors independent from brand and accent tokens', () => {
    for (const token of [...VALUE_COLORS, '--warning']) {
      const declaration = tokens.get(token)
      expect(declaration).toBeDefined()
      expect(declaration).not.toMatch(/var\(--(?:brand|accent)/)
    }
  })

  // The four colors a bare number can wear. Two of them answer "which way did it move"
  // and two answer "did it make money", and a reader tells which question a number is
  // answering from its color alone — so every pair has to be far apart. Warning is absent
  // on purpose: it only ever dresses a badge or a banner that states its own meaning.
  it.each(pairs(VALUE_COLORS))('separates %s from %s', (first, second) => {
    expect(perceptualDistance(tokenValue(first), tokenValue(second))).toBeGreaterThanOrEqual(12)
  })

  // Charts stay in the green family so gold keeps meaning profit wherever it shows up.
  it('leaves gold and blue out of the chart series', () => {
    for (const token of SERIES_COLORS) {
      const { hue, chroma } = oklab(parseColor(tokenValue(token)))
      const isNeutral = chroma < 0.03
      const isGreen = hue >= 100 && hue <= 190
      expect(isNeutral || isGreen).toBe(true)
    }
  })
})
