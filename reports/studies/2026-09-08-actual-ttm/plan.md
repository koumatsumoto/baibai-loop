# 非実績開示と実績TTMの選択点検

価値tier: T2 — 利用可能な収益事実を保持し、既存Approachの判断入力を訂正する。

対象は#1265。既知の4812再現を契機とするcorrectness修正で、将来収益の優劣で採否を選ばない。
実績行の定義・TTM式は既存ownerを使い、実績期間を選んだ後に必要fieldの欠損を判定する。
配当/予想のみの行を除外しても、最新予想のnullや配当修正を古い値へ戻さない。
新score、選定閾値、Approach、売買条件を変更しない。

9月7日断面の比較条件は再計算前にprivate計画へ固定した。全3,705社、同一market store、
同一as-of、同一rules、productionとcalibrationの共通ownerで修正前後を計算する。
保存済みcanonical runとも照合し、不一致があれば元runの効果と同入力比較を分ける。
旧run・正式判断・取引事実は上書きしない。原provider responseは保存されておらず、
公表日制約は後日のbackfill不存在の証明ではない。

以下は較正contextの再計算前に固定する条件。事実選択の変更でmethod identityが変わるため、
旧methodの較正値へ新hashを付け替えない。ローカルの全market入力から、通常のproduction
panelを2019年11月〜2026年8月の全cohortで再構築し、同じrulesで3y/5yの既存E[r] contextを
再生成する。月末grid、成熟・coverage・不明退出の扱いは既存ownerに従う。
これを修正の収益効果や新手法採用の実証とは呼ばず、将来成績の成熟は待たない。

完了条件は実績TTMの不変性、真正欠損/部分訂正の保留、予想・配当契約の維持、
Security Analysisへの伝達とnative order/union影響の説明、full local gates、独立反証Review。
根拠と残る限界、次の1作業は同じPRへまとめる。

較正の実行scopeは全cohortのdiagnosticで既存ownerが示すintegrity・必須metricのeligible条件だけから機械的に決める。
required as-ofは3y/5yの両方がeligibleとなる全月の共通集合とし、収益率・順位・符号で月を選ばない。
context本体のhorizon別利用可能月は既存context ownerに従う。共通集合が空なら生成不能を報告し、
要件を緩めたり旧値のhashを差し替えたりしない。
