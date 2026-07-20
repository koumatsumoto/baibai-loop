import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'

import { Button } from './ui/button'
import { applyTheme, readStoredTheme, resolveInitialTheme, storeTheme, type Theme } from '../lib/theme'

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(resolveInitialTheme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Track OS changes only while the viewer has not overridden the theme manually.
  useEffect(() => {
    if (readStoredTheme() !== null) return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (event: MediaQueryListEvent) => setTheme(event.matches ? 'dark' : 'light')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [theme])

  const toggle = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    storeTheme(next)
    setTheme(next)
  }

  return (
    <Button aria-label="表示テーマを切り替える" onClick={toggle} size="icon-sm" variant="ghost">
      {theme === 'dark' ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
    </Button>
  )
}
