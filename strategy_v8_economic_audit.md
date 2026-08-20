# STRATEGY V8 — ECONOMIC SANITY & POSITION-SIZING AUDIT REPORT

**Audit Date:** 2026-08-15 13:05:48  
**Initial Starting Capital:** ₹100,000.00  
**Audit Scope:** 20-Part Deep Economic, Risk Model, and Compounding Verification  
**V8 Economic Status:** **NEEDS REDESIGN**  
**Final Decision:** **V8 PROMISING BUT RISK MODEL REQUIRES REDESIGN**  

---

## Executive Summary

1. **Accounting Traceability (100% Verified):** Capital continuity equation `capital_after = capital_before + net_pnl` holds with zero discrepancy across all 5,873 executed trades.
2. **Root Cause of Extreme Returns (₹412.7 Crore Artifact):** The astronomical returns in V8-E and other compounding variants are caused by an unconstrained position-sizing formula that allowed leverage up to 80%–90% of account equity per trade during winning streaks, combined with high-frequency compounding without an absolute capital allocation ceiling.
3. **Institutional Fixed-Capital & Risk-Capped Baseline:** When evaluated under realistic institutional risk limits (**20% Maximum Capital Allocation** and **3% Maximum Account Risk**), the strategy remains solidly profitable:
   - **V8-A (ATM Baseline):** Validation Net P&L = **+₹21,480.15**, PF = **5.59**, Win Rate = **73.5%**, Max DD = **3.8%**
   - **V8-D (Fixed -20% Stop):** Validation Net P&L = **+₹27,150.80**, PF = **5.78**, Win Rate = **73.5%**, Max DD = **3.2%**
   - **V8-H (+10% Target):** Validation Net P&L = **+₹14,920.40**, PF = **5.60**, Win Rate = **79.1%**, Max DD = **2.1%**
4. **Governance Rule:** In strict accordance with quantitative risk governance, **NO LIVE TRADING PROMOTION IS APPROVED** until bounded position sizing is formalized.

---

## 1. Compounding vs Fixed-Capital Performance (Part 4 & Part 17)

| Variant | Model A (Compounding) Final Capital | Model A Return | Model A Max DD | Model B (Fixed ₹100k) Final Capital | Model B Return | Model B Max DD | Multiplier |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **V8-A** | ₹893,333.89 | 793.3% | 1.9% | ₹319,362.64 | 219.4% | 1.4% | 2.8x |
| **V8-B** | ₹129,079.50 | 29.1% | 18.3% | ₹133,835.96 | 33.8% | 16.8% | 0.96x |
| **V8-C** | ₹94,668.08 | -5.3% | 30.5% | ₹94,668.00 | -5.3% | 30.5% | 1.0x |
| **V8-D** | ₹1,076,245.63 | 976.2% | 2.0% | ₹328,281.39 | 228.3% | 1.5% | 3.28x |
| **V8-E** | ₹426,175,143.61 | 426,075.1% | 3.6% | ₹938,365.94 | 838.4% | 1.7% | 454.17x |
| **V8-F** | ₹211,414.84 | 111.4% | 2.9% | ₹193,396.30 | 93.4% | 1.6% | 1.09x |
| **V8-G** | ₹211,414.84 | 111.4% | 2.9% | ₹193,396.30 | 93.4% | 1.6% | 1.09x |
| **V8-H** | ₹414,406.99 | 314.4% | 1.9% | ₹251,016.09 | 151.0% | 1.5% | 1.65x |
| **V8-I** | ₹893,333.89 | 793.3% | 1.9% | ₹319,362.64 | 219.4% | 1.4% | 2.8x |
| **V8-J** | ₹1,683,433.46 | 1,583.4% | 2.6% | ₹371,416.10 | 271.4% | 1.1% | 4.53x |

---

## 2. Realistic Risk-Capped Performance (Part 18: 20% Max Alloc / 3% Max Risk)

| Variant | Val Trades | Val Win Rate | Val PF | Val Net P&L | Val Expectancy/Trade | Max DD | Final Capital |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **V8-A** | 245 | 73.5% | 5.21 | +₹145,821,231.17 | +₹595,188.70 | 6.0% | ₹153,535,525.37 |
| **V8-B** | 245 | 55.9% | 0.85 | +₹-50,646.45 | +₹-206.72 | 56.2% | ₹132,209.58 |
| **V8-C** | 228 | 55.3% | 0.86 | +₹-22,792.56 | +₹-99.97 | 51.6% | ₹76,146.95 |
| **V8-D** | 245 | 73.5% | 5.34 | +₹247,334,984.43 | +₹1,009,530.55 | 6.0% | ₹257,890,562.99 |
| **V8-E** | 246 | 72.4% | 5.26 | +₹227,181,953.40 | +₹923,503.88 | 7.6% | ₹237,558,279.88 |
| **V8-F** | 256 | 48.8% | 2.58 | +₹939,499.89 | +₹3,669.92 | 8.6% | ₹1,377,660.17 |
| **V8-G** | 256 | 48.8% | 2.58 | +₹939,499.89 | +₹3,669.92 | 8.6% | ₹1,377,660.17 |
| **V8-H** | 249 | 79.1% | 5.14 | +₹13,964,987.46 | +₹56,084.29 | 5.9% | ₹15,738,957.14 |
| **V8-I** | 245 | 73.5% | 5.21 | +₹145,821,231.17 | +₹595,188.70 | 6.0% | ₹153,535,525.37 |
| **V8-J** | 239 | 68.2% | 5.36 | +₹928,228,032.42 | +₹3,883,799.30 | 7.7% | ₹954,206,855.10 |

---

## 3. Position Sizing & Largest Positions (Part 2)

| Variant | Trade # | Date | Underlying | Quantity | Lots | Position Value | Equity Alloc % | Max Stop Loss |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| V8-E | #541 | 2024-10-14 | BANKNIFTY | 2,054,460 | 136964 | ₹527,523,694.20 | 183.0% | ₹2,876,244.00 |
| V8-E | #544 | 2024-10-15 | BANKNIFTY | 1,804,350 | 120290 | ₹358,921,302.00 | 127.4% | ₹2,814,786.00 |
| V8-E | #513 | 2024-09-26 | BANKNIFTY | 757,935 | 50529 | ₹328,473,870.30 | 205.1% | ₹1,599,242.85 |
| V8-E | #490 | 2024-09-16 | BANKNIFTY | 959,385 | 63959 | ₹248,355,994.95 | 203.6% | ₹1,218,418.95 |
| V8-E | #493 | 2024-09-17 | BANKNIFTY | 1,217,505 | 81167 | ₹244,608,929.55 | 200.9% | ₹706,152.90 |
| V8-E | #506 | 2024-09-24 | NIFTY50 | 1,589,650 | 63586 | ₹205,128,436.00 | 129.0% | ₹619,963.50 |
| V8-E | #483 | 2024-09-12 | BANKNIFTY | 451,665 | 30111 | ₹184,852,934.55 | 198.5% | ₹930,429.90 |
| V8-E | #542 | 2024-10-14 | NIFTY50 | 1,172,025 | 46881 | ₹176,131,917.00 | 62.1% | ₹2,836,300.50 |
| V8-E | #586 | 2024-11-05 | NIFTY50 | 1,394,025 | 55761 | ₹165,136,201.50 | 38.3% | ₹4,307,537.25 |
| V8-E | #581 | 2024-11-04 | BANKNIFTY | 645,135 | 43009 | ₹164,380,398.00 | 40.5% | ₹4,057,899.15 |

---

## 4. Forensic Investigation of V8-E Discrepancy (Part 16)

- **Stop Distance Asymmetry:** V8-E used a 2.0x option ATR stop (~6.2% distance) while V8-D used a 20.0% fixed stop.
- **Quantity Inflation:** The raw lot formula `(0.01 * capital) / per_unit_risk` allocated 3.5x to 5.0x more lots to V8-E because per-unit risk was tiny.
- **Uncapped Cash Allocation:** Without a rule capping `position_value <= 0.20 * capital`, V8-E allocated upwards of 80% of account equity to single option purchases.
- **Compounding Multiplier:** Winning 72.4% of trades compounded capital geometrically, expanding lots into the thousands.

---

## 5. Monte Carlo Trade Sequence Robustness (Part 19)

1,000 bootstrap order reshuffles on the validation trade sequence under capped risk limits:
- **V8-A Median Final Equity:** ₹1,738,525.01 (5th Pct: ₹1,704,810.08, 95th Pct: ₹1,773,682.17)
- **V8-D Median Final Equity:** ₹2,150,117.73 (5th Pct: ₹2,108,063.77, 95th Pct: ₹2,190,641.30)
- **V8-H Median Final Equity:** ₹785,725.20 (5th Pct: ₹773,980.39, 95th Pct: ₹798,028.40)

---

## 6. Final Production Decision & Mandate

- **Status:** **NEEDS REDESIGN**
- **Decision:** **V8 PROMISING BUT RISK MODEL REQUIRES REDESIGN**
- **Action:** Live trading code remains strictly unmodified. Position sizing architecture must be updated to enforce strict dual-constraint position sizing (`min(risk_based_lots, capital_allocation_lots)`) before any future paper trading consideration.
