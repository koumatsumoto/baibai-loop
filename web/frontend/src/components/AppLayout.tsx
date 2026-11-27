import { Suspense } from 'react'
import { Link, Outlet, useLocation } from 'react-router'

import { PROPOSAL_MARKETS, proposalMarketForPath } from '../lib/nav'
import { AppShell } from './AppShell'
import { LoadingPage } from './LoadingIndicator'
import { RouteErrorBoundary } from './RouteErrorBoundary'

function ProposalMarketNavigation({ current }: { current: string }) {
  return (
    <div className="mx-auto max-w-[1280px] px-4 pt-4 sm:px-6 lg:px-8">
      <nav aria-label="売買提案の市場" className="grid w-full max-w-64 grid-cols-2 gap-1 sm:w-56">
        {Object.values(PROPOSAL_MARKETS).map(({ to, label }) => (
          <Link
            aria-current={current === to ? 'page' : undefined}
            className="route-choice flex min-h-11 items-center justify-center gap-1 px-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            key={to}
            to={to}
          >
            {label}
          </Link>
        ))}
      </nav>
    </div>
  )
}

// Keep navigation visible on page errors; reset the boundary when the path changes.
export function AppLayout() {
  const { pathname } = useLocation()
  const market = proposalMarketForPath(pathname)

  return (
    <>
      <AppShell />
      {market && <ProposalMarketNavigation current={market.to} />}
      <RouteErrorBoundary key={pathname}>
        <Suspense fallback={<LoadingPage label="Baibai Loop を読み込んでいます" />}>
          <Outlet />
        </Suspense>
      </RouteErrorBoundary>
    </>
  )
}
