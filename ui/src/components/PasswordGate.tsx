import { Fragment, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { KeyRound } from 'lucide-react'

import { setViewPassword, subscribeAuthRequired } from '../api/auth'
import { Button } from './ui/button'
import { Input } from './ui/input'

interface PasswordGateProps {
  children: ReactNode
}

// Renders its children normally until a `/api/` request answers 401. On that signal it
// takes the full screen with a single password form; submitting stores the password and
// remounts the children (bumped key) so their data fetch retries with the new header.
// Without a 401 this never activates, so local operation is unchanged.
export function PasswordGate({ children }: PasswordGateProps) {
  const [locked, setLocked] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [value, setValue] = useState('')

  useEffect(() => subscribeAuthRequired(() => setLocked(true)), [])

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const password = value.trim()
    if (password === '') return
    setViewPassword(password)
    setValue('')
    setLocked(false)
    setReloadKey((key) => key + 1)
  }

  if (locked) {
    return (
      <main className="grid min-h-screen place-items-center bg-background px-6">
        <form className="grid w-full max-w-sm gap-4 rounded-xl border bg-card p-6 shadow-sm" onSubmit={handleSubmit}>
          <div className="grid gap-1.5">
            <div className="flex items-center gap-2 font-semibold tracking-tight">
              <KeyRound className="size-4 text-muted-foreground" aria-hidden="true" />
              <span>閲覧パスワード</span>
            </div>
            <p className="text-sm text-muted-foreground">閲覧を続けるにはパスワードを入力してください。</p>
          </div>
          <Input
            autoComplete="current-password"
            autoFocus
            onChange={(event) => setValue(event.target.value)}
            type="password"
            value={value}
          />
          <Button disabled={value.trim() === ''} type="submit">続行</Button>
        </form>
      </main>
    )
  }

  return <Fragment key={reloadKey}>{children}</Fragment>
}
