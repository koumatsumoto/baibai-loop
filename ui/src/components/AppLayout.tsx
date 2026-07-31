import { Suspense } from 'react'
import { Outlet } from 'react-router'

import { AppShell } from './AppShell'
import { LoadingPage } from './LoadingIndicator'

// The shell belongs to the route tree, not to each page. Mounting it once above the
// outlet keeps it on screen through lazy-chunk loading, route changes and per-page
// loading states — so its freshness fetch runs once per session instead of once per
// state a page passes through, and a new page cannot be written without it.
export function AppLayout() {
  return (
    <>
      <AppShell />
      <Suspense fallback={<LoadingPage label="Baibai App を読み込んでいます" />}>
        <Outlet />
      </Suspense>
    </>
  )
}
