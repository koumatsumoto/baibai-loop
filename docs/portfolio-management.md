---
title: "Portfolio management"
summary: "企業別の機会評価を資本判断へ接続し、資金目安、human-confirmed ledger、保有規律、年次評価を運用する方針。"
doc_type: governance
status: active
related_docs:
  - "./doctrine.md"
  - "../AGENTS.md"
  - "./reference/portfolio-ledger.md"
  - "./reference/investment-proposal.md#holding"
---

# Portfolio management

[投資目的とリスク許容](./doctrine.md#investment-strategy)に従い、資本判断はresearch、確認済みcash・数量・注文・約定はpositionが所有する。企業評価は[Reviewed Assessment](./reference/stock-assessment.md)、行動と具体条件は[Investment Proposal](./reference/investment-proposal.md)、保有継続・回収も[同じ提案契約](./reference/investment-proposal.md#holding)を参照する。

<a id="required-return-policy"></a>
## 要求年率

要求年率は案件ごとに作者が根拠を説明して定める正の有限値であり、Stock Assessmentに記録する。6%は比較の目安で、一律下限や自動入力値ではない。6%未満も選べるが、3%等の数値だけで購入を決めず、現在価格を通すために要求率を逆算・引き下げない。最適値、株主資本コスト、確率加重期待収益の推定とは区別する。採用判断と検証の正本は[#1548](https://github.com/koumatsumoto/baibai-loop/issues/1548)である。

要求率の経済的理由は既存のBase/Downsideの`calculation`へ記し、下振れ・永久損失、実現期間と遅延、予測の不確実性を既存の仮説・反証・riskへ接続する。同じリスクを利益・倍率・要求率へ説明なく重複計上しない。Proposalの`comparison / forgone`では同じ期間・通貨・税費用の前提で現金維持や他の完成案と比べ、現金の流動性・待機価値と機会損失も説明する。現金利率、ユーザーの損失許容度、シナリオ確率を捏造せず、未確認ならその範囲を明示する。重要な不足は保留理由にする。低い要求率や財務の健全さを、現金より安全という意味へ読み替えない。

独立Reviewは要求率を支える根拠と結論が反転する条件も確認する。buy提案は記録済み要求率によるPmaxと他の購入条件を確認し、数値条件だけで重大な事実不足や企業価値毀損を免除しない。policy変更だけで既存Stock Assessment・Review・Pmax・Proposal・ledgerを書き換えない。要求率を再評価する場合は新しい評価と独立Reviewにする。機械E[r]の固定較正帯と保有sell判断は、この新規購入判断から分離する。

<a id="capital-guidance"></a>
## 資金目安

約500万円は運用イメージであり、予定の月40万円をcashへ加えない。人間が確認した入金だけをledgerへ反映する。銘柄数10〜20、投資率、予算消化はノルマではない。

通常5〜8%程度は目安で、NAVによる自動sizingはしない。現在の1回20〜30万円guideは500万円の4〜6%に相当する。数量はguide上限と確認済みavailable cashの両方から100株単位で算出する。1単元がguideを超えてもcash内ならwarning付き候補となる。cashで1単元を買えなければ0/defer。下限を満たすためだけに増やさない。

通常経路で既保有tickerやactive買い予約のあるtickerへの追加購入を提案しない。同Proposalの約定後は全売却済みでも再利用せず、新しいResearch/Proposalで判断する。企業評価を別IDにすることでこの条件を迂回しない。既保有への追加禁止は[#1242](https://github.com/koumatsumoto/baibai-loop/issues/1242)の現行方針として維持する。新規候補と同じ限界資本で比較する合成例には見送り得る追加機会があるが、実データでの経済効果と禁止の最適性は未実証であり、追加を許すResearch/Proposal発行経路も採用していない。

<a id="reservation-and-warnings"></a>
## 予約とwarning

buy reservationは数量×price guardをcashから拘束し、sell reservationは未約定数量を保有株数の範囲で拘束する。sellの見込代金をcashへ加えず、約定までは保有lotを減らさない。partial fill後は残数量だけを予約に残し、cancel/expireは人間の報告によるreleaseで示す。未報告の注文状態は推定しない。

ticker10%、sector40%、common-factor35%、ADV5%などの集中・執行warningは人間へ示す。数量・資金はevent replay、時価はmarket quoteと現在保有episodeの権利basisから読む。台帳への価格転記は不要である。全保有のquote・権利basisを確認できなければNAVと比率は未評価とし、架空の分母を作らない。cash20%はwarningであり投資率ノルマや自動数量縮小の理由ではない。warning受容にはledgerの既存人間overrideを使い、企業根拠の不足を小口購入のoverrideで通さない。

完了した新規対象をProposalで比較し、一度に一銘柄を提案する。L3完了後の採否・注文は外部アクターの工程である。後続配分では新しい明示開始と最新cash・予約で再判断する。比較対象が変わるCandidate Discovery method改訂後は、新しい候補集合でProposalを比較し直し、旧Proposalを新しい集合の判断として使わない。企業固有の事実・仮定がなお有効なReviewed Assessmentは引き継ぎ、全面再調査を前提にしない。過去のProposal・約定記録は変更しない。一括予約や予定入金を仮定しない。

<a id="holding-discipline"></a>
## 保有規律

保有提案は`hold / sell / defer`。重大な投資理由の不成立なら価格やvaluationが不明でもsell判断を残す。仮説維持を今回確認したholdと、同日起点の残存見返りが十分であるholdを分ける。旧valuation・quote不足だけでは仮説維持のholdを拒否しないが、十分な当日収益を主張しない。確認済みの残存見返りが不十分ならsell、仮説や必要判断が不確実ならdeferで確認事項を示す。

残存見返りにはこれから持ち続けることで得る分配だけを含める。受取済み配当と、権利確定済みで今売っても受け取れる未入金配当を再算入しない。旧horizonを残日数で割り直して年率を水増ししない。売却税・費用・遅延・下振れで結論が変わり得るならuncertainとする。

Target到達・価格下落・経過期間・新規買いfloor未達・集中warningだけでは売らない。現金回収に次の候補は不要。数量basis不明なら数量付き売却案は作らず、人間にbroker実状態の確認を求める。

## 人間が確認した取引事実

brokerの事実を現在の投資条件で再審査しない。旧注文のpartial/late/release、未記録予約の遅延報告、部分売却・方針外追加購入も、既存identity、人間確認、cash/数量の整合確認により記録する。他tickerのquote欠損を事実記録の停止理由にしない。proposalのsellを実際の全売却と推定しない。

記録手順は[Ledger Record](../.agents/skills/ledger-record/SKILL.md)、event・replay・未評価の意味は[Portfolio Ledger](./reference/portfolio-ledger.md)が所有する。

## 成果と学習

既存Portfolio Outcome/TWR/TOPIX比較を使い、cash、未売却損益、配当、実費、確認税を含むportfolio全体で測る。売却済み銘柄や勝率だけで評価せず、未解決データを母数から消さない。指数とportfolioの税・費用basis差を示す。entry予測と後続評価は既存reports/studiesで照合し、新戦略の収益優位は別に検証する。

成果計算の営業日と保有銘柄の価格は、同じSQLite読み取りsnapshotから取得する。新規Outcome payloadはschema 2とし、現在の計算・利用に寄与しないDB全体hashと価格fingerprintを持たない。保存済みの過去payloadは読み取り時に書き換えず、同一outcome IDの結果を新形式で上書きしない。
