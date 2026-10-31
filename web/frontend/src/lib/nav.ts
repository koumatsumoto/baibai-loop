// Run history and step logs stay on GitHub; the serving Worker does not fetch them.
export const ACTIONS_URL =
  'https://github.com/koumatsumoto/baibai-loop/actions/workflows/cloud-daily-batch.yml'

// Saved proposals are read-only reports, not an authoring or order entry screen.
export interface NavTab {
  readonly to: string
  readonly label: string
  readonly match: (pathname: string) => boolean
}

export const NAV_TABS: readonly NavTab[] = [
  { to: '/', label: 'Dashboard', match: (pathname) => pathname === '/' },
  { to: '/tasks', label: 'Tasks', match: (pathname) => pathname === '/tasks' },
  { to: '/proposal', label: '売買提案', match: (pathname) => pathname === '/proposal' },
]
