# Macro World Model Stage A baseline audit

価値tier: T2 — 統合済みの macro prose に残る時間・競合仮説・反証・学習の欠落を特定し、誤った世界認識を次の投資判断へ持ち込む前に止める。

## 対象と判定方法

`stores/application/baibai.sqlite` の `macro_context` table にある schema v4 全 4 revision の `payload` を対象にする。判定日は 2026-08-09。`実在` は failure が引用箇所に現れている、`不在` は引用箇所が failure を具体的に打ち消している、`部分的` は防御と欠落が同居する、を意味する。payload から観測できない author の内部注意配分は、本文に残った網羅転記・焦点化・構造だけで判定する。

| 略号 | context_id | as_of | articles |
| --- | --- | --- | ---: |
| R1 | `macro-context-2026-07-24-rates-high-cushion-thin` | 2026-07-24 | 22 |
| R2 | `macro-context-2026-07-27-capex-reckoning-event-week` | 2026-07-27 | 16 |
| R3 | `macro-context-2026-07-27-capex-reckoning-event-week-r2` | 2026-07-27 | 17 |
| R4 | `macro-context-2026-07-31-yen-policy-floor-capex-proof` | 2026-07-31 | 21 |

## 12 failure layers

### 1. Objective-function failure — completeness が integration の代理になる

- **R1: 部分的。** summary は「金融条件は緩和的なまま割引率だけが切り上がった局面」と圧縮できている。一方 `synthesis` は `null` で、`growth_demand` は「申請件数が 10 年分布の 0.2% 点」「コア資本財受注 z=+2.19」「機械受注 +30.3%」などを一つの judgment に併置する。統合は summary にあるが、重要度の階層は core 全体へ伝播していない。
- **R2: 不在。** 3 forces を「実質金利の再価格化」「円のアンカー変質」「AI capex の審判」に絞り、interaction は「実質金利 100% 点は AI capex の審判を増幅する」と依存関係を明示する。section completion より forces が読み順を支配している。
- **R3: 不在。** R2 の統合構造を保ちつつ、`inflation_costs` を「headline はエネルギー次第・コアは関税の残り玉で下がらない」の二層へ修正する。追加 source が単なる記事数増ではなく既存 force の機序を変えている。
- **R4: 不在。** summary は「円の政策フロア」「AI capex の実証選別」「割引率の天井」の 3 力に圧縮され、interaction も「円を守る行為が国内割引率の逆風を強める同一のトレードオフ」を示す。指定コメント §1 の「head の synthesis は浅くない」と一致する。

### 2. Representation failure — series から prose へ直接飛ぶ

- **R1: 実在。** `growth_demand` は「需要は『解雇のない減速』」と判断するが、level / momentum / breadth / horizon の構造化 state はなく、根拠と状態が一つの `judgment.summary` に閉じる。
- **R2: 実在。** 「需要経路の単一支配リスクが『AI 投資循環の持続性』に集中した」は state と mechanism を表すが、どちらも free text である。
- **R3: 実在。** 「米インフレは『headline はエネルギー次第・コアは関税の残り玉で下がらない』の二層」は有用な state 圧縮だが、state ID・horizon・evidence for/against へ分解されない。
- **R4: 実在。** 「日本は『内需底堅い × 中小信用悪化 × 交易条件は円と油の綱引き』の三すくみ」は統合判断だが typed state / graph ではなく prose のみである。

### 3. Attention failure — 全系列・全記事の同時提示で焦点が埋もれる

- **R1: 実在。** `inflation_costs` の一文に「米 CPI 3.5%」「コア 2.6%」「円 163 円」「輸入物価 100% 点」「+41.9pt/12m」「交易条件 19.3% 点」を並べる。焦点 fact と座標 annex の境界がない。
- **R2: 部分的。** forces は 3 件に絞る一方、`growth_demand` は「決算週はこの力の実証・反証イベント」と焦点化し、core fact は別に網羅される。executive は守られるが evidence pack の選択過程は payload から監査できない。
- **R3: 部分的。** R2 と同じ焦点構造を持つが、関税 source の追加が machine candidate / standing coverage / analyst addition のどれかは残らない。
- **R4: 部分的。** `regime_summary` は「三層構造」に圧縮し、座標は「TOPIX 4,003…全 122 系列の水準・方向は reading snapshot を参照」の 1 fact に隔離する。ただし excluded evidence と除外理由は payload にない。

### 4. Temporal failure — state / impulse / lag / horizon が混ざる

- **R1: 部分的。** fx は「換算益は一気に剥落」「margin 回復は在庫と価格改定のラグを伴って遅れて効く」と順序を持つ。一方 scenario は 0–3m / 3–12m / 12–24m に分かれない。
- **R2: 部分的。** 「7/29-30 の決算が実証・反証イベント」と直近 impulse を示すが、base path は horizon bucket を持たず、conditions と implications の一段で終わる。
- **R3: 部分的。** 「契約満了と段階的値上げで 1 年超持続」の evidence を追加するが、その lag は state / baseline path の型へ束縛されない。
- **R4: 部分的。** 円介入の即時効果と「実質賃金の天井が外れる経路」を区別する一方、bull の「秩序立って戻る」がどの horizon で進むかは明示しない。

### 5. Identification failure — level / momentum / acceleration / revision を混同する

- **R1: 部分的。** 「円は名目 163 円（100% 点）」「日米 10 年差は 12 か月で 0.90pt 縮小」と level と change を区別するが、release 間変化・same-period revision は持たない。
- **R2: 部分的。** `change_since_previous` は「市場水準はほぼ不変」「局面の変化は水準でなく材料」と切り分けるが、current release change と prior report diff が同じ prose に入る。
- **R3: 部分的。** 関税転嫁の「残り玉」を識別するが、revision / release news / external estimate の epistemic type は付かない。
- **R4: 部分的。** 「日経は…往復して水準はほぼ戻ったが、構成が変わった」と level と composition change を分離するが、acceleration・breadth・revision は typed dimension でない。

### 6. Policy endogeneity failure — policy を外生条件として扱う

- **R1: 部分的。** 「政策金利は下がっているのに長期実質金利が上がる」と policy rate と term premium を分けるが、scenario の日銀対応は単一 condition で reaction function ではない。
- **R2: 不在。** 「BOJ が円のために利上げを速めれば JGB と割引率へ跳ね、Fed が高止まりを続ければ円安が続く」と policy reaction と feedback を interaction の中心に置く。
- **R3: 不在。** R2 の reaction 構造を維持し、headline / core inflation の違いが Fed reaction を変える経路を追加する。
- **R4: 不在。** 「円フロアの信認は 10 月利上げの実行に依存し、その利上げは JGB の天井をさらに押し上げる」と policy trade-off を明示する。

### 7. Accounting failure — real / fiscal / external / financial の整合を閉じない

- **R1: 実在。** 「輸出企業の換算益」「輸入コスト企業の margin」を方向として述べるが、income / saving / external balance や nominal-real identity の検算はない。
- **R2: 実在。** 「追加国債・GPIF 需給対策」「借換コスト」「ERP 圧縮」を接続するが、fiscal impulse・debt service・funding の会計整合は検証されない。
- **R3: 実在。** tariff pass-through の持続を追加するが、wage / productivity / unit labor cost / margin の分解はない。
- **R4: 部分的。** 「META の FCF 急減も投資期の会計であり収益悪化ではない」と accounting misread を反証するが、world model 全体の cross-block consistency check はない。

### 8. Financial-cycle failure — rate / spread を並べ、balance-sheet channel を分けない

- **R1: 部分的。** 「HY / IG は…極端なタイトさ」「CCC だけが 93.1% 点」「RRP が枯渇」と裾と流動性を分けるが borrower / lender / collateral は分離しない。
- **R2: 部分的。** 「AI がソフトウェア・private credit の信用毀損側にも回る」と borrower 側へ踏み込むが、lender balance sheet と collateral channel はない。
- **R3: 部分的。** R2 と同じ。`liquidity_credit` は「平均は無事、最下層は選別が進む」に圧縮するが伝播の typed path はない。
- **R4: 部分的。** 「平均スプレッドのタイトさは波及時の再価格化余地」「レバレッジの高い候補の下値保護にはならない」と tail を識別するが liquidity / collateral / lender reaction は未分解である。

### 9. Global propagation failure — 国別 fact を cross-border path にしない

- **R1: 部分的。** FOMC → 実質金利、円 → 輸入物価 / margin、外需 → 日本を接続するが、trade / energy / capital flow / USD funding の代替経路を比較しない。
- **R2: 部分的。** 「Fed 高止まり → 円安」「AI capex → 日本の電機・機械・素材」を描く一方、cross-border propagation は force prose に埋まり、どの edge が baseline / contested か分からない。
- **R3: 部分的。** R2 に tariff pass-through を足すが、米国内転嫁から日本需要への link は matrix 上で比較されない。
- **R4: 部分的。** 円介入・油価・AI capex を日本へ伝える経路は具体的だが、capital flow / USD funding / leverage の competing channel は残らない。

### 10. Hypothesis failure — counter evidence が一文の儀式になる

- **R1: 実在。** `synthesis` がなく、base / bear / bull は別条件を持つものの、同じ evidence を競合仮説へ当てた matrix はない。
- **R2: 部分的。** 「AI 実需の継続」「MSFT・Meta が ROI を示せば減衰」など counter evidence は substantive で、指定コメント §1 と一致する。ただし各 force の alternative explanation / residual / relative plausibility は比較されない。
- **R3: 部分的。** R2 の反証に tariff pass-through を追加するが、共通 evidence matrix はない。
- **R4: 部分的。** 「全 5 社が 2 桁増収」「AI バブル崩壊の力ではない」と反対仮説を明示するが、2–3 hypothesis の同一 evidence 比較にはなっていない。

### 11. Validation failure — topology が semantic quality の代理になる

- **R1: 実在。** 「term premium（供給・財政・保有構成）と読む」は relation kind / claim strength / lag / falsifier を持たないまま valid payload である。
- **R2: 実在。** 「円安 → 輸入物価 +29.7% → 転嫁…→ 円安」という因果 loop は具体的だが、各 edge の sign・lag・current evidence ID・observable falsifier は独立検証できない。
- **R3: 実在。** 「関税の残り玉で下がらない」は一次 source を追加しても claim strength と falsifier が free text のままである。
- **R4: 実在。** 「輸入インフレ減衰 → 実質賃金の天井が外れる経路」は plausibly stated だが lag と反証 series/event が edge に束縛されない。

### 12. Learning failure — threshold 当否から mechanism error を学べない

- **R1: 実在。** `previous_scorecard_review` は「前回 scorecard は存在しない」。初回であるため mechanism learning の証拠がない。
- **R2: 部分的。** 「全件 pending」「発行から 1 営業日」と正直に記録するが、学習可能な結果はまだなく、error taxonomy もない。
- **R3: 部分的。** R2 と同じ scorecard 状態。r2 の修正理由は payload diff から分かるが、data revision / method / state reinterpretation の帰属 field はない。
- **R4: 部分的。** 「met 2・pending 9」「base（p=0.55）の金利持続条件が先に成立」と度合いを記録し、「監視機械条件は日次系列に統一する」という運用学習もある。一方 state / sign / lag / strength / policy reaction のどこが正しかったかは帰属できない。

## 較正結果

1. 現行 v4 の最大の不足を「指標列挙から統合できないこと」とは置かない。R2–R4 は少数 forces、実在する counter evidence、非対称 scenario、interaction を持つ。
2. Stage A が反証すべき marginal T2 は、(a) horizon 混同、(b) 単一 narrative への早期収束、(c) free-text mechanism の過剰因果、(d) threshold 当否から機構誤差へ進めないこと、の 4 点である。
3. `synthesis: null` の R1 と、3 forces を持つ R2–R4 を同じ baseline として平均しない。R1 は v4 初回境界、R2–R4 が統合済み比較対象である。
4. 指定コメント §1 との相違はない。追加監査で確認できた差は、R1 だけは objective / hypothesis failure が実在し、R4 の accounting failure は META FCF の反証によって部分的に抑えられている点である。
