import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppLayout } from './components/AppLayout'
import { PasswordGate } from './components/PasswordGate'
import { RouteErrorBoundary } from './components/RouteErrorBoundary'
import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const ResearchPage = lazy(() => import('./pages/ResearchPage').then((module) => ({ default: module.ResearchPage })))
const CandidateExplorationPage = lazy(() => import('./pages/CandidateExplorationPage').then((module) => ({ default: module.CandidateExplorationPage })))
const InvestmentProposalPage = lazy(() => import('./pages/InvestmentProposalPage').then((module) => ({ default: module.InvestmentProposalPage })))
const StockAssessmentPage = lazy(() => import('./pages/StockAssessmentPage').then((module) => ({ default: module.StockAssessmentPage })))
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
                <Route path="/stocks" element={<ResearchPage />} />
                <Route path="/stocks/explore" element={<CandidateExplorationPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/stocks/investment-proposals/:proposalId" element={<InvestmentProposalPage />} />
                <Route path="/stocks/stock-assessments/:assessmentId" element={<StockAssessmentPage />} />
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
