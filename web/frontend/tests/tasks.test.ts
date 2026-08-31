import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { EventsCard } from '../src/pages/TasksPage'

describe('Tasks partial failure', () => {
  it('does not describe unavailable upcoming events as zero events', () => {
    const markup = renderToStaticMarkup(createElement(EventsCard, {
      events: [],
      ledgerError: 'invalid ledger',
    }))

    expect(markup).toContain('イベント情報が不完全です')
    expect(markup).toContain('不完全')
    expect(markup).not.toContain('0 件')
    expect(markup).not.toContain('イベントはありません')
  })
})
