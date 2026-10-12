import type { AssessmentPublicationView } from '../../api/types'
import { partitionHistoricalCore } from '../../lib/assessment-history'
import { HistoricalDominantForces } from './HistoricalDominantForces'
import { HistoricalEvidenceDetails } from './HistoricalEvidenceDetails'
import { HistoricalReportOverview } from './HistoricalReportOverview'
import { HistoricalResearchImplications } from './HistoricalResearchImplications'
import { HistoricalScenariosAndMonitoring } from './HistoricalScenariosAndMonitoring'

export function HistoricalReportContent({ data }: { data: AssessmentPublicationView }) {
  const partition = partitionHistoricalCore(data.core)
  const hasCore = (data.core?.length ?? 0) > 0
  return (
    <>
      <HistoricalReportOverview data={data} regime={partition.regime} risk={partition.risk} />
      {!hasCore && <p className="whitespace-pre-wrap">{data.summary}</p>}
      {data.synthesis && <HistoricalDominantForces synthesis={data.synthesis} />}
      {data.connection && <HistoricalResearchImplications connection={data.connection} />}
      <HistoricalScenariosAndMonitoring monitoring={partition.monitoring} risk={partition.risk} />
      {hasCore && <HistoricalEvidenceDetails partition={partition} />}
      {data.historical_payload && <details className="mt-6 rounded-lg border p-4"><summary className="cursor-pointer font-medium">保存当時の本文・入力・引用</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(data.historical_payload, null, 2)}</pre></details>}
    </>
  )
}
