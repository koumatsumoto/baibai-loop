// The three top-level tabs. A tab is "active" for its own routes and for the
// tab-less detail routes that belong to it (report detail under Macro, shortlist
// and security detail under Stocks), so the header reflects the current section.
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
