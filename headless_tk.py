"""
A window-less stand-in for tkinter, used by glass_server.py.

The launcher's ControlPanel (Options-angel_live-ok.py) keeps all its state in Tk variables and
fills a Treeview with the reversal dashboard rows. With this module installed as `tkinter`, the
very same ControlPanel code runs with no window: variables keep their values, the Treeview keeps
its rows, and `after()` callbacks run on one event-loop thread. glass_dashboard.py then reads and
drives it exactly as it would a real window, so no strategy or display logic is copied.

install() must run before Options-angel_live-ok.py is imported.
"""

import heapq
import itertools
import sys
import threading
import time
import types


class _Loop:
    """after()/mainloop() for the whole process: callbacks run in order on the thread that calls mainloop()."""

    def __init__(self):
        self._q = []
        self._seq = itertools.count()
        self._ids = itertools.count(1)
        self._cancelled = set()
        self._cv = threading.Condition()
        self._quit = False

    def after(self, ms, func=None, *args):
        if func is None:
            time.sleep((ms or 0) / 1000.0)
            return None
        aid = f"after#{next(self._ids)}"
        with self._cv:
            heapq.heappush(self._q, (time.monotonic() + max(0, ms or 0) / 1000.0, next(self._seq), aid, func, args))
            self._cv.notify()
        return aid

    def after_cancel(self, aid):
        with self._cv:
            self._cancelled.add(aid)

    def quit(self):
        with self._cv:
            self._quit = True
            self._cv.notify()

    def mainloop(self):
        while True:
            with self._cv:
                while not self._quit and (not self._q or self._q[0][0] > time.monotonic()):
                    self._cv.wait(timeout=(self._q[0][0] - time.monotonic()) if self._q else 0.5)
                if self._quit:
                    return
                _, _, aid, func, args = heapq.heappop(self._q)
                if aid in self._cancelled:
                    self._cancelled.discard(aid)
                    continue
            try:
                func(*args)
            except Exception as e:     # one failing callback must never stop the loop
                import traceback
                print(f"[HEADLESS] callback error: {e}")
                traceback.print_exc()


LOOP = _Loop()


# ── variables ────────────────────────────────────────────
class Variable:
    _default = ""

    def __init__(self, master=None, value=None, name=None):
        self._v = self._default if value is None else value
        self._traces = []

    def get(self):
        return self._v

    def set(self, value):
        self._v = value
        for cb in list(self._traces):
            try:
                cb("", "", "write")
            except Exception:
                pass

    def trace_add(self, mode, callback):
        self._traces.append(callback)
        return str(len(self._traces))

    def trace_remove(self, *a):
        pass


class StringVar(Variable):
    _default = ""

    def get(self):
        return "" if self._v is None else str(self._v)


class IntVar(Variable):
    _default = 0

    def get(self):
        try:
            return int(float(self._v))
        except (TypeError, ValueError):
            return 0


class DoubleVar(Variable):
    _default = 0.0

    def get(self):
        try:
            return float(self._v)
        except (TypeError, ValueError):
            return 0.0


class BooleanVar(Variable):
    _default = False

    def get(self):
        return bool(self._v)


# ── widgets ──────────────────────────────────────────────
class Misc:
    """Every widget: keeps its options, ignores layout and drawing."""

    def __init__(self, master=None, *args, **kw):
        self.master = master
        self._opts = dict(kw)
        self._children = []
        if isinstance(master, Misc):
            master._children.append(self)

    # options
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        self._opts.update(kw)

    config = configure

    def cget(self, key):
        return self._opts.get(key, "")

    def __getitem__(self, key):
        return self._opts.get(key, "")

    def __setitem__(self, key, value):
        self._opts[key] = value

    # event loop
    def after(self, ms, func=None, *args):
        return LOOP.after(ms, func, *args)

    def after_idle(self, func, *args):
        return LOOP.after(0, func, *args)

    def after_cancel(self, aid):
        LOOP.after_cancel(aid)

    def mainloop(self, n=0):
        LOOP.mainloop()

    def quit(self):
        LOOP.quit()

    def destroy(self):
        self._destroyed = True

    def winfo_exists(self):
        return 0 if getattr(self, "_destroyed", False) else 1

    def winfo_children(self):
        return list(self._children)

    def __getattr__(self, name):
        # pack / grid / bind / lift / title / geometry / focus / ... : nothing to draw
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class Tk(Misc):
    def __init__(self, *a, **kw):
        super().__init__(None)


class Toplevel(Misc):
    pass


Frame = Label = Button = Checkbutton = Radiobutton = Entry = Scale = Canvas = Scrollbar = Text = \
    Listbox = Menu = OptionMenu = Spinbox = LabelFrame = PanedWindow = Message = Toplevel


class PhotoImage(Misc):
    pass


class Treeview(Misc):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        cols = kw.get("columns", ())
        self._opts["columns"] = tuple(cols) if not isinstance(cols, str) else (cols,)
        self._items = {}          # iid -> {"values": tuple, "tags": tuple}
        self._order = []
        self._n = itertools.count(1)

    def insert(self, parent, index, iid=None, **kw):
        iid = iid or f"I{next(self._n):05d}"
        self._items[iid] = {"values": tuple(kw.get("values", ())), "tags": tuple(kw.get("tags", ()) or ())}
        if index == "end":
            self._order.append(iid)
        else:
            self._order.insert(int(index), iid)
        return iid

    def item(self, iid, option=None, **kw):
        it = self._items[iid]
        if kw:
            if "values" in kw:
                it["values"] = tuple(kw["values"])
            if "tags" in kw:
                t = kw["tags"]
                it["tags"] = (t,) if isinstance(t, str) else tuple(t or ())
            return None
        if option:
            return it.get(option, "")
        return dict(it)

    def get_children(self, item=""):
        return tuple(self._order)

    def delete(self, *iids):
        for iid in iids:
            if iid in self._items:
                del self._items[iid]
                self._order.remove(iid)

    def set(self, iid, column=None, value=None):
        return None


class Combobox(Misc):
    pass


class Style(Misc):
    pass


Notebook = Progressbar = Separator = Combobox
ttk_Scrollbar = Scrollbar


# ── messagebox: shown on the web page instead of a window ──
SINK = []          # callables(kind, title, message); glass_server adds the page's notes


def _box(kind, default=None):
    def show(title="", message="", **kw):
        print(f"[HEADLESS] {kind}: {title}: {message}")
        for fn in list(SINK):
            try:
                fn(kind, title, message)
            except Exception:
                pass
        return default
    return show


def install():
    """Make `import tkinter` (and tkinter.ttk / tkinter.messagebox) give this module's classes."""
    tk = types.ModuleType("tkinter")
    for n in ("Tk", "Toplevel", "Frame", "Label", "Button", "Checkbutton", "Radiobutton", "Entry", "Scale",
              "Canvas", "Scrollbar", "Text", "Listbox", "Menu", "OptionMenu", "Spinbox", "LabelFrame",
              "PanedWindow", "Message", "PhotoImage", "Misc", "Variable", "StringVar", "IntVar", "DoubleVar",
              "BooleanVar"):
        setattr(tk, n, globals()[n])
    for n in ("END", "LEFT", "RIGHT", "TOP", "BOTTOM", "BOTH", "X", "Y", "W", "E", "N", "S", "NW", "NE",
              "SW", "SE", "CENTER", "HORIZONTAL", "VERTICAL", "DISABLED", "NORMAL"):
        setattr(tk, n, n.lower())
    tk.TclError = RuntimeError
    ttk = types.ModuleType("tkinter.ttk")
    for n in ("Treeview", "Combobox", "Style", "Notebook", "Progressbar", "Separator"):
        setattr(ttk, n, globals()[n])
    for n in ("Frame", "Label", "Button", "Checkbutton", "Radiobutton", "Entry", "Scale", "Scrollbar",
              "Spinbox", "LabelFrame", "PanedWindow"):
        setattr(ttk, n, globals()[n])
    mb = types.ModuleType("tkinter.messagebox")
    mb.showinfo, mb.showwarning, mb.showerror = _box("info"), _box("warning"), _box("error")
    mb.askyesno, mb.askokcancel, mb.askquestion = _box("ask", False), _box("ask", False), _box("ask", "no")
    tk.ttk, tk.messagebox = ttk, mb
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb})
    return tk
