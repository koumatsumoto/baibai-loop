import { matchPath } from 'react-router'

// Run history and step logs stay on GitHub; the serving Worker does not fetch them.
export const ACTIONS_URL =
  'https://github.com/koumatsumoto/baibai-loop/actions/workflows/cloud-daily-batch-current.yml'

export const PROPOSAL_MARKETS = {
  japan: { to: '/proposal', label: '日本株' },
  us: { to: '/us-proposal', label: '米国株' },
} as const

export function proposalMarketForPath(pathname: string) {
  return Object.values(PROPOSAL_MARKETS).find(({ to }) => matchPath(to, pathname) !== null)
}

// Saved proposals are read-only reports, not an authoring or order entry screen.
interface NavTab {
  readonly to: string
  readonly label: string
  readonly activeHref: (pathname: string) => string | undefined
}

export const NAV_TABS: readonly NavTab[] = [
  { to: '/', label: '資産状況', activeHref: (pathname) => pathname === '/' ? '/' : undefined },
  { to: PROPOSAL_MARKETS.japan.to, label: '売買提案', activeHref: (pathname) => proposalMarketForPath(pathname)?.to },
]
