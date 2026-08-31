import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppLayout } from './components/AppLayout'
import { PasswordGate } from './components/PasswordGate'
import { RouteErrorBoundary } from './components/RouteErrorBoundary'
import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const StocksPage = lazy(() => import('./pages/StocksPage').then((module) => ({ default: module.StocksPage })))
const ResearchTriagePage = lazy(() => import('./pages/ResearchTriagePage').then((module) => ({ default: module.ResearchTriagePage })))
const CapitalAllocationAssessmentPage = lazy(() => import('./pages/CapitalAllocationAssessmentPage').then((module) => ({ default: module.CapitalAllocationAssessmentPage })))
const MacroPage = lazy(() => import('./pages/MacroPage').then((module) => ({ default: module.MacroPage })))
const MacroReportPage = lazy(() => import('./pages/MacroReportPage').then((module) => ({ default: module.MacroReportPage })))
const SecurityDetailPage = lazy(() => import('./pages/SecurityDetailPage').then((module) => ({ default: module.SecurityDetailPage })))
const TasksPage = lazy(() => import('./pages/TasksPage').then((module) => ({ default: module.TasksPage })))

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
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/research-triage" element={<ResearchTriagePage />} />
                <Route path="/stocks/capital-allocation-assessments/:capitalAllocationAssessmentId" element={<CapitalAllocationAssessmentPage />} />
                <Route path="/securities/:ticker" element={<SecurityDetailPage />} />
              </Route>
            </Routes>
          </RouteErrorBoundary>
        </PasswordGate>
      </BrowserRouter>
    </TooltipProvider>
  )
}

export default App
