import { Suspense } from 'react'
import { Outlet } from 'react-router'

import { AppShell } from './AppShell'
import { LoadingPage } from './LoadingIndicator'

// The shell belongs to the route tree, not to each page. Mounting it once above the
// outlet keeps it on screen through lazy-chunk loading, route changes and per-page
// loading states. Authentication can still remount the layout.
export function AppLayout() {
  return (
    <>
      <AppShell />
      <Suspense fallback={<LoadingPage label="Baibai Loop を読み込んでいます" />}>
        <Outlet />
      </Suspense>
    </>
  )
}
