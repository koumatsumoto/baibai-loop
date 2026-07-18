import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { DashboardPage } from './pages/DashboardPage'
import { ScreeningPage } from './pages/ScreeningPage'
import { SecurityDetailPage } from './pages/SecurityDetailPage'
import './styles.css'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/screening" element={<ScreeningPage />} />
        <Route path="/securities/:ticker" element={<SecurityDetailPage />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
