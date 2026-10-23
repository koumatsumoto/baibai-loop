import { Link, useLocation } from 'react-router'
import { cn } from '../lib/utils'

export function ResearchNav() {
  const { pathname } = useLocation()
  const active = pathname.startsWith('/market-context') ? 'market' : pathname === '/stocks/explore' || pathname.startsWith('/securities/') ? 'explore' : 'proposals'
  return <nav aria-label="調査・提案の内容" className="flex gap-1 rounded-lg bg-muted p-1 text-sm">
    {[
      { key: 'proposals', to: '/stocks', label: '提案' },
      { key: 'explore', to: '/stocks/explore', label: '候補探索' },
      { key: 'market', to: '/market-context', label: '市場環境' },
    ].map(item => <Link aria-current={active === item.key ? 'page' : undefined} className={cn('flex min-h-11 flex-1 items-center justify-center rounded-md px-3 py-2.5 text-center font-medium transition-colors hover:bg-background/70', active === item.key && 'bg-background text-foreground shadow-sm')} key={item.key} to={item.to}>{item.label}</Link>)}
  </nav>
}
