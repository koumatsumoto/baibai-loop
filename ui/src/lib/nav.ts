// Run history and step logs stay on GitHub: reading them needs a token the
// serving Worker deliberately does not hold.
export const ACTIONS_URL =
  'https://github.com/koumatsumoto/baibai-loop/actions/workflows/cloud-daily-batch.yml'

// The three top-level tabs. A tab is "active" for its own routes and for the
// tab-less detail routes that belong to it (report detail under Macro, shortlist
// and security detail under Stocks), so the header reflects the current section.
// /system is not a tab: it is operational state, reached from the gear menu.
export interface NavTab {
  readonly to: string
  readonly label: string
  readonly match: (pathname: string) => boolean
}

export const NAV_TABS: readonly NavTab[] = [
  { to: '/', label: 'Dashboard', match: (pathname) => pathname === '/' },
  {
    to: '/macro',
    label: 'Macro',
    match: (pathname) => pathname === '/macro' || pathname.startsWith('/macro/'),
  },
  {
    to: '/stocks',
    label: 'Stocks',
    match: (pathname) =>
      pathname === '/stocks' ||
      pathname.startsWith('/stocks/') ||
      pathname.startsWith('/securities/'),
  },
]
