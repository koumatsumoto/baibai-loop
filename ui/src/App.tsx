import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const ScreeningPage = lazy(() => import('./pages/ScreeningPage').then((module) => ({ default: module.ScreeningPage })))
const SecurityDetailPage = lazy(() => import('./pages/SecurityDetailPage').then((module) => ({ default: module.SecurityDetailPage })))

function RouteLoading() {
  return (
    <main className="grid min-h-screen place-items-center bg-background px-6 text-center">
      <p className="text-sm font-medium text-muted-foreground">Baibai-Loop を読み込んでいます…</p>
    </main>
  )
}

function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <Suspense fallback={<RouteLoading />}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/screening" element={<ScreeningPage />} />
            <Route path="/securities/:ticker" element={<SecurityDetailPage />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </TooltipProvider>
  )
}

export default App
