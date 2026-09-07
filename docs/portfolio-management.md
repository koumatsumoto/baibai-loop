---
title: "Portfolio management"
summary: "投資価値rankingを先に行い、資金目安、human-confirmed ledger、保有規律、年次評価を運用する方針。"
doc_type: governance
status: active
related_docs:
  - "./doctrine.md"
  - "../AGENTS.md"
  - "./reference/portfolio-ledger.md"
  - "./reference/position-review.md"
---

# Portfolio management

資本判断はresearch、確認済みcash・数量・注文・約定はpositionが所有する。企業評価は[Reviewed Thesis](./reference/thesis.md)、候補比較は[CAA](./reference/capital-allocation-assessment.md)、保有継続・回収は[Position Review](./reference/position-review.md)を参照する。

<a id="capital-guidance"></a>
## 資金目安

約500万円は運用イメージであり、予定の月40万円をcashへ加えない。人間が確認した入金だけをledgerへ反映する。銘柄数10〜20、投資率、予算消化はノルマではない。

通常5〜8%程度は目安で、NAVによる自動sizingはしない。現在の1回20〜30万円guideは500万円の4〜6%に相当する。数量はguide上限と確認済みavailable cashの両方から100株単位で算出する。1単元がguideを超えてもcash内ならwarning付き候補となる。cashで1単元を買えなければ0/defer。下限を満たすためだけに増やさない。

通常経路で既保有tickerやactive買い予約のあるtickerへの追加購入を提案しない。同CAAの約定後は全売却済みでも再利用せず、新しいResearch/CAAで判断する。企業評価を別IDにすることでこの条件を迂回しない。

<a id="reservation-and-warnings"></a>
## 予約とwarning

reservationは数量×price guardをcashから拘束し、partial fill後は残数量だけを残す。cancel/expireは人間の報告によるreleaseで示す。未報告の注文状態は推定しない。

ticker10%、sector40%、common-factor35%、ADV5%などの集中・執行warningは人間へ示す。数量・資金はevent replay、時価はmarket quoteと現在保有episodeの権利basisから読む。台帳への価格転記は不要である。全保有のquote・権利basisを確認できなければNAVと比率は未評価とし、架空の分母を作らない。cash20%はwarningであり投資率ノルマや自動数量縮小の理由ではない。warning受容にはledgerの既存人間overrideを使い、企業根拠の不足を小口購入のoverrideで通さない。

同じResearch SetからCAAを一件ずつ公開できる。先行注文の人間報告をledgerへ反映してから、後続候補を最新cash・予約で再計算する。一括予約や予定入金を仮定しない。

<a id="holding-discipline"></a>
## 保有規律

保有提案は`hold / exit`。重大な経済的投資理由の不成立なら価格やvaluationが不明でもexit候補となる。成立していて残存見返りが十分ならhold、不十分ならexit、不確実ならnullで必要な確認を示す。

残存見返りにはこれから持ち続けることで得る分配だけを含める。受取済み配当と、権利確定済みで今売っても受け取れる未入金配当を再算入しない。旧horizonを残日数で割り直して年率を水増ししない。売却税・費用・遅延・下振れで結論が変わり得るならuncertainとする。

Target到達・価格下落・経過期間・新規買いfloor未達・集中warningだけでは売らない。現金回収に次の候補は不要。数量basis不明なら数量付き売却案は作らず、人間にbroker実状態の確認を求める。

## 人間が確認した取引事実

brokerの事実を現在の投資条件で再審査しない。旧注文のpartial/late/release、未記録予約の遅延報告、部分売却・方針外追加購入も、既存identity、人間確認、cash/数量の整合確認により記録する。他tickerのquote欠損を事実記録の停止理由にしない。proposalのexitを実際の全売却と推定しない。

## 成果と学習

既存Portfolio Outcome/TWR/TOPIX比較を使い、cash、未売却損益、配当、実費、確認税を含むportfolio全体で測る。売却済み銘柄や勝率だけで評価せず、未解決データを母数から消さない。指数とportfolioの税・費用basis差を示す。entry予測と後続評価は既存reports/studiesで照合し、新戦略の収益優位は別に検証する。

確認済み入出金・配当・費用・税・売買のdraft/applyは価格不要のreplayで検証する。`position ledger`とWebは時価未評価でもcash・予約・数量・原価を表示し、未評価を0円や空保有へ変換しない。
