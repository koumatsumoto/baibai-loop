import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppLayout } from './components/AppLayout'
import { PasswordGate } from './components/PasswordGate'
import { TooltipProvider } from './components/ui/tooltip'
import './styles.css'

const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const TasksPage = lazy(() => import('./pages/TasksPage').then((module) => ({ default: module.TasksPage })))
const ProposalPage = lazy(() => import('./pages/ProposalPage').then((module) => ({ default: module.ProposalPage })))

function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <PasswordGate>
          <Routes>
            <Route element={<AppLayout />}>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/proposal" element={<ProposalPage />} />
              <Route path="*" element={<p className="p-6">この画面は廃止しました。上のナビゲーションをご利用ください。</p>} />
            </Route>
          </Routes>
        </PasswordGate>
      </BrowserRouter>
    </TooltipProvider>
  )
}

export default App
