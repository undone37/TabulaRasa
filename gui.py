#!/usr/bin/env python3
"""Desktop GUI for erase.py. Start: sudo .venv/bin/python gui.py

The actual work (frozen handling, erasing, reports) is done by erase.run_all;
the GUI only collects the input and shows status and output.
"""

import base64
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import ttk

import erase

INK, MUTED, LINE, ACCENT, OK, FAIL, BG = "#333333", "#6b6b6b", "#d4d4d4", "#509ea3", "#3f8f4c", "#c0392b", "#ffffff"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class QueueWriter:
    """Replaces sys.stdout: print() from any thread ends up in the output pane."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        self.q.put(ANSI.sub("", text))

    def flush(self):
        pass


def small_logo(path, width=240):
    """Smooth downscale via ImageMagick, otherwise pixelated via subsample (Tk itself cannot interpolate)."""
    tool = shutil.which("magick") or shutil.which("convert")
    if tool:
        out = subprocess.run([tool, str(path), "-filter", "Lanczos", "-resize", f"{width}x", "png:-"],
                             capture_output=True, check=False)
        if out.returncode == 0:
            return tk.PhotoImage(data=base64.b64encode(out.stdout))
    img = tk.PhotoImage(file=str(path))
    return img.subsample(max(1, img.width() // width))


class App:
    def __init__(self, root, defaults):
        self.place, self.language = defaults.get("place", ""), defaults["language"]
        self.root, self.q, self.rows, self.running, self.active = root, queue.Queue(), [], False, []
        root.title("TabulaRasa")
        root.configure(bg=BG)
        root.geometry("1100x800")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.style()

        head = ttk.Frame(root, padding=(24, 18, 24, 6))
        head.pack(fill="x")
        if erase.LOGO.exists():
            self.logo = small_logo(erase.LOGO)
            ttk.Label(head, image=self.logo).pack(side="right")
        ttk.Label(head, text="TabulaRasa", style="Title.TLabel").pack(side="left")

        body = ttk.Frame(root, padding=(24, 0, 24, 18))
        body.pack(fill="both", expand=True)

        self.meta = {}
        fields = ttk.Frame(body)
        fields.pack(fill="x")
        for col, (title, keys) in enumerate([("Performing company", erase.META_FIELDS[:4]),
                                             ("Customer", erase.META_FIELDS[4:])]):
            box = ttk.Frame(fields)
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 24) if col == 0 else 0)
            fields.columnconfigure(col, weight=1)
            self.section(box, title)
            for key, label in keys:
                ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
                self.meta[key] = tk.StringVar(value=defaults.get(key, "-"))
                ttk.Entry(box, textvariable=self.meta[key]).pack(fill="x")

        ttk.Frame(body, height=18).pack()
        self.section(body, "Drives")
        self.table = ttk.Frame(body)
        self.table.pack(fill="x")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=14)
        self.refresh_btn = ttk.Button(buttons, text="Rescan drives", command=self.load_disks)
        self.refresh_btn.pack(side="left")
        if os.environ.get("ERASE_KIOSK"):  # live stick: there is no desktop to power off from
            self.off_btn = ttk.Button(buttons, text="Power off", command=self.poweroff)
            self.off_btn.pack(side="left", padx=8)
        self.start_btn = ttk.Button(buttons, text="Start erasing", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right")

        self.section(body, "Output")
        log = ttk.Frame(body)
        log.pack(fill="both", expand=True)
        self.log = tk.Text(log, height=10, font=("DejaVu Sans Mono", 9), fg=INK, bg="#fafafa", relief="flat",
                           highlightthickness=1, highlightbackground=LINE, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        sys.stdout = QueueWriter(self.q)
        self.load_disks()
        self.poll()
        self.tick()

    def style(self):
        families = set(tkfont.families())
        text = "Quicksand Medium" if "Quicksand Medium" in families else "DejaVu Sans"
        head = "Comfortaa" if "Comfortaa" in families else text
        tkfont.nametofont("TkDefaultFont").configure(family=text, size=10)
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=INK, font=(text, 10), bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, focuscolor=ACCENT)
        s.configure("Title.TLabel", font=(head, 20, "bold"))
        s.configure("Section.TLabel", font=(head, 10, "bold"), foreground=ACCENT)
        s.configure("Muted.TLabel", foreground=MUTED, font=(text, 8))
        s.configure("TEntry", fieldbackground=BG, padding=5)
        s.configure("Toolbutton", padding=(8, 3), background="#f2f2f2", relief="flat")
        s.map("Toolbutton", background=[("selected", ACCENT), ("active", "#e6e6e6")],
              foreground=[("selected", "#ffffff"), ("disabled", MUTED)])
        s.configure("Card.TFrame", background=BG, borderwidth=1, relief="solid")
        s.configure("TButton", padding=(14, 6), background="#f2f2f2", relief="flat")
        s.map("TButton", background=[("active", "#e6e6e6")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=(text, 11, "bold"),
                    padding=(22, 9), bordercolor=ACCENT)
        s.map("Accent.TButton", background=[("disabled", LINE), ("active", "#3f8a8f")])
        s.configure("TCheckbutton", background=BG)
        s.configure("TProgressbar", background=ACCENT, troughcolor="#eeeeee", thickness=6)

    def section(self, parent, title):
        ttk.Label(parent, text=title, style="Section.TLabel").pack(anchor="w")
        tk.Frame(parent, height=2, bg=ACCENT).pack(fill="x", pady=(2, 6))

    # --- Drive list ---

    def load_disks(self):
        for child in self.table.winfo_children():
            child.destroy()
        self.rows = []
        for col, title in enumerate(["", "Device", "Model", "Serial number", "Size", "Method", "Comment", "Status"]):
            ttk.Label(self.table, text=title.upper(), style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=4)
        self.table.columnconfigure(6, weight=1)
        for r, info in enumerate(erase.list_disks(), 1):
            row = {"r": r, "info": info, "check": tk.BooleanVar(), "comment": tk.StringVar(),
                   "method": tk.StringVar(value=erase.pick_method(info["tran"], info["rota"]))}
            check = ttk.Checkbutton(self.table, variable=row["check"])
            check.grid(row=r, column=0, padx=4, pady=3)
            for col, value in enumerate([info["path"], info["model"].strip(), info["serial"].strip(),
                                         erase.size_gb(int(info["size"]))], 1):
                ttk.Label(self.table, text=value).grid(row=r, column=col, sticky="w", padx=4)
            # Toggle buttons, not a Combobox: its drop-down list is a separate window (see dialog())
            methods = ttk.Frame(self.table)
            methods.grid(row=r, column=5, padx=4)
            radios = [ttk.Radiobutton(methods, text=m, value=m, variable=row["method"], style="Toolbutton")
                      for m in erase.METHODS]
            for rb in radios:
                rb.pack(side="left")
            comment = ttk.Entry(self.table, textvariable=row["comment"])
            comment.grid(row=r, column=6, sticky="ew", padx=4)
            row["status"] = ttk.Label(self.table, text="", width=26)
            row["status"].grid(row=r, column=7, sticky="w", padx=4)
            # While running: bar + text (percent, speed, time left) instead of the status text
            row["progress"] = ttk.Frame(self.table)
            row["bar"] = ttk.Progressbar(row["progress"], mode="indeterminate", length=230)
            row["bar"].pack(anchor="w")
            row["eta"] = ttk.Label(row["progress"], text="", style="Muted.TLabel")
            row["eta"].pack(anchor="w")
            row["widgets"] = [check, *radios, comment]
            if info["mounted"]:  # e.g. the live system itself: not selectable at all
                for w in row["widgets"]:
                    w.state(["disabled"])
                row["status"].configure(text="mounted, locked", foreground=MUTED)
            self.rows.append(row)

    # --- Start ---

    def start(self):
        chosen = [row for row in self.rows if row["check"].get()]
        if not chosen:
            self.dialog("TabulaRasa", "Please select at least one drive.")
            return
        disks = []
        try:
            for row in chosen:
                info, method = row["info"], row["method"].get()
                erase.need(erase.METHODS[method][3])
                path = info["path"]
                # Something may have been mounted since the list was read
                if erase.run(["lsblk", "-n", "-o", "MOUNTPOINT", path]).stdout.strip():
                    erase.die(f"{path} has mounted partitions. Please unmount them and click 'Rescan drives'.")
                # Check up front, not halfway through the run: then nwipe can be chosen right away
                if method == "ata" and not erase.security_status(path)["supported"]:
                    row["method"].set("nwipe")
                    erase.die(f"{path} does not support ATA Security (USB adapter?).\n"
                              "The method was set to 'nwipe', please check and start again.")
                disks.append({"device": path, "info": info, "serial": info["serial"].strip(),
                              "method": method, "comment": row["comment"].get().strip(), "row": row})
        except erase.Abort as e:
            self.dialog("TabulaRasa", str(e))
            return

        lines = "\n".join(f"• {d['device']}  {d['info']['model'].strip()} / {d['serial']}  →  "
                          f"{erase.METHODS[d['method']][2]}" for d in disks)
        button, answer = self.dialog(
            "WARNING", f"All data on these drives will be IRREVERSIBLY DESTROYED:\n\n{lines}\n\n"
                       "Type 'yes' to confirm:", ("Erase", "Cancel"), entry=True)
        if button != "Erase" or answer.strip() != "yes":
            return

        self.running = True
        self.start_btn.state(["disabled"])
        self.refresh_btn.state(["disabled"])
        for row in self.rows:
            for w in row["widgets"]:
                w.state(["disabled"])
        meta = {k: v.get().strip() or "-" for k, v in self.meta.items()}
        meta["place"], meta["language"] = self.place, self.language
        threading.Thread(target=self.work, args=(disks, meta), daemon=True).start()

    def work(self, disks, meta):
        try:
            erase.run_all(disks, meta, prompt=self.prompt, quiet=True,
                          on_status=lambda d: self.q.put(lambda: self.show_status(d)))
        except Exception:  # noqa: BLE001 - show unexpected errors instead of dying silently
            print(traceback.format_exc())
        self.q.put(lambda: self.done(disks))

    def prompt(self, msg):
        """Question from the worker thread: open the dialog in the GUI thread and wait for the answer."""
        answer, ready = [], threading.Event()

        def ask():
            answer.append(self.dialog(
                "Drive frozen", f"{msg}.\n\nThen click OK. 'Skip' skips the drive.", ("OK", "Skip")) == "OK")
            ready.set()
        self.q.put(ask)
        ready.wait()
        return answer[0]

    def show_status(self, d):
        row, result = d["row"], d["result"]
        if result == "RUNNING":
            row["status"].grid_remove()
            row["progress"].grid(row=row["r"], column=7, sticky="w", padx=4)
            row["bar"].start(12)
            self.active.append(d)
            return
        if d in self.active:
            self.active.remove(d)
        row["bar"].stop()
        row["progress"].grid_remove()
        row["status"].grid()
        row["status"].configure(text=result, foreground=OK if result == "SUCCESSFUL" else FAIL)

    def done(self, disks):
        self.running = False
        self.start_btn.state(["!disabled"])
        self.refresh_btn.state(["!disabled"])
        summary = "\n".join(f"{d['device']} ({d['serial']}): {d['result']}" for d in disks)
        self.dialog("Done", f"{summary}\n\nReports are in:\n{erase.REPORTS}")
        self.load_disks()

    # --- Helpers ---

    def dialog(self, title, text, buttons=("OK",), entry=False):
        """Modal panel inside the main window. The Cage kiosk compositor does not show separate Tk
        windows (messagebox, Combobox list), so everything stays in this one window.
        Returns the clicked button, with entry=True (button, entered text). Enter = first, Escape = last."""
        cover = tk.Frame(self.root, bg="#e9e9e9")  # hides the window behind the panel (Tk has no transparency)
        cover.place(relx=0, rely=0, relwidth=1, relheight=1)
        card = ttk.Frame(cover, padding=24, style="Card.TFrame")
        card.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(card, text=title, style="Section.TLabel").pack(anchor="w")
        tk.Frame(card, height=2, bg=ACCENT).pack(fill="x", pady=(2, 10))
        ttk.Label(card, text=text, wraplength=600, justify="left").pack(anchor="w")
        value, choice = tk.StringVar(), tk.StringVar()
        if entry:
            field = ttk.Entry(card, textvariable=value)
            field.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(card)
        row.pack(fill="x", pady=(18, 0))
        for label in reversed(buttons):  # first button is the primary one, on the right
            ttk.Button(row, text=label, style="Accent.TButton" if label == buttons[0] else "TButton",
                       command=lambda b=label: choice.set(b)).pack(side="right", padx=(8, 0))
        self.root.bind("<Return>", lambda e: choice.set(buttons[0]))
        self.root.bind("<Escape>", lambda e: choice.set(buttons[-1]))
        (field if entry else card).focus_set()
        cover.grab_set()
        self.root.wait_variable(choice)
        cover.grab_release()
        self.root.unbind("<Return>")
        self.root.unbind("<Escape>")
        cover.destroy()
        return (choice.get(), value.get()) if entry else choice.get()

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if callable(item):
                    item()
                else:
                    self.log.configure(state="normal")
                    self.log.insert("end", item)
                    self.log.see("end")
                    self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def tick(self):
        """Recalculate the progress of running drives every second."""
        for d in self.active:
            frac, text = erase.progress_of(d)
            bar = d["row"]["bar"]
            if frac is not None:
                if str(bar.cget("mode")) != "determinate":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=100)
                bar.configure(value=frac * 100)
            d["row"]["eta"].configure(text=text or "running …")
        self.root.after(1000, self.tick)

    def poweroff(self):
        if self.running:
            self.dialog("TabulaRasa", "Erasure still running. Please wait until it has finished.")
        elif self.dialog("Power off", "Power off the computer now?", ("Power off", "Cancel")) == "Power off":
            os.sync()
            subprocess.run(["systemctl", "poweroff"], check=False)

    def close(self):
        if self.running:  # aborting in the middle of an ATA Secure Erase can leave the drive locked
            self.dialog("TabulaRasa", "Erasure still running. Please wait until it has finished.")
            return
        self.root.destroy()


def main():
    os.chdir(erase.HERE)
    try:
        root = tk.Tk()
    except tk.TclError as e:
        sys.exit(f"Cannot open a window ({e}).\nOn Wayland, run once as your normal user: "
                 "xhost +SI:localuser:root")
    try:
        erase.preflight()
        defaults = erase.load_defaults()
    except erase.Abort as e:
        # Shown in the main window itself: in the kiosk a separate error dialog would stay invisible
        root.title("TabulaRasa")
        tk.Label(root, text=f"TabulaRasa cannot start:\n\n{e}", justify="left", fg=FAIL, bg=BG,
                 font=("DejaVu Sans", 12), padx=40, pady=40).pack(fill="both", expand=True)
        root.mainloop()
        sys.exit(1)
    App(root, defaults)
    root.mainloop()


if __name__ == "__main__":
    main()
