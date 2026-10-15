import { afterEach, expect, it, vi } from 'vitest'
import { fetchJson } from '../src/api/client'
import { fetchProposalHistory } from '../src/api/proposal-history'

vi.mock('../src/api/client', () => ({ fetchJson: vi.fn() }))
afterEach(() => vi.clearAllMocks())

it('提案履歴を最終ページまで読み、先頭100件で古い提案を切り捨てない', async () => {
  const first = Array.from({ length: 100 }, (_, index) => ({ proposal_id: `proposal-${index}` }))
  const last = [{ proposal_id: 'oldest-proposal' }]
  vi.mocked(fetchJson).mockResolvedValueOnce({ proposals: first, next_cursor: 'proposal-99' })
    .mockResolvedValueOnce({ proposals: last, next_cursor: null })
  const signal = new AbortController().signal
  expect(await fetchProposalHistory(signal)).toEqual({ proposals: [...first, ...last], next_cursor: null })
  expect(vi.mocked(fetchJson).mock.calls).toEqual([
    ['/api/investment-proposals', { signal }],
    ['/api/investment-proposals?cursor=proposal-99', { signal }],
  ])
})

it('後続ページの失敗を部分的な履歴で成功にしない', async () => {
  vi.mocked(fetchJson).mockResolvedValueOnce({ proposals: [], next_cursor: 'next' })
    .mockRejectedValueOnce(new Error('unavailable'))
  await expect(fetchProposalHistory(new AbortController().signal)).rejects.toThrow('unavailable')
})
