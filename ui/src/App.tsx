import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'

import { DashboardPage } from './pages/DashboardPage'
import './styles.css'

function PendingPage({ title }: { title: string }) {
  return (
    <main className="page page--message">
      <p className="eyebrow">BAIBAI-LOOP</p>
      <h1>{title}</h1>
      <p>この画面は次の実装段階で有効になります。</p>
      <Link to="/">Dashboard に戻る</Link>
    </main>
  )
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/screening" element={<PendingPage title="Screening" />} />
        <Route path="/securities/:ticker" element={<PendingPage title="Security detail" />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
