"""S2 break-and-reclaim that jumps past the 1% entry band is still taken (AJ, 9 Nov).
Run: python3 tests/test_buy_chase.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_strategy_rules.py")).read().split("# Entry rule (AJ, 8 Oct)")[0])

# helper: band, chase room (30% of risk), never at/above T1, never below the entry
assert app.buy_entry_ok(100.5, 100, ll=90, t1=120)          # inside 1% band
assert app.buy_entry_ok(102.9, 100, ll=90, t1=120)          # 2.9 <= 0.3*10
assert not app.buy_entry_ok(103.2, 100, ll=90, t1=120)      # more than 30% of the risk
assert not app.buy_entry_ok(102.0, 100, ll=90, t1=101.5)    # at/above T1
assert not app.buy_entry_ok(99.0, 100, ll=90, t1=120)       # below the entry
assert not app.buy_entry_ok(723.05, 716.2, ll=712.7, t1=718.25)  # inside 1% band but past T1 (25550 PE, 9 Nov)
print("buy_entry_ok: OK")

# Sweep below S2, 09:30 candle closes green above S2 (its wick is the retest), then the next
# tick is already 2% over the confirmation high: the old 1% band never entered.
LL = round(S2*0.94, 2)
path = [(at(9,25),S2*1.05),(at(9,31),LL),(at(9,33),S2*1.003),(at(9,34),S2*1.01),(at(9,35,5),S2*1.008),(at(9,35,20),S2*1.01*1.02)]
st = run(PIV, path, "BUY", "PE")
assert st.state == "CONFIRMED_BUY" and st.trade_status == "Active", (st.state, st.signal)
assert st.sl == LL and st.entry_price == round(S2*1.01, 2), (st.sl, st.entry_price)
print("jump over the band still enters @ %.2f SL %.2f: OK" % (st.entry_price, st.sl))

# Ran far away (more than 30% of the risk): wait, and enter when it comes back
far = path[:-1] + [(at(9,35,20),S2*1.10)]
st = run(PIV, far, "BUY", "PE")
assert st.trade_status == "Waiting" and st.state == "TRADING_IN_FAVOR_BUY", st.state
print("ran far away: waits: OK")

# A confirmed BUY that the app could not record goes back to watching, not stuck Active
st = run(PIV, path, "BUY", "PE"); st.unconfirm_buy()
assert st.trade_status == "Waiting" and st.state == "TRADING_IN_FAVOR_BUY" and st.conf_high > 0 and st.ll == LL
st.update_tick(S2*1.012, PIV, at(9,36), opt_high=S2*1.1, opt_low=LL)
assert st.trade_status == "Active", st.signal
print("unrecorded entry re-arms: OK")
