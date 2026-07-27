import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { LoadingPage } from './components/LoadingIndicator'
import { PasswordGate } from './components/PasswordGate'
import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const StocksPage = lazy(() => import('./pages/StocksPage').then((module) => ({ default: module.StocksPage })))
const ShortlistPage = lazy(() => import('./pages/ShortlistPage').then((module) => ({ default: module.ShortlistPage })))
const MacroPage = lazy(() => import('./pages/MacroPage').then((module) => ({ default: module.MacroPage })))
const MacroReportPage = lazy(() => import('./pages/MacroReportPage').then((module) => ({ default: module.MacroReportPage })))
const SecurityDetailPage = lazy(() => import('./pages/SecurityDetailPage').then((module) => ({ default: module.SecurityDetailPage })))

function RouteLoading() {
  return <LoadingPage label="Baibai App を読み込んでいます" shell={false} />
}

function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <PasswordGate>
          <Suspense fallback={<RouteLoading />}>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/macro" element={<MacroPage />} />
              <Route path="/macro/reports/:contextId" element={<MacroReportPage />} />
              <Route path="/stocks" element={<StocksPage />} />
              <Route path="/stocks/shortlist" element={<ShortlistPage />} />
              <Route path="/securities/:ticker" element={<SecurityDetailPage />} />
            </Routes>
          </Suspense>
        </PasswordGate>
      </BrowserRouter>
    </TooltipProvider>
  )
}

export default App
