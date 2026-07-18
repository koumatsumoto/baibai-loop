import { Link, NavLink } from 'react-router-dom'

export function AppShell() {
  return (
    <header className="topbar">
      <Link className="brand" to="/"><span className="brand__mark">BL</span><span>Baibai-Loop</span></Link>
      <nav>
        <NavLink to="/" end>Dashboard</NavLink>
        <NavLink to="/screening">Screening</NavLink>
      </nav>
      <span className="readonly">READ ONLY</span>
    </header>
  )
}
