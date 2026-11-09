import { Suspense } from 'react'
import { Outlet, useLocation } from 'react-router'

import { AppShell } from './AppShell'
import { LoadingPage } from './LoadingIndicator'
import { RouteErrorBoundary } from './RouteErrorBoundary'

// Keep navigation visible on page errors; reset the boundary when the path changes.
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
