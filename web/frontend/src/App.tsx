import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppLayout } from './components/AppLayout'
import { PasswordGate } from './components/PasswordGate'
import { RouteErrorBoundary } from './components/RouteErrorBoundary'
import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const StocksPage = lazy(() => import('./pages/StocksPage').then((module) => ({ default: module.StocksPage })))
const CandidateExplorationPage = lazy(() => import('./pages/CandidateExplorationPage').then((module) => ({ default: module.CandidateExplorationPage })))
const ResearchTriagePage = lazy(() => import('./pages/ResearchTriagePage').then((module) => ({ default: module.ResearchTriagePage })))
const CapitalAllocationAssessmentPage = lazy(() => import('./pages/CapitalAllocationAssessmentPage').then((module) => ({ default: module.CapitalAllocationAssessmentPage })))
const MarketContextPage = lazy(() => import('./pages/MarketContextPage').then((module) => ({ default: module.MarketContextPage })))
const AssessmentPage = lazy(() => import('./pages/AssessmentPage').then((module) => ({ default: module.AssessmentPage })))
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
                <Route path="/market-context" element={<MarketContextPage />} />
                <Route path="/market-context/assessments/:contextId" element={<AssessmentPage />} />
                <Route path="/stocks" element={<StocksPage />} />
                <Route path="/stocks/explore" element={<CandidateExplorationPage />} />
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
