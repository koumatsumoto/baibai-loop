---
title: "Earnings Power v1 A′ outcome-free preregistration"
date: 2026-08-24
status: frozen_before_forward_replay
issues: [1041, 1064]
---

価値tier: T1 — Value / Carry上位とは異なる割安候補をResearch Gateへ追加できるかを、production authority付与前に反証する。

# 目的と封印

`normalized_per_3fy`を正常利益やFVと呼ばず、複数FY利益に対する現在価格の倍率というDerived Metricのまま、独立したEarnings Power Opportunity Laneの候補生成に使えるかを検証する。本書のcommitがfreeze pointであり、この時点ではcurrent calibration bundleの`calibration.forward`を読んでいない。既知peekとして、2026-08-02 raw-axis studyが低い`normalized_per_3fy`の長期方向を支持し、2026-08-08 exploration band studyが別policyでnegativeだったことは既知である。したがって今回をblind proofとは呼ばない。

# 固定Selection Policy

正本は`method/screening/selection-policies/earnings-power-v1.yaml`とする。

- Lane ID: `earnings-power`
- Selection Policy ID: `earnings-power-v1`
- input: common investable gate通過行
- observed: `normalized_per_3fy`が欠損でなく有限かつ正
- minimum opportunity condition: `normalized_per_3fy <= 12.0`
- depth: 20
- order: `normalized_per_3fy`昇順、finite `er_annual`降順（null-last）、ticker昇順
- E[r] floorなし、durability gateなし、missing補完なし
- K=5は探索せず、Attention Policyの固定pilot parameterとする

12倍は「利益倍率として割安」というminimum conditionを先に固定する絶対水準であり、historical returnから選ばない。12倍外を供給不足時に補充しない。

# Policy Diagnostic / metric integrity

- `earnings-power-leverage-risk-v1`: candidateにraw `debt`と`cash`がともに観測され、`debt > cash`なら付与する。financial riskのannotationであり自動blockしない。
- `earnings-power-historical-special-gain-risk-v1`: current candidate / existing panelは3FY各年の特別損益compositionを持たず、exact判定不能。推定tagを作らず付与しない。
- `metric_integrity.attention_block_condition`: `null`。観測不能なhistorical special gainを理由にblockを捏造しない。
- `forecast_special_gain_flag`はforward会社予想の別factでありhistorical 3FY compositionの代用にしない。

# Outcome-free geometry

forward replay前にcurrent fixed bundleのpanelだけを用いて、各as-ofのeligible件数、top20、Value / Carry top20 overlap、alt-only供給、sector concentrationを記録する。fresh supplyはhistorical canonical Shortlistが同じ過去as-ofを覆わないためhistorical replayでは算定せず、live canonical historyだけをproduction Attentionが判定する。

# Historical replay contract（exactly once）

固定bundle:

- bundle ID: `20260820T073844Z-02061626ff91452cb645ef3c0ffc54a1`
- bundle manifest SHA-256: `e189c36dc0f098747be30569d8f934612b45a51bddf71ed47964758e47042cb4`
- source field aliases: pre-rename `selection_rank`はValue / Carry comparator rank、`evidence_playbooks`はEvidence Pattern historyとして読む。policy metricは`normalized_per_3fy`。
- source rules identityはcohort diagnosticsの`rules_hash`を列挙する。#1064 rename後のnew rules hashへはsemantic-equivalence reportだけでbridgeし、forward replayを再実行しない。

対象はbundle内でproduction authorityとcanonical cohort integrityを満たし、forward statusがcompleteなcohort。3y / 5y、`price_return` / `total_return`を別々に出す。各cohortでEarnings top20、Value / Carry `selection_rank <= 20`、両者のalt-onlyを固定し、ticker-equalとcohort-equalを併記する。未解決行は除外したas-reportedに加え、neutral=0とfailure=-100%の両感応度を出す。trapはreturn <= -20%、sector concentrationはtop20最大sector shareとする。

固定verdict:

1. canonical eligible cohortが各horizon 12未満なら`insufficient`。
2. いずれかのhorizon・basisでEarnings top20のcohort-equal median returnがValue / Carry top20を下回り、neutral / failureの双方でも救済されなければ`negative`。
3. unresolved sensitivityで差の符号が割れる、またはtotal-return coverageが75%未満なら`inconclusive`。
4. 上記に該当せず、両horizon・両basisで差が非負、alt-only median件数が5以上、top20最大sector shareのcohort medianが50%以下なら`eligible_for_shadow`。
5. verdict後にthreshold、minimum condition、ordering、diagnostic、block、K、windowを変更して救済しない。

Shadowは`eligible_for_shadow`のときだけ開始する。結果が`negative` / `inconclusive`ならEarnings production pathを削除し、raw annotationだけを残す。
