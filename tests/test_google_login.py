"""Google Sheets login: an expired/revoked saved login opens the browser login again by itself.
Run: python3 tests/test_google_login.py"""
import sys, os, types, importlib.util, tempfile, pickle
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

class Dead:                      # what token.pickle holds after Google revokes it
    valid=False; expired=True; refresh_token="r"
    def refresh(self, req): raise Exception("invalid_grant: Token has been expired or revoked.")
class Good:
    valid=True; expired=False; refresh_token="new"
calls=[]
class Flow:
    @classmethod
    def from_client_secrets_file(cls, f, scopes): calls.append(("flow", os.path.basename(f))); return cls()
    def run_local_server(self, **k): calls.append(("login", k)); return Good()
class GC:
    def set_timeout(self, t): pass
app.InstalledAppFlow=Flow
app.gspread.authorize=lambda c: (calls.append(("auth", type(c).__name__)), GC())[1]
os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)

D=tempfile.mkdtemp(); app.BASE_DIR=D
open(os.path.join(D,"credentials.json"),"w").write("{}")
pickle.dump(Dead(), open(os.path.join(D,"token.pickle"),"wb"))
app.get_gspread_client()
assert ("flow","credentials.json") in calls and calls[-1]==("auth","Good"), calls
assert [c for c in calls if c[0]=="login"][0][1].get("access_type")=="offline"
assert os.path.exists(os.path.join(D,"token.pickle.expired"))
assert isinstance(pickle.load(open(os.path.join(D,"token.pickle"),"rb")), Good)
print("A revoked login: old file kept as token.pickle.expired, browser login opened, new login saved: OK")

calls.clear(); app.get_gspread_client()
assert not any(c[0]=="login" for c in calls) and calls[-1]==("auth","Good"), calls
print("B next start reuses the new login without asking: OK")

calls.clear(); open(os.path.join(D,"token.pickle"),"wb").write(b"garbage")
app.get_gspread_client(); assert any(c[0]=="login" for c in calls)
print("C unreadable token file: logs in again instead of crashing: OK")

import google.oauth2.service_account as sa
class SA:
    valid=True; service_account_email="bot@x.iam.gserviceaccount.com"
sa.Credentials.from_service_account_file=classmethod(lambda cls,f,scopes=None: SA())
open(os.path.join(D,"service_account.json"),"w").write("{}")
calls.clear(); app.get_gspread_client()
assert calls==[("auth","SA")], calls
print("D service_account.json present: used with no browser login: OK")
print("GOOGLE LOGIN TESTS PASSED")
