import { readdirSync, readFileSync } from 'node:fs'
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

// CSS Color 4 oklch() -> linear-light sRGB, before clamping. A channel outside [0, 1] means
// sRGB cannot hold the color, so the browser gamut-maps it to something other than the pixels
// measured here.
function oklchToLinear(lightness: number, chroma: number, hue: number): readonly number[] {
  const radians = (hue * Math.PI) / 180
  const a = chroma * Math.cos(radians)
  const b = chroma * Math.sin(radians)
  const long = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const medium = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const short = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
    -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
    -0.0041960863 * long - 0.7034186147 * medium + 1.707614701 * short,
  ]
}

// Status colors are stated perceptually, so the checks below have to reach the same pixels the
// browser paints.
function oklchToRgb(lightness: number, chroma: number, hue: number): Rgb {
  const [red, green, blue] = oklchToLinear(lightness, chroma, hue)
  return { red: linearToSrgb(red), green: linearToSrgb(green), blue: linearToSrgb(blue) }
}

const OKLCH = /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)$/i

function fitsInSrgb(value: string): boolean {
  const oklch = value.match(OKLCH)
  if (oklch === null) return true
  return oklchToLinear(Number(oklch[1]), Number(oklch[2]), Number(oklch[3])).every(
    (channel) => channel >= -0.0005 && channel <= 1.0005,
  )
}

function parseColor(value: string): Rgb {
  const hex = value.match(/^#([0-9a-f]{6})$/i)
  if (hex !== null) {
    const channels = Number.parseInt(hex[1], 16)
    return { red: (channels >> 16) & 0xff, green: (channels >> 8) & 0xff, blue: channels & 0xff }
  }
  const oklch = value.match(OKLCH)
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

function contrastOf(foreground: Rgb, background: Rgb): number {
  const first = relativeLuminance(foreground)
  const second = relativeLuminance(background)
  const [lighter, darker] = first > second ? [first, second] : [second, first]
  return (lighter + 0.05) / (darker + 0.05)
}

function contrastRatio(foreground: string, background: string): number {
  return contrastOf(parseColor(foreground), parseColor(background))
}

// What a partly transparent text color actually becomes once it is drawn over its background.
function flatten(foreground: string, background: string, alpha: number): Rgb {
  const front = parseColor(foreground)
  const back = parseColor(background)
  const blend = (over: number, under: number): number => Math.round(over * alpha + under * (1 - alpha))
  return {
    red: blend(front.red, back.red),
    green: blend(front.green, back.green),
    blue: blend(front.blue, back.blue),
  }
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

// Colors measured from web/frontend/brand/logo.png, keyed by the palette entry each color belongs to.
const measuredColors = JSON.parse(source('brand/measured-colors.json')) as Record<string, string>

describe('brand palette', () => {
  // The colors the mark itself owns, checked against the image rather than against values typed
  // in twice. Compared perceptually, not byte for byte: re-rendering the same artwork moves a
  // channel by a step whichever way the color is read out of it, and a step is at most 0.36 on
  // this scale while the palette keeps its own colors 12 apart. A bar of 0.5 lets a re-render
  // through and still fails a logo whose color has actually moved.
  it.each(Object.entries(measuredColors))('takes %s from the source logo', (token, measured) => {
    expect(measured).toMatch(/^#[0-9a-f]{6}$/i)
    expect(perceptualDistance(tokenValue(token), measured)).toBeLessThan(0.5)
  })

  // Both directions of that pairing. A measured color the palette does not declare fails above,
  // because tokenValue throws for a name :root has not declared. A brand token nothing measures
  // is the other half: a value with no image behind it, which is how a token outlives the mark
  // it was named for.
  it('leaves no brand token unmeasured', () => {
    const declared = [...tokens.keys()].filter((name) => name.startsWith('--brand-'))
    expect(declared.sort()).toEqual(Object.keys(measuredColors).sort())
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

  // A token dropped from the palette leaves its references behind as `var(--gone)`, which
  // costs no error: the declaration is simply discarded and the element keeps whatever it
  // inherited. The palette's own aliases are followed as well as the utility layer, since
  // an alias is one more place a name outlives the color it stood for.
  it('points no declaration at a token that no longer exists', () => {
    for (const [name, value] of [...themeMappings, ...tokens]) {
      const reference = value.match(/^var\((--[\w-]+)\)$/)
      if (reference === null) continue
      const declared = tokens.has(reference[1]) || themeMappings.has(reference[1])
      expect(declared, `${name} points at a missing token`).toBe(true)
    }
  })

  // The mark's own green is the palette's record of the artwork, not a color to paint with: it
  // sits under the 3:1 a state indicator needs, and the greens that carry interaction are
  // darkened from it instead. Since a new logo moves this value, a component reaching for it
  // would let the artwork decide an affordance's contrast.
  //
  // What this holds is the reachable half — the token and the `:root` aliases that resolve to it,
  // by utility, by var() and by the `bg-(--x)` shorthand. Tailwind accepts enough spellings that
  // chasing the rest would cost more than the rule is worth, so the palette states it as well.
  // The threshold is asserted rather than described, so a mark whose green does clear 3:1 fails
  // here and the rule gets revisited instead of outliving its reason.
  it('leaves the mark’s own green out of the components', () => {
    const markGreen = tokenValue('--brand-green').toLowerCase()
    expect(contrastRatio(markGreen, tokenValue('--surface'))).toBeLessThan(3)
    const aliases = [...tokens.keys()].filter((name) => tokenValue(name).toLowerCase() === markGreen)
    for (const name of aliases) {
      expect(themeMappings.has(`--color${name.slice(1)}`), `${name} is a utility`).toBe(false)
    }
    const painted = componentSources((entry) => entry !== 'styles.css').filter((text) =>
      aliases.some((name) => text.includes(`var(${name})`) || text.includes(`(${name})`)),
    )
    expect(painted).toEqual([])
  })

  // Tailwind emits a rule for `bg-primary-soft` only because `--color-primary-soft` is
  // re-exported through `@theme inline`. Painting with a name that is not costs no error: the
  // class matches nothing and the element keeps whatever it inherited, so dropping a mapping
  // empties every class string that named it without a word. Only names whose first segment
  // belongs to the palette are checked, which leaves Tailwind's own scale (text-sm, border-b)
  // alone.
  it('paints with no color the theme layer does not export', () => {
    // Which names belong to the palette is read from the palette, not from the utility layer this
    // checks: taking it from `@theme` would let the last mapping under a name take the check with
    // it when it goes.
    const roots = new Set([...tokens.keys()].map((name) => name.slice(2).split('-')[0]))
    const missing = new Set<string>()
    for (const text of componentSources()) {
      for (const [, name] of text.matchAll(
        /\b(?:bg|text|border|ring|fill|stroke|from|via|to|outline|divide|caret|accent|decoration|placeholder)-([a-z][\w-]*)/g,
      )) {
        if (roots.has(name.split('-')[0]) && !themeMappings.has(`--color-${name}`)) missing.add(name)
      }
    }
    expect([...missing]).toEqual([])
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

// The three families that fill a badge or a banner. Each owns an opaque `-surface` to paint
// with and an opaque `-ink` to write on it, because a translucent fill has no fixed contrast:
// the ratio depends on whatever is behind it, and it changes when a page moves the badge from
// a white card to a tinted one.
const STATUS_FAMILIES = ['--positive', '--warning', '--destructive'] as const
const STATUS_NAMES = 'positive|warning|destructive'

// Everything under src/ that can name a color, with the palette itself excludable: a token
// has to be declared somewhere, so a check about where a color is *used* has to skip it.
function componentSources(include: (entry: string) => boolean = () => true): readonly string[] {
  const root = resolve(uiRoot, 'src')
  return readdirSync(root, { encoding: 'utf8', recursive: true })
    .filter((entry) => /\.(?:tsx?|css)$/.test(entry) && include(entry))
    .map((entry) => readFileSync(resolve(root, entry), 'utf8'))
}

describe('status surfaces', () => {
  it.each(STATUS_FAMILIES)('keeps the ink on %s-surface at AA', (family) => {
    const ratio = contrastRatio(tokenValue(`${family}-ink`), tokenValue(`${family}-surface`))
    expect(ratio).toBeGreaterThanOrEqual(4.5)
  })

  // A measured ratio is only the rendered one while both colors fit in sRGB. Raising chroma to
  // force a pair apart would pass the check above and leave the browser painting a color this
  // file never measured. The mark colors above sit a hair outside sRGB — a channel at -0.009 —
  // and there Chrome's gamut mapping lands on the same bytes this file clamps to, so their
  // ratios are the rendered ones. That agreement is not owed for a larger overshoot, which is
  // what this keeps the fill and its ink away from.
  it.each(STATUS_FAMILIES)('states %s-surface and its ink inside sRGB', (family) => {
    expect(fitsInSrgb(tokenValue(`${family}-surface`))).toBe(true)
    expect(fitsInSrgb(tokenValue(`${family}-ink`))).toBe(true)
  })

  // Three pale fills are scanned in one list, and near white sRGB leaves so little chroma that
  // neighbouring hues collapse into the same tint. The bar is what the translucent fills these
  // replaced left between their closest pair (3.37, green against amber), rounded down: the
  // opaque set may not read as flatter than what it replaced. Distance is measured between the
  // fills, since that is what a reader compares before reading either badge.
  it.each(pairs(STATUS_FAMILIES))('separates the %s fill from the %s fill', (first, second) => {
    const apart = perceptualDistance(tokenValue(`${first}-surface`), tokenValue(`${second}-surface`))
    expect(apart).toBeGreaterThanOrEqual(3)
  })

  it.each(STATUS_FAMILIES)('exposes %s-surface and its ink as utilities', (family) => {
    expect(themeMappings.get(`--color${family.slice(1)}-surface`)).toBe(`var(${family}-surface)`)
    expect(themeMappings.get(`--color${family.slice(1)}-ink`)).toBe(`var(${family}-ink)`)
  })

  // Every shape Tailwind accepts for "this status color, but see-through": the plain alpha,
  // the arbitrary alpha, the variable shorthand, and an arbitrary value that names the token.
  // The opaque hover mixes are deliberately not matched — they name `-surface` and `-ink`.
  it('paints no status color as a translucent fill', () => {
    const patterns = [
      new RegExp(`\\bbg-(?:${STATUS_NAMES}|profit|loss)(?:-surface|-ink)?/(?:\\d+|\\[[^\\]]*\\])`, 'g'),
      new RegExp(`\\bbg-\\(--(?:${STATUS_NAMES}|profit|loss)\\)`, 'g'),
      new RegExp(`\\bbg-\\[[^\\]]*var\\(--(?:${STATUS_NAMES}|profit|loss)\\)[^\\]]*\\]`, 'g'),
    ]
    const found = componentSources().flatMap((text) =>
      patterns.flatMap((pattern) => [...text.matchAll(pattern)].map(([used]) => used)),
    )
    expect(found).toEqual([])
  })

  // The family color is a mark color, not a text color for its own surface: --warning on
  // --warning-surface is 4.4 and --destructive on its own 4.1. This catches the pair written
  // into one class string; a fill on a parent and the text on a child are not visible to a
  // regex, which is why the palette states the rule as well.
  it('writes on a status surface with that family ink', () => {
    const pattern = new RegExp(
      `\\bbg-(${STATUS_NAMES})-surface\\b[^"'\`]*\\btext-\\1\\b(?!-)|\\btext-(${STATUS_NAMES})\\b(?!-)[^"'\`]*\\bbg-\\2-surface\\b`,
      'g',
    )
    const found = componentSources().flatMap((text) =>
      [...text.matchAll(pattern)].map(([used]) => used),
    )
    expect(found).toEqual([])
  })

  // A dimmed status text keeps its own family's surface underneath, so the alpha it is written
  // at is measurable — and has to clear AA at that alpha, not at full strength.
  it('keeps a dimmed status ink legible on its own surface', () => {
    const dimmed = new RegExp(`\\btext-(${STATUS_NAMES})-ink/(\\d+)`, 'g')
    const uses = componentSources().flatMap((text) => [...text.matchAll(dimmed)])
    for (const [, family, alpha] of uses) {
      const drawn = flatten(
        tokenValue(`--${family}-ink`),
        tokenValue(`--${family}-surface`),
        Number(alpha) / 100,
      )
      expect(
        contrastOf(drawn, parseColor(tokenValue(`--${family}-surface`))),
        `text-${family}-ink/${alpha}`,
      ).toBeGreaterThanOrEqual(4.5)
    }
  })
})
