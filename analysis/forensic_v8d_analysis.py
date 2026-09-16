import pandas as pd
import numpy as np

pd.set_option('display.width', 220)
pd.set_option('display.max_columns', 50)

df = pd.read_csv('strategy_v8_execution_research.csv')

def stats(g, label=""):
    n = len(g)
    if n == 0:
        return {"label": label, "trades": 0}
    wins = g[g['net_pnl'] > 0]
    losses = g[g['net_pnl'] <= 0]
    win_rate = len(wins) / n * 100
    avg_win = wins['net_pnl'].mean() if len(wins) else 0.0
    avg_loss = losses['net_pnl'].mean() if len(losses) else 0.0
    gross_profit = wins['net_pnl'].sum()
    gross_loss = -losses['net_pnl'].sum()
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    expectancy = g['net_pnl'].mean()
    net_pnl = g['net_pnl'].sum()
    gross_pnl_total = g['gross_pnl'].sum()
    cost_total = g['total_cost'].sum()
    # max drawdown on cumulative net_pnl in original row order
    cum = g['net_pnl'].cumsum()
    peak = cum.cummax()
    dd = (cum - peak).min()
    return {
        "label": label, "trades": n, "win_rate_pct": round(win_rate,1),
        "avg_win": round(avg_win,1), "avg_loss": round(avg_loss,1),
        "profit_factor": round(pf,3) if pf != float('inf') else None,
        "expectancy": round(expectancy,1), "net_pnl": round(net_pnl,0),
        "gross_pnl": round(gross_pnl_total,0), "total_cost": round(cost_total,0),
        "max_drawdown": round(dd,0),
    }

print("="*100)
print("SECTION 1: BY VARIANT, VALIDATION (out-of-sample) PERIOD ONLY")
print("="*100)
val = df[df['period']=='VALIDATION']
rows = []
for v, g in val.groupby('variant'):
    rows.append(stats(g, v))
res = pd.DataFrame(rows).sort_values('expectancy', ascending=False)
print(res.to_string(index=False))

print()
print("="*100)
print("SECTION 2: V8-D (current production variant) DEEP DIVE — VALIDATION PERIOD")
print("="*100)
d = val[val['variant']=='V8-D'].copy()
print(f"n={len(d)}")
print(stats(d, "V8-D VALIDATION overall"))

print()
print("--- By exit_reason ---")
for r, g in d.groupby('exit_reason'):
    print(stats(g, r))

print()
print("--- By option_type (CE vs PE) ---")
for r, g in d.groupby('option_type'):
    print(stats(g, r))

print()
print("--- By underlying ---")
for r, g in d.groupby('underlying'):
    print(stats(g, r))

print()
print("--- By setup_score bucket ---")
d['score_bucket'] = pd.cut(d['setup_score'], bins=[0,49,59,69,79,89,100], include_lowest=True)
for r, g in d.groupby('score_bucket', observed=True):
    print(stats(g, str(r)))

print()
print("--- By entry_hour ---")
for r, g in sorted(d.groupby('entry_hour'), key=lambda x: x[0]):
    print(stats(g, f"hour={r}"))

print()
print("--- By is_expiry_day ---")
for r, g in d.groupby('is_expiry_day'):
    print(stats(g, f"expiry_day={r}"))

print()
print("--- MFE/MAE analysis for STOP_LOSS-exited trades ---")
sl = d[d['exit_reason'].str.contains('STOP_LOSS')]
print(f"n={len(sl)}")
print("mean MFE% (favorable move reached before stop):", round(sl['mfe_pct'].mean(),2))
print("mean MAE% (adverse move / should equal near -SL):", round(sl['mae_pct'].mean(),2))
print("fraction of SL-hit trades where MFE% > 0 (i.e. was in profit before reversing to stop):",
      round((sl['mfe_pct']>0).mean()*100,1), "%")
print("fraction where MFE%% was already > target distance implied (would have hit target if held longer)")
# crude: compare mfe_pct to the eventual target's implied % (approx via option_return at target trades average)
target_trades = d[d['exit_reason']=='TARGET']
if len(target_trades):
    avg_target_return_pct = target_trades['option_return_pct'].mean()
    print(f"avg option_return_pct on TARGET-hit trades: {avg_target_return_pct:.1f}%")
    near_miss = sl[sl['mfe_pct'] >= avg_target_return_pct * 0.8]
    print(f"SL-hit trades whose MFE reached >=80% of typical target return before reversing: {len(near_miss)} ({len(near_miss)/len(sl)*100:.1f}% of SL trades)")

print()
print("--- Consecutive losses (chronological, V8-D validation) ---")
d_sorted = d.sort_values(['date','entry_timestamp'])
streak = 0; maxstreak = 0
for pnl in d_sorted['net_pnl']:
    if pnl <= 0:
        streak += 1
        maxstreak = max(maxstreak, streak)
    else:
        streak = 0
print("max consecutive losses:", maxstreak)

print()
print("--- Transaction cost impact ---")
print("gross_pnl sum:", round(d['gross_pnl'].sum(),0))
print("total_cost sum:", round(d['total_cost'].sum(),0))
print("net_pnl sum:", round(d['net_pnl'].sum(),0))
print("cost as % of gross profit (wins only):", )
wins = d[d['gross_pnl']>0]
if len(wins):
    print(round(wins['total_cost'].sum() / wins['gross_pnl'].sum() * 100, 1), "%")

print()
print("--- gap_through_stop / same_candle_conflict frequency ---")
print("gap_through_stop count:", d['gap_through_stop'].sum(), "/", len(d))
print("same_candle_conflict count:", d['same_candle_conflict'].sum(), "/", len(d))
