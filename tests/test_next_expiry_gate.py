"""Orders: the current expiry only by default; the next expiry too only with 'Next Expiry ▸
Enable' ticked; a far (monthly) expiry never.
Run: python3 tests/test_next_expiry_gate.py"""
import sys, os, types, importlib.util, tempfile
from datetime import datetime
tk=types.ModuleType("tkinter")
class _W:
    def __init__(s,*a,**k): pass
    def __getattr__(s,n): return lambda *a,**k: None
for n in ["Tk","Label","Frame","Button","StringVar","BooleanVar","IntVar","Radiobutton","Checkbutton","Toplevel","Entry","Canvas","Scrollbar","Text","Menu","OptionMenu","Spinbox","PhotoImage","Listbox"]:
    setattr(tk,n,_W)
ttk=types.ModuleType("tkinter.ttk"); ttk.Combobox=_W; ttk.Treeview=_W; ttk.Style=_W; ttk.Scrollbar=_W; ttk.Notebook=_W; ttk.Progressbar=_W
mb=types.ModuleType("tkinter.messagebox"); tk.ttk=ttk; tk.messagebox=mb
sys.modules.update({"tkinter":tk,"tkinter.ttk":ttk,"tkinter.messagebox":mb})
_src=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","Options-angel_live-ok.py")
_spec=importlib.util.spec_from_file_location("app",_src); app=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(app)
class FakeDT(datetime):
    v=datetime(2026,10,9,11,25,0)   # Friday, market hours
    @classmethod
    def now(cls,tz=None): return cls.v
app.datetime=FakeDT
os.environ.pop("FORCE_MARKET_OPEN",None)

CUR, NXT, FAR = "13OCT2026", "20OCT2026", "27OCT2026"

# A. which expiries are allowed
assert app.allowed_entry_expiries(CUR, NXT, True) == [CUR, NXT]        # ticked: current + next
assert app.allowed_entry_expiries(CUR, NXT, False) == [CUR]            # unticked: current only
assert app.allowed_entry_expiries(CUR, CUR, True) == [CUR]
assert app.allowed_entry_expiries("", NXT, False) is None              # no current expiry yet: don't block
print("A allowed expiries: OK")

# B. tracker opens no trade (and so places no order) on the next expiry when unticked
orders=[]
class FakeOrders:
    def place_order(self, trade, levels=None, bid=0.0, ask=0.0):
        orders.append(trade["Expiry"]); return "QUEUED"
app.live_order_manager=FakeOrders()

def entry(t, exp, strike):
    return t.log_entry("NIFTY","CE",strike,"BUY",85.0,77.0,120.2,184.4,timeframe="15M Intraday",expiry=exp)

t=app.ForwardTestTracker(os.path.join(tempfile.mkdtemp(),"fwd.csv"))
t.set_allowed_expiries(app.allowed_entry_expiries(CUR, NXT, False))
assert entry(t, NXT, 22400) is None
assert entry(t, NXT.lower(), 22450) is None
assert entry(t, CUR, 22400) is not None
assert orders == [CUR], orders
print("B unticked: next-expiry entry skipped, current-expiry order placed: OK")

# C. ticked: next expiry trades/orders come back, a far expiry still never
t.set_allowed_expiries(app.allowed_entry_expiries(CUR, NXT, True))
assert entry(t, NXT, 22500) is not None
assert entry(t, FAR, 22550) is None
assert orders == [CUR, NXT], orders
print("C ticked: next-expiry order placed, far expiry still skipped: OK")
print("NEXT EXPIRY GATE TESTS PASSED")
