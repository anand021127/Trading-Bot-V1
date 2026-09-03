import pickle
import json
import statistics
from datetime import datetime
from collections import Counter, defaultdict

with open('/tmp/res_combined.pkl', 'rb') as f:
    res = pickle.load(f)

trades = res.trade_log
print(f"Total trades loaded: {len(trades)}")

# 1. TRADE LEVEL & CONTRACT VALIDATION
symbols = defaultdict(list)
ce_count = 0
pe_count = 0
unknown_type = 0
contract_failures = 0
spot_substituted_count = 0
invalid_lot_sizes = 0

for t in trades:
    sym = t.get('symbol')
    symbols[sym].append(t)
    opt_type = t.get('option_type')
    if opt_type == 'CE':
        ce_count += 1
    elif opt_type == 'PE':
        pe_count += 1
    else:
        unknown_type += 1
        
    if not t.get('strike') or not t.get('expiry') or not t.get('option_symbol'):
        contract_failures += 1
    if t.get('entry_price', 0) > 2000: # Clearly spot index price, not option premium
        spot_substituted_count += 1
    if t.get('lot_size') == 1:
        invalid_lot_sizes += 1

print("\n--- CONTRACT VALIDATION ---")
print(f"Total trades: {len(trades)}")
print(f"CE: {ce_count}, PE: {pe_count}, Unknown/Empty: {unknown_type}")
print(f"Contract failures: {contract_failures} / {len(trades)} ({contract_failures/len(trades)*100:.1f}%)")
print(f"Spot substituted for option premium: {spot_substituted_count} / {len(trades)} ({spot_substituted_count/len(trades)*100:.1f}%)")
print(f"Invalid lot size (lot_size=1): {invalid_lot_sizes} / {len(trades)}")

print("\n--- SUMMARY TABLE: OPTION CONTRACT VALIDATION ---")
print(f"{'Symbol':<12} {'Trades':<8} {'CE':<6} {'PE':<6} {'Contract failures':<18}")
print("-" * 52)
for sym in sorted(symbols.keys()):
    sym_trades = symbols[sym]
    sym_ce = sum(1 for t in sym_trades if t.get('option_type') == 'CE')
    sym_pe = sum(1 for t in sym_trades if t.get('option_type') == 'PE')
    sym_fail = sum(1 for t in sym_trades if not t.get('strike') or not t.get('expiry') or not t.get('option_symbol'))
    print(f"{sym:<12} {len(sym_trades):<8} {sym_ce:<6} {sym_pe:<6} {sym_fail:<18}")

# 2. TRANSACTION COST FORENSICS
brokerage = sum(t.get('brokerage', 0.0) for t in trades)
stt = sum(t.get('stt', 0.0) for t in trades)
exchange_charges = sum(t.get('exchange_charges', 0.0) for t in trades)
gst = sum(t.get('gst', 0.0) for t in trades)
sebi = sum(t.get('sebi_charges', 0.0) for t in trades)
stamp_duty = sum(t.get('stamp_duty', 0.0) for t in trades)
slippage = sum(t.get('slippage', 0.0) for t in trades)
total_charges = sum(t.get('total_cost', 0.0) for t in trades)

turnovers = [(t['entry_price'] + t['exit_price']) * t['quantity'] for t in trades]
total_turnover = sum(turnovers)
costs = [t.get('total_cost', 0.0) for t in trades]
gross_pnls = [t.get('gross_pnl', 0.0) for t in trades]
net_pnls = [t.get('net_pnl', 0.0) for t in trades]
total_gross_pnl = sum(gross_pnls)
total_net_pnl = sum(net_pnls)

print("\n--- TRANSACTION COST FORENSICS ---")
print(f"Total Reported Charges: ₹{res.total_charges:.2f}")
print(f"Sum of Individual Trade Charges: ₹{total_charges:.2f}")
print(f"  - Brokerage:         ₹{brokerage:10.2f} ({brokerage/total_charges*100:5.2f}%)")
print(f"  - STT:               ₹{stt:10.2f} ({stt/total_charges*100:5.2f}%)")
print(f"  - Exchange Charges:  ₹{exchange_charges:10.2f} ({exchange_charges/total_charges*100:5.2f}%)")
print(f"  - GST:               ₹{gst:10.2f} ({gst/total_charges*100:5.2f}%)")
print(f"  - SEBI Charges:      ₹{sebi:10.2f} ({sebi/total_charges*100:5.2f}%)")
print(f"  - Stamp Duty:        ₹{stamp_duty:10.2f} ({stamp_duty/total_charges*100:5.2f}%)")
print(f"  - Slippage:          ₹{slippage:10.2f} ({slippage/total_charges*100:5.2f}%)")
print(f"  - Total Turnover:    ₹{total_turnover:12.2f}")
print(f"  - Total Gross P&L:   ₹{total_gross_pnl:10.2f}")
print(f"  - Total Net P&L:     ₹{total_net_pnl:10.2f}")

avg_cost = statistics.mean(costs)
med_cost = statistics.median(costs)
min_cost = min(costs)
max_cost = max(costs)
cost_pct_turnover = (total_charges / total_turnover) * 100
cost_pct_gross = (total_charges / abs(total_gross_pnl)) * 100

print(f"\nCost Statistics per Trade:")
print(f"  - Average Cost / Trade: ₹{avg_cost:.2f}")
print(f"  - Median Cost / Trade:  ₹{med_cost:.2f}")
print(f"  - Min Cost / Trade:     ₹{min_cost:.2f}")
print(f"  - Max Cost / Trade:     ₹{max_cost:.2f}")
print(f"  - Cost as % of Turnover:   {cost_pct_turnover:.4f}%")
print(f"  - Cost as % of Gross P&L:  {cost_pct_gross:.2f}% (Charges are {total_charges/abs(total_gross_pnl):.1f}x Gross P&L!)")

# 3. QUANTITY & LOT-SIZE
print("\n--- QUANTITY & LOT-SIZE VALIDATION ---")
for sym, t_list in symbols.items():
    quantities = [t['quantity'] for t in t_list]
    lot_sizes = [t['lot_size'] for t in t_list]
    print(f"{sym}: Trades={len(t_list)}, Quantities={sorted(list(set(quantities)))}, Lot Sizes={set(lot_sizes)}")

# 4. TRADE FREQUENCY & TEMPORAL ANALYSIS
entry_dates = [t['entry_time'][:10] for t in trades]
unique_dates = sorted(list(set(entry_dates)))
trades_per_day = Counter(entry_dates)
days_count = len(unique_dates)

weeks = [datetime.fromisoformat(t['entry_time']).strftime('%Y-W%W') for t in trades]
months = [t['entry_time'][:7] for t in trades]
hours = [int(t['entry_time'][11:13]) for t in trades]

def get_session(t_str):
    time_part = t_str[11:16]
    if "09:15" <= time_part < "11:30":
        return "MORNING"
    elif "11:30" <= time_part < "13:00":
        return "MIDDAY_LULL"
    elif "13:00" <= time_part <= "15:30":
        return "AFTERNOON"
    return "OTHER"

sessions = [get_session(t['entry_time']) for t in trades]
setups = [t.get('strategy', 'UNKNOWN') for t in trades]
confidences = [t.get('confidence', 0.0) for t in trades]

def conf_bucket(c):
    if c >= 90: return "90-100"
    elif c >= 80: return "80-89"
    elif c >= 70: return "70-79"
    elif c >= 60: return "60-69"
    else: return "<60"

conf_buckets = [conf_bucket(c) for c in confidences]

print("\n--- TRADE FREQUENCY BY SYMBOL ---")
print(f"{'Symbol':<12} {'Trades':<8} {'Avg/day':<10} {'Avg/week':<10} {'Net P&L':<12}")
print("-" * 54)
total_weeks = max(1, len(set(weeks)))
for sym in sorted(symbols.keys()):
    t_list = symbols[sym]
    sym_days = len(set(t['entry_time'][:10] for t in t_list))
    sym_weeks = len(set(datetime.fromisoformat(t['entry_time']).strftime('%Y-W%W') for t in t_list))
    avg_day = len(t_list) / max(1, sym_days)
    avg_week = len(t_list) / max(1, sym_weeks)
    net_p = sum(t['net_pnl'] for t in t_list)
    print(f"{sym:<12} {len(t_list):<8} {avg_day:<10.2f} {avg_week:<10.2f} ₹{net_p:<10.2f}")

print(f"\nTrading Days: {days_count}, Trading Weeks: {total_weeks}")
print(f"Overall Avg Trades / Day:  {len(trades)/days_count:.2f}")
print(f"Overall Avg Trades / Week: {len(trades)/total_weeks:.2f}")
print(f"Trades by Month: {dict(Counter(months))}")
print(f"Trades by Hour:  {dict(sorted(Counter(hours).items()))}")
print(f"Trades by Session: {dict(Counter(sessions))}")
print(f"Trades by Confidence Bucket: {dict(Counter(conf_buckets))}")

# 5. CHURN & RE-ENTRY DETECTION
print("\n--- CHURN, RE-ENTRY & DUPLICATE ANALYSIS ---")
reentries_same_symbol_5m = 0
reentries_same_symbol_15m = 0
immediate_reentries = 0

for sym, t_list in symbols.items():
    s_trades = sorted(t_list, key=lambda x: x['entry_time'])
    for i in range(len(s_trades) - 1):
        curr = s_trades[i]
        nxt = s_trades[i+1]
        exit_dt = datetime.fromisoformat(curr['exit_time'])
        next_entry_dt = datetime.fromisoformat(nxt['entry_time'])
        gap_mins = (next_entry_dt - exit_dt).total_seconds() / 60.0
        if gap_mins == 0:
            immediate_reentries += 1
        if 0 <= gap_mins <= 5:
            reentries_same_symbol_5m += 1
        if 0 <= gap_mins <= 15:
            reentries_same_symbol_15m += 1
            
print(f"Immediate re-entry after exit (0 min gap): {immediate_reentries}")
print(f"Re-entry within 5 minutes of exit:        {reentries_same_symbol_5m}")
print(f"Re-entry within 15 minutes of exit:       {reentries_same_symbol_15m}")

# 6. REJECTION BREAKDOWN
print("\n--- REJECTION BREAKDOWN (All 81,938 non-executed signals) ---")
total_rejections = res.rejected_signals_total_count
print(f"Total Rejected Signals: {total_rejections}")
sorted_reasons = sorted(res.rejection_reason_counts.items(), key=lambda x: -x[1])
for reason, count in sorted_reasons:
    pct = (count / total_rejections) * 100
    print(f"  - {reason:<65}: {count:6d} ({pct:5.2f}%)")

# 7. P&L QUALITY & PERFORMANCE
print("\n--- P&L QUALITY & PERFORMANCE ---")
winning_trades = [t for t in trades if t.get('net_pnl', 0.0) > 0]
losing_trades = [t for t in trades if t.get('net_pnl', 0.0) <= 0]
gross_winning = [t for t in trades if t.get('gross_pnl', 0.0) > 0]
gross_losing = [t for t in trades if t.get('gross_pnl', 0.0) < 0]
gross_breakeven = [t for t in trades if t.get('gross_pnl', 0.0) == 0]

win_rate_net = (len(winning_trades) / len(trades)) * 100
win_rate_gross = (len(gross_winning) / len(trades)) * 100
sum_gross_wins = sum(t['gross_pnl'] for t in gross_winning)
sum_gross_losses = abs(sum(t['gross_pnl'] for t in gross_losing))
gross_profit_factor = (sum_gross_wins / sum_gross_losses) if sum_gross_losses > 0 else 0

sum_net_wins = sum(t['net_pnl'] for t in winning_trades)
sum_net_losses = abs(sum(t['net_pnl'] for t in losing_trades))
net_profit_factor = (sum_net_wins / sum_net_losses) if sum_net_losses > 0 else 0

avg_win = (sum_net_wins / len(winning_trades)) if winning_trades else 0
avg_loss = (sum_net_losses / len(losing_trades)) if losing_trades else 0
expectancy = (win_rate_net / 100 * avg_win) - ((100 - win_rate_net) / 100 * avg_loss)

print(f"Gross P&L Total:           ₹{total_gross_pnl:10.2f}")
print(f"Net P&L Total:             ₹{total_net_pnl:10.2f}")
print(f"Gross Winning Trades:      {len(gross_winning)} / {len(trades)} ({win_rate_gross:.2f}%)")
print(f"Gross Losing Trades:       {len(gross_losing)} / {len(trades)}")
print(f"Gross Breakeven Trades:    {len(gross_breakeven)} / {len(trades)}")
print(f"Net Winning Trades:        {len(winning_trades)} / {len(trades)} ({win_rate_net:.2f}%)")
print(f"Net Losing Trades:         {len(losing_trades)} / {len(trades)}")
print(f"Gross Profit Factor:       {gross_profit_factor:.4f}")
print(f"Net Profit Factor:         {net_profit_factor:.4f}")
print(f"Average Win:               ₹{avg_win:.2f}")
print(f"Average Loss:              ₹{avg_loss:.2f}")
print(f"Trade Expectancy:          ₹{expectancy:.2f} per trade")
print(f"Maximum Drawdown:          {res.max_drawdown_pct:.2f}%")

# 8. HOLDING TIME ANALYSIS
holding_durations_mins = []
for t in trades:
    e_dt = datetime.fromisoformat(t['entry_time'])
    x_dt = datetime.fromisoformat(t['exit_time'])
    mins = (x_dt - e_dt).total_seconds() / 60.0
    holding_durations_mins.append(mins)

print("\n--- HOLDING TIME ANALYSIS ---")
print(f"Average Holding Time: {statistics.mean(holding_durations_mins):.2f} minutes ({statistics.mean(holding_durations_mins)/60:.2f} hours)")
print(f"Median Holding Time:  {statistics.median(holding_durations_mins):.2f} minutes")
print(f"Min Holding Time:     {min(holding_durations_mins):.2f} minutes")
print(f"Max Holding Time:     {max(holding_durations_mins):.2f} minutes")
exit_reasons = Counter(t.get('exit_reason', 'UNKNOWN') for t in trades)
print("Exit Reasons Breakdown:")
for reason, count in exit_reasons.most_common():
    print(f"  - {reason:<40}: {count:4d} ({count/len(trades)*100:5.2f}%)")

# 9. LOOKAHEAD AND CANDLE COMPLETION
print("\n--- LOOKAHEAD AND REALISM CHECKS ---")
lookahead_violations = 0
for t in trades:
    e_dt = datetime.fromisoformat(t['entry_time'])
    x_dt = datetime.fromisoformat(t['exit_time'])
    if x_dt < e_dt:
        lookahead_violations += 1
print(f"Lookahead exit < entry violations: {lookahead_violations}")

