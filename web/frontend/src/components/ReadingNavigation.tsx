import { Link, useLocation } from 'react-router'
import { assessmentReturnHref } from '../lib/reading-navigation'

export function AssessmentReturnLink({ ticker }: { ticker?: string }) {
  const { search } = useLocation()
  const to = assessmentReturnHref(search, ticker)
  return <Link className="inline-flex min-h-11 w-fit items-center rounded px-2 text-sm underline underline-offset-4" to={to}>
    {to === '/stocks' ? '提案一覧に戻る' : ticker ? '元の提案・この銘柄の判断に戻る' : '元の提案に戻る'}
  </Link>
}
