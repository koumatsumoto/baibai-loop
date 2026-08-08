import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppLayout } from './components/AppLayout'
import { PasswordGate } from './components/PasswordGate'
import { RouteErrorBoundary } from './components/RouteErrorBoundary'
import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const StocksPage = lazy(() => import('./pages/StocksPage').then((module) => ({ default: module.StocksPage })))
const ShortlistPage = lazy(() => import('./pages/ShortlistPage').then((module) => ({ default: module.ShortlistPage })))
const AssessmentPage = lazy(() => import('./pages/AssessmentPage').then((module) => ({ default: module.AssessmentPage })))
const MacroPage = lazy(() => import('./pages/MacroPage').then((module) => ({ default: module.MacroPage })))
const MacroReportPage = lazy(() => import('./pages/MacroReportPage').then((module) => ({ default: module.MacroReportPage })))
const SecurityDetailPage = lazy(() => import('./pages/SecurityDetailPage').then((module) => ({ default: module.SecurityDetailPage })))
const SystemPage = lazy(() => import('./pages/SystemPage').then((module) => ({ default: module.SystemPage })))

function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <PasswordGate>
          <RouteErrorBoundary>
            <Routes>
              <Route element={<AppLayout />}>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/macro" element={<MacroPage />} />
                <Route path="/macro/reports/:contextId" element={<MacroReportPage />} />
                <Route path="/stocks" element={<StocksPage />} />
                <Route path="/stocks/shortlist" element={<ShortlistPage />} />
                <Route path="/stocks/assessments/:assessmentId" element={<AssessmentPage />} />
                <Route path="/securities/:ticker" element={<SecurityDetailPage />} />
                <Route path="/system" element={<SystemPage />} />
              </Route>
            </Routes>
          </RouteErrorBoundary>
        </PasswordGate>
      </BrowserRouter>
    </TooltipProvider>
  )
}

export default App
