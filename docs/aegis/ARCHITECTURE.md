# AEGIS INVEST AI — ARCHITECTURE

## Purpose

Aegis Invest AI is a research-first investment intelligence system.

Target active-scanner flow:

SCAN MANY
-> FILTER
-> RANK
-> SELECT FEW
-> RISK CHECK
-> SIMULATE
-> MANAGE POSITIONS
-> CONTINUE SCANNING

Aegis is not required to trade every day.

NO TRADE is a valid decision.

## Existing lifecycle

Historical lifecycle already includes:

observation
-> intelligence
-> decision
-> TradeProposal
-> RiskManager
-> simulated OPEN / INCREASE
-> position management
-> REDUCE / CLOSE
-> realized/unrealized P/L
-> audit trail

## Entry path

market observation
-> opportunity ranking
-> agent decision
-> TradeProposal
-> RiskManager
-> simulated OPEN / INCREASE

## Position-management path

open position
-> next eligible causal market bar
-> current position state
-> agent/context
-> ExitPolicy
-> HOLD / REDUCE / CLOSE
-> simulated execution

## Critical separation

Entry ranking and position management are independent.

An open position must remain manageable even if:

- the symbol is no longer highly ranked
- the symbol is excluded from new-entry comparison
- another asset has a better opportunity score

## Active scanner principles

The future scanner must support:

- broad market observation
- cross-asset comparison
- deterministic ranking
- explicit exclusions and reason codes
- limited simulated capital
- repeated intraday observation
- independent management of existing positions
- complete audit trail
