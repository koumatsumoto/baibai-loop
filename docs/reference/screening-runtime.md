# Screening

保存済みの解析モデルから調査候補を抽出する。モデル生成は候補選定より先に完了し、候補外の企業もInvestment Judgmentから読める。候補政策は企業の経済的reject・価値評価・購入判断を代行しない。

<a id="rule-purpose"></a>
## 候補抽出の意味

通常政策は、収益力・事業拡大・営業回復・資本活用の経済経路ごとに、価格と事業を組み合わせる。[順位付け](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_paths.py)が計算・容量・同順位、[入力投影](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_path_inputs.py)が座標・比較期間・参照群の正本である。

順位は有限な注意配分のheuristicであり、期待収益・確率・健全性・買付順位ではない。相対上位は市場全体が割高でも生じる。業種内の異質性、少数参照群の百分位上限、有効経路数の機会差、同じ利益変化の共有や一過性利益による順序の偏りは残る。二座標や複数モデルを独立した証拠票とは呼ばない。

政策比較の有限断面と採否は[#1680](https://github.com/koumatsumoto/baibai-loop/issues/1680)に残す。候補の入替え自体を品質・将来収益の改善とせず、一過性利益の検証は調査者が行う。

入力不足はその材料・経路の不足として示し、候補数を満たすために0や別期間の好値で埋めない。成長・回復は現在の利益やCFの黒字を要求しない。売上や営業改善を株式時価総額で割る座標は、負債込み事業価値の割安性を保証せず、追加投資・借換・希薄化はL3で評価する。

完全FYの利用はTTMの対応期間未観測を補うもので、既知の悪化・欠測・会計basisの不一致を消す手段ではない。収益力は成長の主張でもなく、横ばい・小幅減益だけで除外しない。選択FYと後続の直近報告を区別し、最新終端のTTMと読み替えない。

資本経路の正の報告利益はこの経路の条件であり、赤字企業全体の除外条件ではない。利益/簿価は、保存PBRが含意する簿価に対する報告利益proxyである。簿価の個別source・会計scope・測定日は現モデルに未保存で、検証済み普通株ROE・平均自己資本利益率・資産解放額とは呼ばない。未実現資産の機会を全て拾う政策でもない。

<a id="candidate-discovery"></a>
## 調査への引渡し

カードは原数値・符号・対象期・反対材料を保つ。Investment Judgmentは複数モデル・一次資料・必要な少数の候補外対照を比較し、価値獲得経路と反証を自ら評価する。外部評価・テクニカル・共有環境は比較材料であり、保存市場判断やportfolioで機械順位を変更しない。旧成果は[保存reader](estimate-calibration.md)に従う。

同じ比較の詳細を `selection-context --compare-ticker` で再読する時は、返されたSelection ID・`research_as_of`・`input_known_at`を保ち、`--screening-selection-id`・`--research-asof`・timezone付きの`--known-at`へ渡す。Selection IDはモデル世代を固定し、Research・市場判断は各保存/公開時刻をcutoffと照合して読む。`--known-at`省略時は実行時刻を使う。

cutoffだけでcontext全体は固定されない。保有は現在の台帳を発生時刻でreplayするため、過去の注文/約定の後追記で同じcutoffでも保有と依頼対象が変わり得る。contextの再読を保存Operationの固定入力からの再開と区別する。古いcutoffの指定は、後で取得した原本を当時既知だった証拠に変えない。

## 入力不足と品質

[時点・欠測](model-observations.md#source-owners)の共通条件に従う。取得coverageは成功した要求範囲で、全銘柄の入力充足ではない。入力不足で候補が少ないことを、投資機会が少ないという結論へ置き換えない。価格/株数basisと期間比較は[開示と会計比較](model-observations.md#financial-periods)に従う。

## 保存と読取

ModelGenerationとScreeningSelectionは別identityであり、同じモデルからの政策変更をモデル再生成と混同しない。候補0件も正常な公開結果で、過去の非空候補へ戻さない。保存・時点選択の詳細は[selection store](../../engine/src/baibai_engine/machine_analysis/screening/selection_store.py)と[selection query](../../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)が所有する。

L3再開と生成cacheの寿命は[保存責務](../architecture.md#store-authority)、取得・retry・発行と整理の順序は[日次運用](../../jobs/OPERATIONS.md#日次実行と結果確認)に従う。
