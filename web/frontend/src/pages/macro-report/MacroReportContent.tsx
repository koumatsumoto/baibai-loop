import type { MacroContextView } from '../../api/types'
import { AssessmentContent } from '../../components/market-context/AssessmentContent'
import { partitionMacroCore } from '../../lib/macro-report'
import { MacroDominantForces } from './MacroDominantForces'
import { MacroEvidenceDetails } from './MacroEvidenceDetails'
import { MacroReportOverview } from './MacroReportOverview'
import { MacroResearchImplications } from './MacroResearchImplications'
import { MacroScenariosAndMonitoring } from './MacroScenariosAndMonitoring'

export function MacroReportContent({ data }: { data: MacroContextView }) {
  if (data.assessment) return <><p className="mb-6 whitespace-pre-wrap">{data.summary}</p><AssessmentContent data={data.assessment} /></>
  const partition = partitionMacroCore(data.core)
  const hasCore = (data.core?.length ?? 0) > 0
  return (
    <>
      <MacroReportOverview data={data} regime={partition.regime} risk={partition.risk} />
      {!hasCore && <p className="whitespace-pre-wrap">{data.summary}</p>}
      {data.synthesis && <MacroDominantForces synthesis={data.synthesis} />}
      {data.connection && <MacroResearchImplications connection={data.connection} />}
      <MacroScenariosAndMonitoring monitoring={partition.monitoring} risk={partition.risk} />
      {hasCore && <MacroEvidenceDetails partition={partition} />}
      {data.historical_payload && <details className="mt-6 rounded-lg border p-4"><summary className="cursor-pointer font-medium">保存当時の本文・入力・引用</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(data.historical_payload, null, 2)}</pre></details>}
    </>
  )
}
