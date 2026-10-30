#!/usr/bin/env python3
"""Desktop-Oberfläche für erase.py. Start: sudo .venv/bin/python gui.py

Die eigentliche Arbeit (Frozen-Behandlung, Löschen, Reports) macht erase.run_all,
die GUI sammelt nur die Eingaben ein und zeigt Status und Ausgabe an.
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
from tkinter import messagebox, simpledialog, ttk

import erase

INK, MUTED, LINE, ACCENT, OK, FAIL, BG = "#333333", "#6b6b6b", "#d4d4d4", "#509ea3", "#3f8f4c", "#c0392b", "#ffffff"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class QueueWriter:
    """Ersetzt sys.stdout: print() aus allen Threads landet im Ausgabefenster."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        self.q.put(ANSI.sub("", text))

    def flush(self):
        pass


def small_logo(path, width=240):
    """Glatt verkleinert per ImageMagick, sonst pixelig per subsample (Tk selbst kann nicht interpolieren)."""
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
        self.place = defaults.get("place", "")
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
        for col, (title, keys) in enumerate([("Ausführende Firma", erase.META_FIELDS[:4]),
                                             ("Kunde", erase.META_FIELDS[4:])]):
            box = ttk.Frame(fields)
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 24) if col == 0 else 0)
            fields.columnconfigure(col, weight=1)
            self.section(box, title)
            for key, label in keys:
                ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
                self.meta[key] = tk.StringVar(value=defaults.get(key, "-"))
                ttk.Entry(box, textvariable=self.meta[key]).pack(fill="x")

        ttk.Frame(body, height=18).pack()
        self.section(body, "Laufwerke")
        self.table = ttk.Frame(body)
        self.table.pack(fill="x")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=14)
        self.refresh_btn = ttk.Button(buttons, text="Laufwerke neu einlesen", command=self.load_disks)
        self.refresh_btn.pack(side="left")
        if os.environ.get("ERASE_KIOSK"):  # Live-Stick: kein Desktop, über den man sonst ausschalten könnte
            self.off_btn = ttk.Button(buttons, text="Ausschalten", command=self.poweroff)
            self.off_btn.pack(side="left", padx=8)
        self.start_btn = ttk.Button(buttons, text="Löschen starten", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right")

        self.section(body, "Ausgabe")
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
        s.configure("TCombobox", fieldbackground=BG, padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", BG)])
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

    # --- Laufwerksliste ---

    def load_disks(self):
        for child in self.table.winfo_children():
            child.destroy()
        self.rows = []
        for col, title in enumerate(["", "Gerät", "Modell", "Seriennummer", "Größe", "Methode", "Bemerkung", "Status"]):
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
            method = ttk.Combobox(self.table, textvariable=row["method"], values=list(erase.METHODS),
                                  state="readonly", width=7)
            method.grid(row=r, column=5, padx=4)
            comment = ttk.Entry(self.table, textvariable=row["comment"])
            comment.grid(row=r, column=6, sticky="ew", padx=4)
            row["status"] = ttk.Label(self.table, text="", width=26)
            row["status"].grid(row=r, column=7, sticky="w", padx=4)
            # Laufend: Balken + Text (Prozent, Tempo, Restzeit) statt Status-Text
            row["progress"] = ttk.Frame(self.table)
            row["bar"] = ttk.Progressbar(row["progress"], mode="indeterminate", length=230)
            row["bar"].pack(anchor="w")
            row["eta"] = ttk.Label(row["progress"], text="", style="Muted.TLabel")
            row["eta"].pack(anchor="w")
            row["widgets"] = [check, method, comment]
            if info["mounted"]:  # z.B. das Live-System selbst: gar nicht erst auswählbar
                for w in row["widgets"]:
                    w.state(["disabled"])
                row["status"].configure(text="eingehängt, gesperrt", foreground=MUTED)
            self.rows.append(row)

    # --- Start ---

    def start(self):
        chosen = [row for row in self.rows if row["check"].get()]
        if not chosen:
            messagebox.showinfo("TabulaRasa", "Bitte mindestens ein Laufwerk auswählen.", parent=self.root)
            return
        disks = []
        try:
            for row in chosen:
                info, method = row["info"], row["method"].get()
                erase.need(erase.METHODS[method][3])
                path = info["path"]
                # Seit dem Einlesen könnte etwas eingehängt worden sein
                if erase.run(["lsblk", "-n", "-o", "MOUNTPOINT", path]).stdout.strip():
                    erase.die(f"{path} hat eingehängte Partitionen. Bitte aushängen und 'Aktualisieren'.")
                # Vorab prüfen, nicht erst mitten im Lauf: dann kann direkt nwipe gewählt werden
                if method == "ata" and not erase.security_status(path)["supported"]:
                    row["method"].set("nwipe")
                    erase.die(f"{path} unterstützt kein ATA Security (USB-Adapter?).\n"
                              "Methode wurde auf 'nwipe' gesetzt, bitte prüfen und erneut starten.")
                disks.append({"device": path, "info": info, "serial": info["serial"].strip(),
                              "method": method, "comment": row["comment"].get().strip(), "row": row})
        except erase.Abbruch as e:
            messagebox.showerror("TabulaRasa", str(e), parent=self.root)
            return

        lines = "\n".join(f"• {d['device']}  {d['info']['model'].strip()} / {d['serial']}  →  "
                          f"{erase.METHODS[d['method']][2]}" for d in disks)
        answer = simpledialog.askstring(
            "ACHTUNG", f"Alle Daten auf diesen Laufwerken werden UNWIDERRUFLICH ZERSTÖRT:\n\n{lines}\n\n"
                       "Zum Bestätigen 'ja' eingeben:", parent=self.root)
        if (answer or "").strip() != "ja":
            return

        self.running = True
        self.start_btn.state(["disabled"])
        self.refresh_btn.state(["disabled"])
        for row in self.rows:
            for w in row["widgets"]:
                w.state(["disabled"])
        meta = {k: v.get().strip() or "-" for k, v in self.meta.items()}
        meta["place"] = self.place
        threading.Thread(target=self.work, args=(disks, meta), daemon=True).start()

    def work(self, disks, meta):
        try:
            erase.run_all(disks, meta, prompt=self.prompt, quiet=True,
                          on_status=lambda d: self.q.put(lambda: self.show_status(d)))
        except Exception:  # noqa: BLE001 - unerwartete Fehler sichtbar machen statt still zu sterben
            print(traceback.format_exc())
        self.q.put(lambda: self.done(disks))

    def prompt(self, msg):
        """Rückfrage aus dem Arbeits-Thread: Dialog im GUI-Thread öffnen und auf Antwort warten."""
        answer, ready = [], threading.Event()

        def ask():
            answer.append(messagebox.askokcancel(
                "Laufwerk eingefroren", f"{msg}.\n\nDanach OK klicken, 'Abbrechen' überspringt das Laufwerk.",
                parent=self.root))
            ready.set()
        self.q.put(ask)
        ready.wait()
        return answer[0]

    def show_status(self, d):
        row, result = d["row"], d["result"]
        if result == "LÄUFT":
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
        row["status"].configure(text=result, foreground=OK if result == "ERFOLGREICH" else FAIL)

    def done(self, disks):
        self.running = False
        self.start_btn.state(["!disabled"])
        self.refresh_btn.state(["!disabled"])
        summary = "\n".join(f"{d['device']} ({d['serial']}): {d['result']}" for d in disks)
        messagebox.showinfo("Fertig", f"{summary}\n\nReports liegen in:\n{erase.REPORTS}", parent=self.root)
        self.load_disks()

    # --- Hilfen ---

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
        """Fortschritt der laufenden Laufwerke jede Sekunde neu berechnen."""
        for d in self.active:
            frac, text = erase.progress_of(d)
            bar = d["row"]["bar"]
            if frac is not None:
                if str(bar.cget("mode")) != "determinate":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=100)
                bar.configure(value=frac * 100)
            d["row"]["eta"].configure(text=text or "läuft …")
        self.root.after(1000, self.tick)

    def poweroff(self):
        if self.running:
            messagebox.showwarning("TabulaRasa", "Löschvorgang läuft noch. Bitte bis zum Ende warten.",
                                   parent=self.root)
        elif messagebox.askokcancel("Ausschalten", "Rechner jetzt ausschalten?", parent=self.root):
            os.sync()
            subprocess.run(["systemctl", "poweroff"], check=False)

    def close(self):
        if self.running:  # Abbruch mitten im ATA Secure Erase kann das Laufwerk gesperrt zurücklassen
            messagebox.showwarning("TabulaRasa", "Löschvorgang läuft noch. Bitte bis zum Ende warten.",
                                   parent=self.root)
            return
        self.root.destroy()


def main():
    os.chdir(erase.HERE)
    try:
        root = tk.Tk()
    except tk.TclError as e:
        sys.exit(f"Kein Fenster möglich ({e}).\nUnter Wayland einmalig als normaler Benutzer: "
                 "xhost +SI:localuser:root")
    try:
        erase.preflight()
        defaults = erase.load_defaults()
    except erase.Abbruch as e:
        root.withdraw()
        messagebox.showerror("TabulaRasa", str(e))
        sys.exit(1)
    App(root, defaults)
    root.mainloop()


if __name__ == "__main__":
    main()
