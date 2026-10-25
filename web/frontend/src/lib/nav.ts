// Run history and step logs stay on GitHub; the serving Worker does not fetch them.
export const ACTIONS_URL =
  'https://github.com/koumatsumoto/baibai-loop/actions/workflows/cloud-daily-batch.yml'

// The navigation follows the reader's work; analysis domains are evidence within Research.
export interface NavTab {
  readonly to: string
  readonly label: string
  readonly match: (pathname: string) => boolean
}

export const NAV_TABS: readonly NavTab[] = [
  { to: '/', label: 'Dashboard', match: (pathname) => pathname === '/' },
  {
    to: '/stocks',
    label: '投資判断',
    match: (pathname) =>
      pathname === '/market-context' || pathname.startsWith('/market-context/') ||
      pathname === '/stocks' ||
      pathname.startsWith('/stocks/') ||
      pathname.startsWith('/securities/'),
  },
  { to: '/tasks', label: 'Tasks', match: (pathname) => pathname === '/tasks' },
]
