#!/usr/bin/env python3
"""Desktop GUI for erase.py. Start: sudo .venv/bin/python gui.py

The actual work (frozen handling, erasing, reports) is done by erase.run_all;
the GUI only collects the input and shows status and output. Window texts are German or
English (config.toml: gui_language, switchable at runtime); the output pane is the technical
log and stays English like the terminal version.
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

# All window texts. Both languages must have the same keys (checked in test_erase.py).
UI_TEXT = {
    "en": {
        "company": "Performing company", "customer": "Customer", "drives": "Drives", "output": "Output",
        "fields": {"biz_name": "Company name", "biz_address": "Company address", "biz_contact": "Company contact person",
                   "biz_phone": "Company phone", "cust_name": "Customer name", "cust_address": "Customer address",
                   "cust_contact": "Customer contact person", "cust_phone": "Customer phone"},
        "standard": "STANDARD", "std_labels": {"nist": "NIST SP 800-88", "bsi": "BSI (normal)", "bsi-high": "BSI (high)"},
        "help_btn": "Help: which standard?", "rescan": "Rescan drives", "poweroff": "Power off", "start": "Start erasing",
        "columns": ["", "Device", "Model", "Serial number", "Size", "Method", "Comment", "Status"],
        "mounted": "mounted, locked", "running": "running …",
        "results": {"SUCCESSFUL": "SUCCESSFUL", "FAILED": "FAILED", "SKIPPED (frozen, no report)":
                    "SKIPPED (frozen, no report)", "ABORTED (no report)": "ABORTED (no report)"},
        "method_warn": {"ata": "ATA SECURE ERASE", "nvme": "NVMe FORMAT / USER DATA ERASE",
                        "nwipe": "OVERWRITE WITH ZEROS + VERIFY", "random": "OVERWRITE WITH RANDOM DATA + VERIFY"},
        "select_one": "Please select at least one drive.",
        "mounted_err": "{path} has mounted partitions. Please unmount them and click '{rescan}'.",
        "ata_err": "{path} does not support ATA Security (USB adapter?).\n"
                   "The method was set to 'nwipe', please check and start again.",
        "warning": "WARNING", "destroy": "All data on these drives will be IRREVERSIBLY DESTROYED:",
        "passes": " ({n} passes)", "confirm": "Type '{word}' to confirm:", "word": "yes",
        "erase": "Erase", "cancel": "Cancel", "ok": "OK", "skip": "Skip",
        "frozen_title": "Drive frozen",
        "frozen": "Unplug and replug the SATA power cable of {serial} while the machine is running.\n\n"
                  "Then click OK. '{skip}' skips the drive.",
        "done": "Done", "reports_in": "Reports are in:",
        "still_running": "Erasure still running. Please wait until it has finished.",
        "power_q": "Power off the computer now?",
        "progress": erase.PROGRESS_EN,
        "help_title": "Which standard?",
        "help_text": "The standard decides how each drive is erased and is named on the report.",
        # rows: label, NIST, BSI normal, BSI high
        "help_table": [
            ("Use for", "Default, internationally recognised", "Data with a normal protection requirement",
             "Sensitive data: personnel, health, financial data, trade secrets"),
            ("HDD", "1 pass of zeros (Clear)", "1 pass of random data", "3 passes of random data"),
            ("SATA SSD", "ATA Secure Erase (Purge)", "1 pass of random data", "3 passes of random data"),
            ("NVMe SSD", "NVMe Format, User Data Erase (Purge)", "1 pass of random data", "3 passes of random data"),
            ("Verification", "HDD: full read-back; SSD: drive state and controller response checked",
             "Full read-back", "Full read-back of the last pass"),
            ("Time, 1 TB HDD", "~4 h", "~4 h", "~7.5 h"),
            ("Reference", "NIST SP 800-88 Rev. 1", "BSI IT-Grundschutz M 2.167, M 2.433",
             "BSI IT-Grundschutz M 2.167, M 2.433"),
        ],
        "help_note": "Random data: ISAAC-64 generator (nwipe). BSI requires random data, so firmware commands such "
                     "as ATA Secure Erase are not used in BSI mode. SSDs: overwriting cannot reach spare flash "
                     "cells; the BSI recommends encrypting SSDs from the start and physically destroying them for "
                     "very high protection requirements. BSI-TL 03420 (classified information) is not covered.",
        "cannot_start": "TabulaRasa cannot start:",
    },
    "de": {
        "company": "Ausführende Firma", "customer": "Kunde", "drives": "Laufwerke", "output": "Ausgabe",
        "fields": {"biz_name": "Firmenname", "biz_address": "Firmenadresse", "biz_contact": "Kontaktperson der Firma",
                   "biz_phone": "Telefon der Firma", "cust_name": "Kundenname", "cust_address": "Kundenadresse",
                   "cust_contact": "Kontaktperson des Kunden", "cust_phone": "Telefon des Kunden"},
        "standard": "NORM", "std_labels": {"nist": "NIST SP 800-88", "bsi": "BSI (normal)", "bsi-high": "BSI (hoch)"},
        "help_btn": "Hilfe: welche Norm?", "rescan": "Laufwerke neu einlesen", "poweroff": "Ausschalten",
        "start": "Löschen starten",
        "columns": ["", "Gerät", "Modell", "Seriennummer", "Größe", "Methode", "Bemerkung", "Status"],
        "mounted": "eingehängt, gesperrt", "running": "läuft …",
        "results": {"SUCCESSFUL": "ERFOLGREICH", "FAILED": "FEHLGESCHLAGEN", "SKIPPED (frozen, no report)":
                    "ÜBERSPRUNGEN (eingefroren, kein Report)", "ABORTED (no report)": "ABGEBROCHEN (kein Report)"},
        "method_warn": {"ata": "ATA SECURE ERASE", "nvme": "NVMe FORMAT / USER DATA ERASE",
                        "nwipe": "ÜBERSCHREIBEN MIT NULLEN + VERIFIKATION",
                        "random": "ÜBERSCHREIBEN MIT ZUFALLSDATEN + VERIFIKATION"},
        "select_one": "Bitte mindestens ein Laufwerk auswählen.",
        "mounted_err": "{path} hat eingehängte Partitionen. Bitte aushängen und '{rescan}' klicken.",
        "ata_err": "{path} unterstützt kein ATA Security (USB-Adapter?).\n"
                   "Die Methode wurde auf 'nwipe' gesetzt, bitte prüfen und erneut starten.",
        "warning": "ACHTUNG", "destroy": "Alle Daten auf diesen Laufwerken werden UNWIDERRUFLICH ZERSTÖRT:",
        "passes": " ({n} Durchgänge)", "confirm": "Zum Bestätigen '{word}' eingeben:", "word": "ja",
        "erase": "Löschen", "cancel": "Abbrechen", "ok": "OK", "skip": "Überspringen",
        "frozen_title": "Laufwerk eingefroren",
        "frozen": "SATA-Stromkabel von {serial} im laufenden Betrieb ab- und wieder anstecken.\n\n"
                  "Danach OK klicken. '{skip}' überspringt das Laufwerk.",
        "done": "Fertig", "reports_in": "Reports liegen in:",
        "still_running": "Löschvorgang läuft noch. Bitte bis zum Ende warten.",
        "power_q": "Rechner jetzt ausschalten?",
        "progress": {"writing": "schreibt", "writing_pass": "schreibt Durchgang {n}/{rounds}", "verifying": "prüft",
                     "eta": "noch {left} · fertig ca. {end}"},
        "help_title": "Welche Norm?",
        "help_text": "Die Norm bestimmt, wie jedes Laufwerk gelöscht wird, und wird im Report genannt.",
        "help_table": [
            ("Geeignet für", "Standard, international anerkannt", "Daten mit normalem Schutzbedarf",
             "Sensible Daten: Personal-, Gesundheits-, Finanzdaten, Betriebsgeheimnisse"),
            ("HDD", "1 Durchgang Nullen (Clear)", "1 Durchgang Zufallsdaten", "3 Durchgänge Zufallsdaten"),
            ("SATA-SSD", "ATA Secure Erase (Purge)", "1 Durchgang Zufallsdaten", "3 Durchgänge Zufallsdaten"),
            ("NVMe-SSD", "NVMe Format, User Data Erase (Purge)", "1 Durchgang Zufallsdaten",
             "3 Durchgänge Zufallsdaten"),
            ("Verifikation", ("HDD: vollständiges Zurücklesen; SSD: Laufwerksstatus und Rückmeldung des "
                              "Controllers geprüft"),
             "Vollständiges Zurücklesen", "Vollständiges Zurücklesen des letzten Durchgangs"),
            ("Dauer, 1-TB-HDD", "~4 h", "~4 h", "~7,5 h"),
            ("Grundlage", "NIST SP 800-88 Rev. 1", "BSI IT-Grundschutz M 2.167, M 2.433",
             "BSI IT-Grundschutz M 2.167, M 2.433"),
        ],
        "help_note": "Zufallsdaten: Generator ISAAC-64 (nwipe). Das BSI verlangt Zufallsdaten, daher werden "
                     "Firmware-Befehle wie ATA Secure Erase im BSI-Modus nicht verwendet. SSDs: Überschreiben "
                     "erreicht keine Reserve-Flashzellen; das BSI empfiehlt, SSDs von Anfang an zu verschlüsseln "
                     "und sie bei sehr hohem Schutzbedarf physisch zu vernichten. BSI-TL 03420 (Verschlusssachen) "
                     "ist nicht abgedeckt.",
        "cannot_start": "TabulaRasa kann nicht starten:",
    },
}


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
        # Everything entered lives in these variables, so build() can recreate the widgets (language switch)
        self.gui_language = tk.StringVar(value=defaults["gui_language"])
        self.standard = tk.StringVar(value=defaults["standard"])
        self.meta = {key: tk.StringVar(value=defaults.get(key, "-")) for key, _ in erase.META_FIELDS}
        self.logo = small_logo(erase.LOGO) if erase.LOGO.exists() else None
        root.title("TabulaRasa")
        root.configure(bg=BG)
        root.geometry("1280x820")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.style()
        sys.stdout = QueueWriter(self.q)
        self.build()
        self.poll()
        self.tick()

    @property
    def t(self):
        return UI_TEXT[self.gui_language.get()]

    def build(self):
        """Create all widgets in the current language; called again when the language changes."""
        t = self.t
        log_text = self.log.get("1.0", "end-1c") if hasattr(self, "log") else ""
        for child in self.root.winfo_children():
            child.destroy()

        head = ttk.Frame(self.root, padding=(24, 18, 24, 6))
        head.pack(fill="x")
        if self.logo:
            ttk.Label(head, image=self.logo).pack(side="right")
        ttk.Label(head, text="TabulaRasa", style="Title.TLabel").pack(side="left")
        langs = ttk.Frame(head)
        langs.pack(side="left", padx=24)
        self.lang_buttons = [ttk.Radiobutton(langs, text=code.upper(), value=code, variable=self.gui_language,
                                             style="Toolbutton", command=self.build) for code in UI_TEXT]
        for rb in self.lang_buttons:
            rb.pack(side="left")

        body = ttk.Frame(self.root, padding=(24, 0, 24, 18))
        body.pack(fill="both", expand=True)
        fields = ttk.Frame(body)
        fields.pack(fill="x")
        for col, (title, keys) in enumerate([(t["company"], erase.META_FIELDS[:4]),
                                             (t["customer"], erase.META_FIELDS[4:])]):
            box = ttk.Frame(fields)
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 24) if col == 0 else 0)
            fields.columnconfigure(col, weight=1)
            self.section(box, title)
            for key, _ in keys:
                ttk.Label(box, text=t["fields"][key], style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
                ttk.Entry(box, textvariable=self.meta[key]).pack(fill="x")

        ttk.Frame(body, height=18).pack()
        self.section(body, t["drives"])
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text=t["standard"], style="Muted.TLabel").pack(side="left", padx=(4, 8))
        self.std_buttons = [ttk.Radiobutton(bar, text=t["std_labels"][k], value=k, variable=self.standard,
                                            style="Toolbutton", command=self.load_disks) for k in erase.STANDARDS]
        for rb in self.std_buttons:
            rb.pack(side="left")
        ttk.Button(bar, text=t["help_btn"], command=self.help).pack(side="right")
        self.table = ttk.Frame(body)
        self.table.pack(fill="x")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=14)
        self.refresh_btn = ttk.Button(buttons, text=t["rescan"], command=self.load_disks)
        self.refresh_btn.pack(side="left")
        if os.environ.get("ERASE_KIOSK"):  # live stick: there is no desktop to power off from
            ttk.Button(buttons, text=t["poweroff"], command=self.poweroff).pack(side="left", padx=8)
        self.start_btn = ttk.Button(buttons, text=t["start"], style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right")

        self.section(body, t["output"])
        log = ttk.Frame(body)
        log.pack(fill="both", expand=True)
        self.log = tk.Text(log, height=10, font=("DejaVu Sans Mono", 9), fg=INK, bg="#fafafa", relief="flat",
                           highlightthickness=1, highlightbackground=LINE, wrap="word")
        self.log.insert("end", log_text)
        self.log.configure(state="disabled")
        scroll = ttk.Scrollbar(log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.load_disks()

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
        t, standard = self.t, self.standard.get()
        # Keep selection, comment and method across rescans, standard and language changes
        kept = {r["info"]["path"]: (r["check"].get(), r["comment"].get(), r["method"].get()) for r in self.rows}
        for child in self.table.winfo_children():
            child.destroy()
        self.rows = []
        for col, title in enumerate(t["columns"]):
            ttk.Label(self.table, text=title.upper(), style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=4)
        self.table.columnconfigure(6, weight=1, minsize=200)  # long progress texts must not squeeze it
        allowed = erase.STANDARDS[standard]["methods"]
        for r, info in enumerate(erase.list_disks(), 1):
            checked, comment_text, method = kept.get(info["path"], (False, "", None))
            if method not in allowed:
                method = erase.pick_method(info["tran"], info["rota"], standard)
            row = {"r": r, "info": info, "check": tk.BooleanVar(value=checked),
                   "comment": tk.StringVar(value=comment_text), "method": tk.StringVar(value=method)}
            check = ttk.Checkbutton(self.table, variable=row["check"])
            check.grid(row=r, column=0, padx=4, pady=3)
            for col, value in enumerate([info["path"], info["model"].strip(), info["serial"].strip(),
                                         erase.size_gb(int(info["size"]))], 1):
                ttk.Label(self.table, text=value).grid(row=r, column=col, sticky="w", padx=4)
            # Toggle buttons, not a Combobox: its drop-down list is a separate window (see dialog())
            methods = ttk.Frame(self.table)
            methods.grid(row=r, column=5, padx=4)
            radios = [ttk.Radiobutton(methods, text=m, value=m, variable=row["method"], style="Toolbutton")
                      for m in allowed]
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
                row["status"].configure(text=t["mounted"], foreground=MUTED)
            self.rows.append(row)

    # --- Start ---

    def start(self):
        t = self.t
        chosen = [row for row in self.rows if row["check"].get()]
        if not chosen:
            self.dialog("TabulaRasa", t["select_one"])
            return
        disks = []
        try:
            for row in chosen:
                info, method = row["info"], row["method"].get()
                erase.need(erase.METHODS[method][3])
                path = info["path"]
                # Something may have been mounted since the list was read
                if erase.run(["lsblk", "-n", "-o", "MOUNTPOINT", path]).stdout.strip():
                    erase.die(t["mounted_err"].format(path=path, rescan=t["rescan"]))
                # Check up front, not halfway through the run: then nwipe can be chosen right away
                if method == "ata" and not erase.security_status(path)["supported"]:
                    row["method"].set("nwipe")
                    erase.die(t["ata_err"].format(path=path))
                disks.append({"device": path, "info": info, "serial": info["serial"].strip(),
                              "method": method, "comment": row["comment"].get().strip(), "row": row})
        except erase.Abort as e:
            self.dialog("TabulaRasa", str(e))
            return

        standard = self.standard.get()
        rounds = erase.STANDARDS[standard]["rounds"]
        passes = t["passes"].format(n=rounds) if rounds > 1 else ""
        lines = "\n".join(f"• {d['device']}  {d['info']['model'].strip()} / {d['serial']}  →  "
                          f"{t['method_warn'][d['method']]}{passes}"
                          f"\n   {erase.standard_text(d['method'], standard, self.gui_language.get())}" for d in disks)
        button, answer = self.dialog(t["warning"], f"{t['destroy']}\n\n{lines}\n\n" + t["confirm"].format(word=t["word"]),
                                     (t["erase"], t["cancel"]), entry=True)
        if button != t["erase"] or answer.strip().lower() != t["word"]:
            return

        self.running = True
        for w in (self.start_btn, self.refresh_btn, *self.std_buttons, *self.lang_buttons):
            w.state(["disabled"])
        for row in self.rows:
            for w in row["widgets"]:
                w.state(["disabled"])
        meta = {k: v.get().strip() or "-" for k, v in self.meta.items()}
        meta["place"], meta["language"], meta["standard"] = self.place, self.language, standard
        threading.Thread(target=self.work, args=(disks, meta), daemon=True).start()

    def work(self, disks, meta):
        try:
            erase.run_all(disks, meta, prompt=self.prompt, quiet=True,
                          on_status=lambda d: self.q.put(lambda: self.show_status(d)))
        except Exception:  # noqa: BLE001 - show unexpected errors instead of dying silently
            print(traceback.format_exc())
        self.q.put(lambda: self.done(disks))

    def prompt(self, serial):
        """Replug question from the worker thread: open the dialog in the GUI thread and wait for the answer."""
        answer, ready = [], threading.Event()

        def ask():
            t = self.t
            answer.append(self.dialog(t["frozen_title"], t["frozen"].format(serial=serial, skip=t["skip"]),
                                      (t["ok"], t["skip"])) == t["ok"])
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
        row["status"].configure(text=self.t["results"].get(result, result),
                                foreground=OK if result == "SUCCESSFUL" else FAIL)

    def done(self, disks):
        t = self.t
        self.running = False
        for w in (self.start_btn, self.refresh_btn, *self.std_buttons, *self.lang_buttons):
            w.state(["!disabled"])
        summary = "\n".join(f"{d['device']} ({d['serial']}): {t['results'].get(d['result'], d['result'])}"
                            for d in disks)
        self.dialog(t["done"], f"{summary}\n\n{t['reports_in']}\n{erase.REPORTS}")
        self.load_disks()

    # --- Helpers ---

    def help(self):
        t = self.t

        def table(card):
            grid = ttk.Frame(card)
            grid.pack(anchor="w", pady=(12, 0))
            for col, title in enumerate([""] + [t["std_labels"][k] for k in erase.STANDARDS]):
                ttk.Label(grid, text=title, style="Section.TLabel").grid(row=0, column=col, sticky="w", padx=8)
            for r, cells in enumerate(t["help_table"], 1):
                for col, text in enumerate(cells):
                    ttk.Label(grid, text=text, wraplength=230, justify="left",
                              style="Muted.TLabel" if col == 0 else "TLabel").grid(
                        row=r, column=col, sticky="nw", padx=8, pady=(6, 0))
                tk.Frame(grid, height=1, bg=LINE).grid(row=r, column=0, columnspan=4, sticky="new")
            ttk.Label(card, text=t["help_note"], wraplength=900, justify="left", style="Muted.TLabel").pack(
                anchor="w", pady=(14, 0))
        self.dialog(t["help_title"], t["help_text"], (t["ok"],), body=table)

    def dialog(self, title, text, buttons=None, entry=False, body=None):
        """Modal panel inside the main window. The Cage kiosk compositor does not show separate Tk
        windows (messagebox, Combobox list), so everything stays in this one window.
        Returns the clicked button, with entry=True (button, entered text). Enter = first, Escape = last.
        body(card) can add further widgets below the text."""
        buttons = buttons or (self.t["ok"],)
        cover = tk.Frame(self.root, bg="#e9e9e9")  # hides the window behind the panel (Tk has no transparency)
        cover.place(relx=0, rely=0, relwidth=1, relheight=1)
        card = ttk.Frame(cover, padding=24, style="Card.TFrame")
        card.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(card, text=title, style="Section.TLabel").pack(anchor="w")
        tk.Frame(card, height=2, bg=ACCENT).pack(fill="x", pady=(2, 10))
        ttk.Label(card, text=text, wraplength=600, justify="left").pack(anchor="w")
        if body:
            body(card)
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
            frac, text = erase.progress_of(d, self.t["progress"])
            bar = d["row"]["bar"]
            if frac is not None:
                if str(bar.cget("mode")) != "determinate":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=100)
                bar.configure(value=frac * 100)
            d["row"]["eta"].configure(text=text or self.t["running"])
        self.root.after(1000, self.tick)

    def poweroff(self):
        t = self.t
        if self.running:
            self.dialog("TabulaRasa", t["still_running"])
        elif self.dialog(t["poweroff"], t["power_q"], (t["poweroff"], t["cancel"])) == t["poweroff"]:
            os.sync()
            subprocess.run(["systemctl", "poweroff"], check=False)

    def close(self):
        if self.running:  # aborting in the middle of an ATA Secure Erase can leave the drive locked
            self.dialog("TabulaRasa", self.t["still_running"])
            return
        self.root.destroy()


def main():
    os.chdir(erase.HERE)
    try:
        root = tk.Tk()
    except tk.TclError as e:
        sys.exit(f"Cannot open a window ({e}).\nOn Wayland, run once as your normal user: "
                 "xhost +SI:localuser:root")
    defaults = {}
    try:
        defaults = erase.load_defaults()
        erase.preflight()
    except erase.Abort as e:
        # Shown in the main window itself: in the kiosk a separate error dialog would stay invisible
        root.title("TabulaRasa")
        text = UI_TEXT[defaults.get("gui_language", "en")]["cannot_start"]
        tk.Label(root, text=f"{text}\n\n{e}", justify="left", fg=FAIL, bg=BG,
                 font=("DejaVu Sans", 12), padx=40, pady=40).pack(fill="both", expand=True)
        root.mainloop()
        sys.exit(1)
    App(root, defaults)
    root.mainloop()


if __name__ == "__main__":
    main()
