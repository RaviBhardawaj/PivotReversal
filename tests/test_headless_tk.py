"""The window-less tkinter used by glass_server.py keeps variable values, Treeview rows and runs
after() callbacks in order. Run: python3 tests/test_headless_tk.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import headless_tk
tk = headless_tk.install()
import tkinter, tkinter.ttk as ttk, tkinter.messagebox as mb
assert tkinter is tk

root = tk.Tk()
v, b, n = tk.StringVar(value="NIFTY"), tk.BooleanVar(), tk.IntVar(value=10)
seen = []
v.trace_add("write", lambda *a: seen.append(v.get()))
v.set("SENSEX"); b.set(1)
assert (v.get(), b.get(), n.get(), seen) == ("SENSEX", True, 10, ["SENSEX"])
print("variables: OK")

class Btn(tk.Label):           # like the launcher's DarkButton
    def config(self, **kw):
        super().config(**kw)
lbl = Btn(root, text="x", fg="#fff"); lbl.config(fg="#3fb950"); lbl.pack(side="left"); lbl.bind("<Button-1>", None)
assert lbl.cget("fg") == "#3fb950" and lbl.cget("text") == "x"
cb = ttk.Combobox(root, values=[]); cb["values"] = ["06NOV2026"]
assert cb["values"] == ["06NOV2026"]
print("widgets: OK")

tv = ttk.Treeview(root, columns=("A", "B"))
i1 = tv.insert("", "end", values=(1, 2), tags=("buy_row",)); i2 = tv.insert("", "end", values=(3, 4))
tv.item(i1, values=(5, 6), tags=("target",)); tv.delete(i2)
assert tv["columns"] == ("A", "B") and tv.get_children() == (i1,)
assert tv.item(i1, "values") == (5, 6) and tv.item(i1, "tags") == ("target",)
print("treeview: OK")

got = []
headless_tk.SINK.append(lambda kind, t, m: got.append((kind, t)))
assert mb.askyesno("Q", "?") is False; mb.showerror("E", "boom")
assert got == [("ask", "Q"), ("error", "E")]
print("messagebox: OK")

order = []
root.after(30, lambda: order.append("b"))
root.after(0, lambda: order.append("a"))
root.after(10, lambda: 1 / 0)            # an error must not stop the loop
root.after(60, root.quit)
root.mainloop()
assert order == ["a", "b"], order
print("after/mainloop: OK")
print("HEADLESS TK TESTS PASSED")
