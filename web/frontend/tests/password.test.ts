import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { PasswordForm } from '../src/components/PasswordGate'
import {
  PASSWORD_INVALID_NOTICE,
  PASSWORD_REJECTED_NOTICE,
  noticeForAuthRequired,
  resolveViewPasswordSubmit,
} from '../src/lib/password'

function renderForm(notice: string | null): string {
  return renderToStaticMarkup(
    createElement(PasswordForm, {
      value: '',
      notice,
      onValueChange: () => undefined,
      onSubmit: () => undefined,
    }),
  )
}

describe('resolveViewPasswordSubmit', () => {
  it('rejects a non-ASCII password with the invalid notice and yields no password', () => {
    const result = resolveViewPasswordSubmit('ひみつ123')
    expect(result.ok).toBe(false)
    expect(result).toEqual({ ok: false, notice: PASSWORD_INVALID_NOTICE })
  })

  it('accepts a printable-ASCII password and returns it trimmed', () => {
    const result = resolveViewPasswordSubmit('  Str0ng-secret!  ')
    expect(result).toEqual({ ok: true, password: 'Str0ng-secret!' })
  })

  it('treats an empty submit as a no-op with no notice', () => {
    expect(resolveViewPasswordSubmit('   ')).toEqual({ ok: false, notice: null })
  })
})

describe('noticeForAuthRequired', () => {
  it('explains a rejected password on the repeat prompt', () => {
    expect(noticeForAuthRequired('rejected')).toBe(PASSWORD_REJECTED_NOTICE)
  })

  it('shows no notice on a first prompt', () => {
    expect(noticeForAuthRequired('required')).toBeNull()
  })
})

describe('PasswordForm rendering', () => {
  it('shows the invalid-input notice when validation fails', () => {
    const markup = renderForm(PASSWORD_INVALID_NOTICE)
    expect(markup).toContain(PASSWORD_INVALID_NOTICE)
    expect(markup).toContain('role="alert"')
  })

  it('shows the rejected notice when the gate reappears after a 401', () => {
    expect(renderForm(PASSWORD_REJECTED_NOTICE)).toContain(PASSWORD_REJECTED_NOTICE)
  })

  it('renders no alert when there is no notice', () => {
    expect(renderForm(null)).not.toContain('role="alert"')
  })
})
