// Pure policy and notices for the view-password gate, kept out of the component so the
// decisions are unit-testable.
//
// The fixed view password is attached as a fetch header value, which must be a Latin-1
// ByteString. Restricting input to printable ASCII guarantees the value can always be
// set on a request (no pre-flight TypeError) and keeps the 401 recovery path reachable.

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

// Trims, ignores an empty submit (no notice), and rejects non-ASCII with a notice. A
// valid value is returned for storage.
export function resolveViewPasswordSubmit(raw: string): ViewPasswordSubmit {
  const password = raw.trim()
  if (password === '') return { ok: false, notice: null }
  if (!isAllowedViewPassword(password)) return { ok: false, notice: PASSWORD_INVALID_NOTICE }
  return { ok: true, password }
}

// Message shown when the gate reappears: a rejected password explains itself; a first
// prompt shows none.
export function noticeForAuthRequired(reason: AuthRequiredReason): string | null {
  return reason === 'rejected' ? PASSWORD_REJECTED_NOTICE : null
}
