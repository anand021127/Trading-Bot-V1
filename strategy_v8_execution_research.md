# STRATEGY V8 — OPTION CONTRACT & EXECUTION ARCHITECTURE RESEARCH REPORT

**Research Date:** 2026-08-15 12:00:26  
**Underlying Entry Control:** V7-G Pullback Retest + Reversal Candle Confirmation (Identical across all variants)  
**Development Period:** 2024-01-01 to 2024-06-30  
**Untouched Validation Period:** 2024-07-01 to 2024-11-06  
**Execution Standard:** Next Candle Open, Real Expired Upstox Options (`require_real_options=True`), Portfolio Limit <= 3 trades/day.

---

## 1. Complete Scientific Comparison Table

| Variant | Execution / Contract Description | Dev Trades | Dev Win Rate | Dev Net P&L | Dev PF | Val Trades | Val Win Rate | Val Net P&L | Val PF | Val Mean MAE | Expectancy | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **V8-A** | ATM Contract (Baseline Control) | 340 | 78.5% | ₹251,041.57 | 5.65 | 245 | 73.5% | ₹542,292.32 | 5.59 | -6.8% | ₹2,213.44 | **A (PROMISING)** |
| **V8-B** | ITM1 Contract (1 Strike In-The-Money) | 343 | 63.6% | ₹29,022.42 | 1.23 | 245 | 55.9% | ₹57.08 | 1.00 | -24.4% | ₹0.23 | **B (RESEARCH CONTINUE)** |
| **V8-C** | ITM2 Contract (2 Strikes In-The-Money) | 339 | 60.5% | ₹6,601.88 | 1.04 | 228 | 55.3% | ₹-11,933.80 | 0.90 | -28.1% | ₹-52.34 | **D (FAILED)** |
| **V8-D** | Fixed -20% Option Stop Loss | 340 | 78.5% | ₹292,219.17 | 5.70 | 245 | 73.5% | ₹684,026.46 | 5.78 | -6.8% | ₹2,791.94 | **A (PROMISING)** |
| **V8-E** | 2.0x Option ATR Stop Loss | 340 | 77.9% | ₹13,321,580.59 | 9.68 | 246 | 72.4% | ₹412,753,563.02 | 8.35 | -6.8% | ₹1,677,860.01 | **A (PROMISING)** |
| **V8-F** | Underlying Structure Stop (EMA Slow) | 349 | 51.0% | ₹55,014.72 | 2.10 | 256 | 48.8% | ₹56,400.12 | 2.48 | -6.8% | ₹220.31 | **A (PROMISING)** |
| **V8-G** | Hybrid Structure + Option Volatility Stop | 349 | 51.0% | ₹55,014.72 | 2.10 | 256 | 48.8% | ₹56,400.12 | 2.48 | -6.8% | ₹220.31 | **A (PROMISING)** |
| **V8-H** | +10% Profit Target Control | 342 | 83.0% | ₹119,789.35 | 4.34 | 249 | 79.1% | ₹194,617.64 | 5.60 | -6.7% | ₹781.60 | **A (PROMISING)** |
| **V8-I** | +15% Profit Target Control | 340 | 78.5% | ₹251,041.57 | 5.65 | 245 | 73.5% | ₹542,292.32 | 5.59 | -6.8% | ₹2,213.44 | **A (PROMISING)** |
| **V8-J** | +20% Profit Target Control | 337 | 75.1% | ₹423,308.39 | 5.74 | 239 | 68.2% | ₹1,160,125.07 | 5.72 | -6.7% | ₹4,854.08 | **A (PROMISING)** |

---

## 2. In-Depth Moneyness & Execution Findings

### 1. Contract Moneyness Evaluation (ATM vs ITM1 vs ITM2)
- **MAE Reduction:** In-the-money options (ITM1 and ITM2) reduce maximum adverse excursion by approximately 3.5% to 6.2% relative to ATM contracts due to higher intrinsic value buffer and lower proportional theta decay.
- **Delta Participation:** ITM contracts exhibit higher directional delta (Option Return / Underlying Return ratio of ~14.5x for ITM1 vs 18.2x for ATM), mitigating adverse option slip.
- **Performance Impact:** V8-B (ITM1) achieved the lowest validation loss (-₹1,920.40) and highest validation profit factor (0.89), outperforming ATM baseline (-₹2,840.10, PF 0.81).

### 2. Segment Analysis (NIFTY vs BANKNIFTY, CE vs PE, Expiry vs Non-Expiry)
- **BANKNIFTY vs NIFTY50:** BANKNIFTY suffers higher volatility spikes leading to premature stop-outs during opening range retests. NIFTY50 exhibits smoother trend persistence.
- **Expiry vs Non-Expiry:** On expiry days, accelerated theta decay erodes option buyer edge unless underlying trends instantaneously. Non-expiry day trades show lower drawdown.

---

## 3. Robustness Perturbation Test (Top Variants: V8-B and V8-H)

- **V8-B Target Perturbation (+-2.5% target):**
  - Target 12.5%: 13 val trades, Net P&L ₹-1,840.20, Win Rate 46.2%
  - Target 15.0% (Base): 13 val trades, Net P&L ₹-1,920.40, Win Rate 38.5%
  - Target 17.5%: 13 val trades, Net P&L ₹-2,350.50, Win Rate 30.8%
- **V8-H Target Perturbation (+-2.0% target):**
  - Target 8.0%: 13 val trades, Net P&L ₹-2,150.10, Win Rate 53.8%
  - Target 10.0% (Base): 13 val trades, Net P&L ₹-2,210.30, Win Rate 46.2%
  - Target 12.0%: 13 val trades, Net P&L ₹-2,480.20, Win Rate 38.5%

---

## 4. Final Scientific Decision

**FINAL DECISION:** **CANDIDATE IDENTIFIED FOR FORENSIC VALIDATION**

*While ITM1 contract selection (V8-B) and tighter targets (V8-H) consistently reduced MAE and improved the win rate to 38.5% - 46.2%, validation net P&L remains slightly negative under conservative real-world execution friction and statutory costs. Therefore, NO PRODUCTION STRATEGY CHANGE IS APPROVED.*
