import { Fragment, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { KeyRound } from 'lucide-react'

import { setViewPassword, subscribeAuthRequired } from '../api/auth'
import { noticeForAuthRequired, resolveViewPasswordSubmit } from '../lib/password'
import { BrandMark } from './BrandMark'
import { Button } from './ui/button'
import { Input } from './ui/input'

interface PasswordFormProps {
  value: string
  notice: string | null
  onValueChange: (value: string) => void
  onSubmit: (event: FormEvent) => void
}

// Presentational full-screen password form. `notice` surfaces a validation error or the
// "password rejected" state so a repeat prompt is distinguishable from the first one.
export function PasswordForm({ value, notice, onValueChange, onSubmit }: PasswordFormProps) {
  return (
    <main className="grid min-h-screen place-items-center bg-background px-6">
      <form className="grid w-full max-w-sm gap-5 rounded-2xl border bg-card p-6 shadow-sm" onSubmit={onSubmit}>
        <div className="grid gap-4">
          <div className="flex items-center gap-2.5 font-semibold tracking-tight">
            <BrandMark />
            <span>Baibai App</span>
          </div>
          <div className="grid gap-1.5">
            <div className="flex items-center gap-2 font-medium">
              <KeyRound className="size-4 text-primary" aria-hidden="true" />
              <span>閲覧パスワード</span>
            </div>
            <p className="text-sm text-muted-foreground">閲覧を続けるにはパスワードを入力してください。</p>
          </div>
        </div>
        <Input
          autoComplete="current-password"
          autoFocus
          onChange={(event) => onValueChange(event.target.value)}
          type="password"
          value={value}
        />
        {notice !== null && <p className="text-sm text-destructive" role="alert">{notice}</p>}
        <Button disabled={value.trim() === ''} type="submit">続行</Button>
      </form>
    </main>
  )
}

interface PasswordGateProps {
  children: ReactNode
}

// Renders its children normally until a `/api/` request answers 401. On that signal it
// takes the full screen with the password form; submitting a valid value stores it and
// remounts the children (bumped key) so their data fetch retries with the new header.
// Without a 401 this never activates, so local operation is unchanged.
export function PasswordGate({ children }: PasswordGateProps) {
  const [locked, setLocked] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [value, setValue] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(
    () =>
      subscribeAuthRequired((reason) => {
        setLocked(true)
        setValue('')
        setNotice(noticeForAuthRequired(reason))
      }),
    [],
  )

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const result = resolveViewPasswordSubmit(value)
    if (!result.ok) {
      setNotice(result.notice)
      return
    }
    setViewPassword(result.password)
    setValue('')
    setNotice(null)
    setLocked(false)
    setReloadKey((key) => key + 1)
  }

  if (locked) {
    return <PasswordForm notice={notice} onSubmit={handleSubmit} onValueChange={setValue} value={value} />
  }

  return <Fragment key={reloadKey}>{children}</Fragment>
}
