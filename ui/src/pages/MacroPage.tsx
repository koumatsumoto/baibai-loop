import { useEffect, useState } from 'react'
import { CircleAlert } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'

import { fetchJson } from '../api/client'
import type { MacroSeriesView, MacroView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'

function PageState({ message }: { message: string }) {
  return <><AppShell /><main className="grid min-h-[60vh] place-items-center px-6 text-center"><h1 className="text-xl font-semibold">{message}</h1></main></>
}

function SeriesChart({ series }: { series: MacroSeriesView }) {
  const config = { value: { label: series.label, color: 'var(--chart-1)' } } satisfies ChartConfig
  return (
    <Card className="gap-3 py-5 shadow-sm">
      <CardHeader className="px-5">
        <CardTitle className="text-base">{series.label}</CardTitle>
        <CardDescription>{series.series_id} · {series.unit}</CardDescription>
      </CardHeader>
      <CardContent className="px-3 sm:px-5">
        {series.points.length === 0 ? <p className="py-12 text-center text-sm text-muted-foreground">観測値なし</p> : (
          <ChartContainer className="h-52 w-full" config={config}>
            <LineChart data={series.points} margin={{ left: 4, right: 12 }}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="observed_at" minTickGap={28} tickLine={false} axisLine={false} />
              <YAxis domain={['auto', 'auto']} width={44} tickLine={false} axisLine={false} />
              <ChartTooltip content={<ChartTooltipContent />} />
              <Line dataKey="value" type="monotone" stroke="var(--color-value)" strokeWidth={2} dot={false} />
            </LineChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  )
}

export function MacroPage() {
  const [data, setData] = useState<MacroView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<MacroView>('/api/macro').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Macro を読み込めませんでした')
    })
  }, [])
  if (error) return <PageState message={error} />
  if (!data) return <PageState message="Macro を読み込んでいます…" />
  const context = data.context
  return (
    <><AppShell /><main className="mx-auto grid max-w-[1600px] gap-8 px-4 py-6 sm:px-6 lg:px-8">
      <section className="grid gap-4">
        <div><p className="text-sm font-medium text-muted-foreground">Judgment</p><h1 className="text-2xl font-semibold tracking-tight">Macro context</h1></div>
        {!context ? <Alert><CircleAlert /><AlertTitle>Published context なし</AlertTitle><AlertDescription>指標は fact として表示します。投資判断用 context は publish 後に現れます。</AlertDescription></Alert> : (
          <Card className="shadow-sm">
            <CardHeader className="border-b">
              <div className="flex flex-wrap items-center gap-2"><CardTitle>{context.summary}</CardTitle>{context.stale && <Badge variant="destructive">STALE</Badge>}</div>
              <CardDescription>{context.context_id} · as-of {context.as_of} · valid until {context.valid_until}</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-6 md:grid-cols-2">
              <div className="grid gap-3"><h2 className="font-semibold">Material delta</h2>{context.material_deltas.map((item, index) => <div className="rounded-lg border p-3" key={`${item.channel}-${index}`}><div className="mb-2 flex gap-2"><Badge variant="outline">{item.channel}</Badge><Badge variant="secondary">{item.direction} / {item.materiality}</Badge></div><p className="text-sm">{item.summary}</p><p className="mt-2 text-xs text-muted-foreground">{item.used_for}</p></div>)}</div>
              <div className="grid content-start gap-3"><h2 className="font-semibold">Sizing caution</h2>{context.sizing_cautions.length === 0 ? <p className="text-sm text-muted-foreground">なし</p> : context.sizing_cautions.map((item, index) => <Alert key={index}><CircleAlert /><AlertTitle>{item.severity}</AlertTitle><AlertDescription>{item.summary}</AlertDescription></Alert>)}</div>
            </CardContent>
          </Card>
        )}
        {data.context_history.length > 0 && <Card className="gap-3 py-5 shadow-sm"><CardHeader className="px-5"><CardTitle className="text-base">Published history</CardTitle><CardDescription>immutable revisions</CardDescription></CardHeader><CardContent className="grid gap-2 px-5">{data.context_history.map((revision) => <div className="flex flex-wrap items-baseline justify-between gap-2 border-b py-2 last:border-0" key={revision.context_id}><div><p className="text-sm font-medium">{revision.summary}</p><p className="font-mono text-xs text-muted-foreground">{revision.context_id}</p></div><time className="text-xs text-muted-foreground" dateTime={revision.as_of}>{revision.as_of}</time></div>)}</CardContent></Card>}
      </section>
      <section className="grid gap-5"><div><p className="text-sm font-medium text-muted-foreground">Fact</p><h2 className="text-2xl font-semibold tracking-tight">Macro indicators</h2></div>{data.groups.map((group) => <div className="grid gap-4" key={group.title}><h3 className="text-lg font-semibold">{group.title}</h3><div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">{group.series.map((series) => <SeriesChart key={series.series_id} series={series} />)}</div></div>)}</section>
    </main></>
  )
}
