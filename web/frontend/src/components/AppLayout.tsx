import { Suspense } from 'react'
import { Outlet, useLocation } from 'react-router'

import { AppShell } from './AppShell'
import { LoadingPage } from './LoadingIndicator'
import { RouteErrorBoundary } from './RouteErrorBoundary'

// The shell belongs to the route tree, not to each page. Mounting it once above the
// outlet keeps it on screen through lazy-chunk loading, route changes and per-page
// loading states. A failed page leaves navigation available; changing pathname
// resets its boundary, including browser Back/Forward. Authentication can still
// remount the layout.
export function AppLayout() {
  const { pathname } = useLocation()

  return (
    <>
      <AppShell />
      <RouteErrorBoundary key={pathname}>
        <Suspense fallback={<LoadingPage label="Baibai Loop を読み込んでいます" />}>
          <Outlet />
        </Suspense>
      </RouteErrorBoundary>
    </>
  )
}
