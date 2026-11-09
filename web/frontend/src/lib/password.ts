// Printable ASCII can always be used in an Authorization header.

import type { AuthRequiredReason } from '../api/auth'

const VIEW_PASSWORD_PATTERN = /^[\x21-\x7e]+$/

export const PASSWORD_INVALID_NOTICE = '半角英数字と記号のみ使用できます。'
export const PASSWORD_REJECTED_NOTICE = 'パスワードが拒否されました。もう一度入力してください。'

export function isAllowedViewPassword(value: string): boolean {
  return VIEW_PASSWORD_PATTERN.test(value)
}

export type ViewPasswordSubmit =
  | { readonly ok: true; readonly password: string }
  | { readonly ok: false; readonly notice: string | null }

export function resolveViewPasswordSubmit(raw: string): ViewPasswordSubmit {
  const password = raw.trim()
  if (password === '') return { ok: false, notice: null }
  if (!isAllowedViewPassword(password)) return { ok: false, notice: PASSWORD_INVALID_NOTICE }
  return { ok: true, password }
}

export function noticeForAuthRequired(reason: AuthRequiredReason): string | null {
  return reason === 'rejected' ? PASSWORD_REJECTED_NOTICE : null
}
