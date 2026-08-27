import type { MacroContextView } from '../../api/types'
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert'
import { partitionMacroCore } from '../../lib/macro-report'
import { MacroDominantForces } from './MacroDominantForces'
import { MacroEvidenceDetails } from './MacroEvidenceDetails'
import { MacroReportOverview } from './MacroReportOverview'
import { MacroResearchImplications } from './MacroResearchImplications'
import { MacroScenariosAndMonitoring } from './MacroScenariosAndMonitoring'

export function MacroReportContent({ data }: { data: MacroContextView }) {
  const partition = partitionMacroCore(data.core)
  const hasCore = (data.core?.length ?? 0) > 0
  return (
    <>
      <MacroReportOverview data={data} regime={partition.regime} risk={partition.risk} />
      {!hasCore && <Alert><AlertTitle>セクションを表示できません</AlertTitle><AlertDescription>この revision の core セクションが served view に含まれていません。view の再生成待ちの可能性があります。</AlertDescription></Alert>}
      {data.synthesis && <MacroDominantForces synthesis={data.synthesis} />}
      {data.connection && <MacroResearchImplications connection={data.connection} />}
      <MacroScenariosAndMonitoring monitoring={partition.monitoring} risk={partition.risk} />
      {hasCore && <MacroEvidenceDetails partition={partition} />}
    </>
  )
}
