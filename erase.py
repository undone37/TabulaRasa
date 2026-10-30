#!/usr/bin/env python3
"""Datenlöschung für HDD, SATA-SSD und NVMe-SSD mit einheitlichem PDF-Report.

Methoden (Vorschlag anhand des Laufwerkstyps, kann überschrieben werden):
  ata   = ATA Secure Erase (SATA-SSD)          -> NIST 800-88 Purge
  nvme  = NVMe Format, User Data Erase (NVMe)  -> NIST 800-88 Purge
  nwipe = 1 Durchgang Nullen + Verify (HDD)    -> NIST 800-88 Clear

!! AUSFÜHRUNG NUR ALS ROOT UND VON EINEM LIVE-SYSTEM AUS !!
"""

import base64
import contextlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from string import Template

PASSWORD = "EraseMyDisk"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "report.html"
# Datenordner mit allem Firmenspezifischen: config.toml, logo.png, unterschrift.png, reports/.
# Auf dem Live-Stick die Reports-Partition (ERASE_HOME), sonst der Skriptordner.
DATA = Path(os.environ.get("ERASE_HOME", HERE))
CONFIG = DATA / "config.toml"
SIGNATURE = DATA / "unterschrift.png"
LOGO = DATA / "logo.png"
REPORTS = DATA / "reports"
HEXDUMP_BYTES = 1024000

ROT, GRUEN, GELB, NC = "\033[0;31m", "\033[0;32m", "\033[1;33m", "\033[0m"

# Deutsche Monatsnamen fest verdrahtet: unabhängig von der installierten Locale
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]

# (Schlüssel, Beschriftung) – gemeinsam für Terminal und GUI, Vorbelegung aus config.toml
META_FIELDS = [
    ("biz_name", "Name der ausführenden Firma"),
    ("biz_address", "Adresse der ausführenden Firma"),
    ("biz_contact", "Kontaktperson der Firma"),
    ("biz_phone", "Telefonnummer der Firma"),
    ("cust_name", "Kundenname"),
    ("cust_address", "Kundenadresse"),
    ("cust_contact", "Kontaktperson des Kunden"),
    ("cust_phone", "Telefonnummer des Kunden"),
]
# Abschnitt [firma] in config.toml -> Schlüssel im Report
CONFIG_KEYS = {"name": "biz_name", "adresse": "biz_address", "kontakt": "biz_contact",
               "telefon": "biz_phone", "ort": "place"}

METHODS = {
    "ata": ("SSD", "NIST 800-88 Purge", "ATA SECURE ERASE", "hdparm"),
    "nvme": ("NVMe-SSD", "NIST 800-88 Purge", "NVMe FORMAT / USER DATA ERASE", "nvme"),
    "nwipe": ("HDD", "NIST 800-88 Clear", "ÜBERSCHREIBEN MIT NULLEN + VERIFY", "nwipe"),
}


# --- Reine Hilfsfunktionen (getestet in test_erase.py) ---

def de_date(dt):
    return f"{dt.day:02d}. {MONATE[dt.month - 1]} {dt.year}"


def size_gb(size_bytes):
    return f"{size_bytes / 1000**3:.2f} GB"


def hms(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def is_true(value):
    """lsblk liefert ROTA je nach Version als bool, "1"/"0" oder "true"/"false"."""
    return str(value).strip().lower() in ("1", "true")


def pick_method(tran, rota):
    if (tran or "").lower() == "nvme":
        return "nvme"
    return "nwipe" if is_true(rota) else "ata"


def parse_security(hdparm_i):
    """Wertet den 'Security:'-Block von 'hdparm -I' aus.

    Positive Zeilen sind nur eingerückt ("\\t\\tenabled"), negative haben "not"
    davor ("\\tnot\\tenabled") – daher Abgleich am Zeilenanfang.
    """
    m = re.search(r"^Security:\s*\n(.*?)(?:\n[ \t]*\n|\Z)", hdparm_i, re.DOTALL | re.MULTILINE)
    block = m.group(1) if m else ""

    def flag(word):
        return re.search(rf"^\s+{re.escape(word)}\s*$", block, re.MULTILINE) is not None

    return {
        "supported": flag("supported"),
        "enabled": flag("enabled"),
        "frozen": flag("frozen"),
        "enhanced": flag("supported: enhanced erase"),
    }


def png_img(data, cls=""):
    return f'<img class="{cls}" src="data:image/png;base64,{base64.b64encode(data).decode()}">' if data else ""


def find_by_serial(lsblk_json, serial):
    """Gerätepfad zur Seriennummer, nach Hot-Plug ändert sich der Name (sda -> sdb)."""
    paths = [d["path"] for d in json.loads(lsblk_json)["blockdevices"]
             if d.get("type") == "disk" and (d.get("serial") or "").strip() == serial]
    return paths[0] if serial and len(paths) == 1 else None


def parse_config(text):
    """Vorbelegung aus config.toml. utf-8-sig beim Lesen: Windows-Editor schreibt gern ein BOM davor."""
    firma = tomllib.loads(text).get("firma", {})
    return {CONFIG_KEYS[k]: str(v) for k, v in firma.items() if k in CONFIG_KEYS}


def erase_minutes(hdparm_i):
    """Herstellerangabe aus 'hdparm -I', z.B. '2min for SECURITY ERASE UNIT' (passend zu enhanced/normal)."""
    enhanced = "ENHANCED " if parse_security(hdparm_i)["enhanced"] else ""
    m = re.search(rf"(\d+)min for {enhanced}SECURITY ERASE UNIT", hdparm_i)
    return int(m.group(1)) if m else None


def progress(method, size, elapsed, io_delta=None, ata_minutes=None, now=None):
    """(Anteil 0..1 oder None, Text) für die Fortschrittsanzeige.

    nwipe: geschriebene + gelesene Bytes (Durchgang + Verify) gegen 2x Laufwerksgröße.
    ata:   Firmware löscht intern, nur Zeit gegen Herstellerangabe, max. 99 % bis fertig.
    """
    def eta(seconds):
        now_ = now or datetime.now()
        end = now_ + timedelta(seconds=seconds)
        fmt = "%H:%M" if end.date() == now_.date() else "%d.%m. %H:%M"
        return f"noch {hms(int(seconds))} · fertig ca. {end:{fmt}}"

    if method == "nwipe" and io_delta and elapsed > 0:
        read, written = io_delta
        done = min(written, size) + min(read, size)
        frac = done / (2 * size)
        phase = "prüft" if written >= size else "schreibt"
        rate = done / elapsed
        text = f"{frac:.0%} {phase} · {rate / 1e6:.0f} MB/s"
        return frac, f"{text} · {eta((2 * size - done) / rate)}" if rate else text
    if method == "ata" and ata_minutes:
        total = ata_minutes * 60
        frac = min(elapsed / total, 0.99)
        return frac, f"ca. {frac:.0%} · {eta(max(total - elapsed, 0))}"
    return None, ""


def parse_disks(lsblk_json):
    """Ganze Laufwerke aus 'lsblk -J --tree -b -o PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,MOUNTPOINT'."""
    def mounted(dev):
        return bool(dev.get("mountpoint")) or any(mounted(c) for c in dev.get("children", []))
    return [{**{k: ("" if v is None else v) for k, v in d.items() if k != "children"}, "mounted": mounted(d)}
            for d in json.loads(lsblk_json)["blockdevices"] if d.get("type") == "disk"]


def build_html(meta, appendices, signature_png=None, logo_png=None):
    """meta: Felder für report.html, appendices: Liste (Überschrift, Text)."""
    values = {k: html.escape(str(v)) for k, v in meta.items()}
    comment = meta.get("comment")
    values["comment_block"] = (
        '<table><tr><th class="section">Bemerkung</th></tr>'
        f"<tr><td>{html.escape(comment)}</td></tr></table>" if comment else "")
    values["place_date"] = html.escape(f"{meta['place']}, den {meta['date']}" if meta.get("place") else meta["date"])
    values["status_class"] = "ok" if meta["final_result"] == "ERFOLGREICH" else "fail"
    values["signature"] = png_img(signature_png)
    values["logo"] = png_img(logo_png, "logo")
    values["appendices"] = "".join(
        f"<h3>{html.escape(title)}</h3><pre>{html.escape(text.rstrip())}</pre>"
        for title, text in appendices)
    # substitute (nicht safe_substitute): fehlt ein Feld, knallt es sofort statt "${x}" im PDF
    return Template(TEMPLATE.read_text(encoding="utf-8")).substitute(values)


PDF_LOCK = threading.Lock()  # parallele nwipe-Läufe: nicht mehrere Chromium-Instanzen gleichzeitig


def render_pdf(html_file, pdf_file, footer):
    from playwright.sync_api import sync_playwright  # nur zum Rendern nötig, nicht für Tests der Logik
    footer_html = (
        '<div style="font-size:8pt;width:100%;margin:0 2cm;border-top:0.4pt solid #d4d4d4;'
        'padding-top:3px;display:flex;color:#333;font-family:Quicksand,DejaVu Sans,Arial,sans-serif">'
        f'<span style="flex:1;text-align:center">{html.escape(footer)}</span>'
        '<span class="pageNumber"></span></div>')
    with PDF_LOCK, sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(Path(html_file).resolve().as_uri())
        page.pdf(path=str(pdf_file), format="A4", print_background=True,
                 prefer_css_page_size=True, display_header_footer=True,
                 header_template="<span></span>", footer_template=footer_html)
        browser.close()


# --- System-Interaktion ---

def run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=check)


class Abbruch(Exception):
    pass


def die(msg):
    # Exception statt sys.exit: bei mehreren Laufwerken bricht nur das betroffene ab
    raise Abbruch(msg)


def need(cmd):
    if not shutil.which(cmd):
        die(f"'{cmd}' nicht gefunden. Bitte installieren.")


def ask(prompt, default=""):
    import readline  # nur interaktiv nötig; liefert den vorbelegten, editierbaren Standardwert
    readline.set_startup_hook(lambda: readline.insert_text(default))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


def step(msg):
    print(f"\n{GELB}--- {msg} ---{NC}")


def lsblk_info(device):
    out = run(["lsblk", "-J", "-d", "-b", "-o", "TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL", device], check=True)
    info = json.loads(out.stdout)["blockdevices"][0]
    return {k: ("" if v is None else v) for k, v in info.items()}


def security_status(device):
    return parse_security(run(["hdparm", "-I", device]).stdout)


def ask_continue(msg):
    return ask(f"\n{msg}, dann Enter ('abbruch' zum Beenden): ").strip() != "abbruch"


def io_bytes(device):
    """(gelesen, geschrieben) seit Boot laut Kernel; /sys/block/*/stat zählt immer in 512-Byte-Sektoren."""
    try:
        v = Path("/sys/block", Path(os.path.realpath(device)).name, "stat").read_text().split()
        return int(v[2]) * 512, int(v[6]) * 512
    except (OSError, IndexError, ValueError):
        return None


def progress_of(d):
    """Fortschritt eines laufenden Laufwerks aus run_all (Felder: started, io_start, ata_minutes)."""
    now_io = io_bytes(d["device"]) if d["io_start"] else None
    delta = (now_io[0] - d["io_start"][0], now_io[1] - d["io_start"][1]) if now_io else None
    return progress(d["method"], int(d["info"]["size"]), time.time() - d["started"], delta, d["ata_minutes"])


def list_disks():
    # --tree: neuere lsblk geben JSON sonst flach aus, dann hängen die Partitionen nicht am Laufwerk
    cols = "PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,MOUNTPOINT"
    return parse_disks(run(["lsblk", "-J", "--tree", "-b", "-o", cols]).stdout)


def unfreeze(device, serial, prompt=ask_continue):
    """Hebt 'frozen' auf und liefert den (nach Hot-Plug evtl. neuen) Gerätenamen."""
    if not security_status(device)["frozen"]:
        return device
    step("Laufwerk ist EINGEFROREN (frozen)")
    # Nur echter Standby (S3 "deep") schaltet das Laufwerk aus und hebt "frozen" auf.
    # Bei s2idle bleibt es bestromt, rtcwake -m mem bringt dann nichts.
    mem_sleep = Path("/sys/power/mem_sleep")
    modes = mem_sleep.read_text().strip() if mem_sleep.exists() else "unbekannt"
    if "deep" in modes:
        print("System geht für 5 Sekunden in Standby (S3)...")
        mem_sleep.write_text("deep")
        run(["rtcwake", "-m", "mem", "-s", "5"])
        time.sleep(5)  # Laufwerk nach dem Aufwachen neu anmelden lassen
        if not security_status(device)["frozen"]:
            return device
        print("Standby (S3) hat nicht geholfen, das BIOS friert das Laufwerk beim Aufwachen wieder ein.")
    else:
        print(f"Standby hilft hier nicht: kein S3 verfügbar (mem_sleep: {modes}), das Laufwerk bleibt bestromt.")
    if not serial:
        die("Laufwerk hat keine Seriennummer, nach Hot-Plug nicht sicher wiederzufinden. Methode 'nwipe' wählen.")
    while True:
        if not prompt(f"SATA-Stromkabel von {serial} im laufenden Betrieb ab- und wieder anstecken"):
            die("Vom Benutzer abgebrochen, Laufwerk ist weiterhin frozen.")
        time.sleep(3)  # Kernel braucht nach dem Anstecken einen Moment
        new = find_by_serial(run(["lsblk", "-J", "-d", "-o", "PATH,TYPE,SERIAL"]).stdout, serial)
        if not new:
            print("Laufwerk nicht (wieder) erkannt. Hot-Plug im BIOS aktiv? Kurz warten und erneut Enter.")
        elif run(["lsblk", "-n", "-o", "MOUNTPOINT", new]).stdout.strip():
            print(f"{new} wurde automatisch eingehängt, bitte aushängen.")
        elif security_status(new)["frozen"]:
            print(f"{new} ist immer noch frozen.")
        else:
            print(f"{GRUEN}{new} ist nicht mehr frozen.{NC}")
            return new


def erase_ata(device):
    step("Schritt 1: Initiale Statusprüfung")
    sec = security_status(device)
    if not sec["supported"]:
        die("Das Laufwerk unterstützt kein ATA Security (USB-Adapter? Dann Methode 'nwipe' wählen).")
    if sec["frozen"]:  # unfreeze() in main() lässt es eigentlich nicht so weit kommen
        die("Der Sicherheitsstatus ist EINGEFROREN (frozen).")
    print("Status 'not frozen' bestätigt.")

    step("Schritt 2: Passwort setzen und verifizieren")
    run(["hdparm", "--user-master", "u", "--security-set-pass", PASSWORD, device])
    time.sleep(2)
    if not security_status(device)["enabled"]:
        die("Das Laufwerk hat das Setzen des Passworts nicht akzeptiert.")
    print("Status 'enabled' bestätigt.")

    step("Schritt 3: Löschbefehl senden")
    if sec["enhanced"]:
        cmd, method = "--security-erase-enhanced", "ATA Secure Erase (Enhanced)"
    else:
        cmd, method = "--security-erase", "ATA Secure Erase (Standard)"
    print(f"Führe {method} aus... (kann je nach Laufwerk dauern)")
    erase = run(["hdparm", "--user-master", "u", cmd, PASSWORD, device])

    step("Schritt 4: Finale Verifizierung")
    time.sleep(5)
    ok = not security_status(device)["enabled"]
    if not ok:
        # Sonst bleibt das Laufwerk nach dem nächsten Neustart mit unserem Passwort gesperrt
        run(["hdparm", "--user-master", "u", "--security-disable", PASSWORD, device])
        print(f"{ROT}Passwort wurde wieder entfernt. hdparm-Ausgabe:{NC}\n{erase.stdout}{erase.stderr}")
    fields = {"erase_method": method, "verification": "Finaler Sicherheitsstatus geprüft"}
    return ok, fields, [("Ausgabe hdparm", erase.stdout + erase.stderr)]


def erase_nvme(device):
    step("NVMe Format mit User Data Erase (SES=1)")
    r = run(["nvme", "format", device, "--ses=1", "--force"])
    output = r.stdout + r.stderr
    print(output)
    fields = {"erase_method": "NVMe Format (User Data Erase)",
              "verification": "Rückmeldung des Controllers geprüft"}
    return r.returncode == 0, fields, [("Ausgabe nvme format", output)]


def erase_nwipe(device, log_file, quiet=False):
    step(f"Überschreiben mit Nullen + Verifizierung (nwipe) auf {device}")
    print("Dies kann viele Stunden dauern. Bitte warten...")
    # Einzeln: Ausgabe durchreichen, nwipe zeigt so seinen Fortschritt direkt an.
    # Parallel (quiet): stdout verwerfen, sonst vermischen sich die Läufe; das Log steht in log_file.
    code = subprocess.run(["nwipe", "--autonuke", "--nogui", "--method=zero", "--rounds=1",
                           "--verify=last", f"--logfile={log_file}", device],
                          stdout=subprocess.DEVNULL if quiet else None, check=False).returncode
    log = log_file.read_text(errors="replace") if log_file.exists() else "(kein Protokoll erzeugt)"
    fields = {"erase_method": "Überschreiben mit Nullen (nwipe)", "rounds": "1",
              "prng": "Nicht zutreffend (Nullen)", "errors": f"nwipe Exit-Code {code}",
              "verification": "Letzter Durchgang vollständig zurückgelesen"}
    return code == 0, fields, [("Vollständiges nwipe-Protokoll", log)]


def main():
    os.chdir(HERE)  # Vorlage, Unterschrift und Reports liegen neben dem Skript
    os.system("clear")
    print(f"{GELB}Starte initiale Überprüfungen...{NC}")
    preflight()
    print(f"{GRUEN}Basis-Abhängigkeiten sind installiert.{NC}\n")

    print(f"{GELB}--- Optionale Metadaten für den Report (Enter für Standardwerte) ---{NC}")
    defaults = load_defaults()
    meta = {key: ask(f"{label}: ", defaults.get(key, "-")) for key, label in META_FIELDS}
    meta["place"] = defaults.get("place", "")
    print("\nVerfügbare Laufwerke werden aufgelistet:")
    subprocess.run(["lsblk", "-d", "-o", "NAME,SIZE,ROTA,TRAN,MODEL,SERIAL"], check=False)
    names = ask("\nGerätenamen der zu löschenden Laufwerke, mehrere mit Leerzeichen (z.B. /dev/sda /dev/sdb): ").split()
    if not names:
        die("Kein Laufwerk angegeben.")
    disks, seen = [], set()
    for device in names:
        if not Path(device).is_block_device() or lsblk_info(device)["type"] != "disk":
            die(f"'{device}' ist kein ganzes Laufwerk (Partition oder ungültig).")
        if run(["lsblk", "-n", "-o", "MOUNTPOINT", device]).stdout.strip():
            subprocess.run(["lsblk", device], check=False)
            die(f"'{device}' hat eingehängte Partitionen. Abbruch.")
        info = lsblk_info(device)
        serial = info["serial"].strip()
        key = serial or os.path.realpath(device)
        if key in seen:
            die(f"'{device}' wurde doppelt angegeben.")
        seen.add(key)

        auto = pick_method(info["tran"], info["rota"])
        print(f"\n{GELB}{device}{NC}: {info['model']} / {serial or '?'} / {size_gb(int(info['size']))}")
        print(f"Erkannt: Bus={GELB}{info['tran'].upper() or '?'}{NC}, "
              f"rotierend={GELB}{'ja' if is_true(info['rota']) else 'nein'}{NC} -> Vorschlag: {GELB}{auto}{NC}")
        while True:
            method = ask("Löschmethode (ata/nvme/nwipe): ", auto).strip()
            if method not in METHODS:
                print(f"{ROT}Unbekannte Methode '{method}'.{NC}")
                continue
            need(METHODS[method][3])
            # Vorab prüfen, nicht erst mitten im Lauf: dann kann direkt nwipe gewählt werden
            if method == "ata" and not security_status(device)["supported"]:
                print(f"{ROT}{device} unterstützt kein ATA Security (USB-Adapter?). Bitte andere Methode wählen.{NC}")
                auto = "nwipe"
                continue
            break
        comment = ask("Bemerkung (z.B. Einsatzort: Buchhaltung PC 3, leer = keine): ")
        disks.append({"device": device, "info": info, "serial": serial, "method": method, "comment": comment})

    print(f"\n{ROT}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ACHTUNG !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!{NC}")
    print(f"Sie haben {len(disks)} Laufwerk(e) ausgewählt:")
    for d in disks:
        _, nist, warn, _ = METHODS[d["method"]]
        print(f"  {GELB}{d['device']}{NC} ({d['info']['model']} / {d['serial']} / "
              f"{size_gb(int(d['info']['size']))}): {ROT}{warn} ({nist}){NC}")
    print(f"Dieser Vorgang wird {ROT}ALLE DATEN AUF DIESEN LAUFWERKEN UNWIDERRUFLICH ZERSTÖREN{NC}.")
    print(f"{ROT}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!{NC}\n")
    if input("Sind Sie absolut sicher, dass Sie fortfahren möchten? (ja/nein): ").strip() != "ja":
        print("Vom Benutzer abgebrochen.")
        return

    run_all(disks, meta)
    print(f"\n{GELB}--- ZUSAMMENFASSUNG ---{NC}")
    for d in disks:
        print(f"  {d['device']} ({d['serial']}): {GRUEN if d['result'] == 'ERFOLGREICH' else ROT}{d['result']}{NC}")


def give_to_user(*paths):
    """Unter sudo die Dateien dem aufrufenden Benutzer geben. Als echtes root (Live-Stick) nichts zu tun,
    auf exFAT gibt es keine Besitzer (chown schlägt fehl, wird ignoriert)."""
    if "SUDO_UID" not in os.environ:
        return
    for f in paths:
        if f.exists():
            with contextlib.suppress(OSError):
                os.chown(f, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))


def load_defaults():
    if not CONFIG.exists():
        return {}
    try:
        return parse_config(CONFIG.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        die(f"{CONFIG} ist fehlerhaft: {e}")


def preflight():
    if os.geteuid() != 0:
        die("Bitte mit 'sudo' ausführen.")
    # Live-Stick: ohne Reports-Partition landen Reports im RAM und sind nach dem Ausschalten weg
    if os.environ.get("ERASE_KIOSK") and not os.path.ismount(DATA):
        die(f"Reports-Partition des Sticks ist nicht eingehängt ({DATA}).\nStick mit live/make-stick.sh erstellt?")
    for cmd in ("lsblk", "smartctl", "hexdump"):
        need(cmd)
    try:
        import playwright  # noqa: F401
    except ImportError:
        die("Python-Paket 'playwright' fehlt: pip install -r requirements.txt && playwright install chromium")


def run_all(disks, meta, prompt=ask_continue, quiet=False, on_status=lambda d: None):
    """Löscht alle Laufwerke, Ergebnis steht danach in d["result"]. on_status meldet jede Änderung."""
    def set_result(d, result):
        d["result"] = result
        on_status(d)

    # Eingefrorene Laufwerke vorab lösen, danach läuft alles ohne Eingriff durch
    for d in disks:
        d["result"] = None
        if d["method"] == "ata":
            try:
                d["device"] = unfreeze(d["device"], d["serial"], prompt)
            except Abbruch as e:
                print(f"{ROT}Fehler: {e}{NC}")
                set_result(d, "ÜBERSPRUNGEN (frozen, kein Report)")

    REPORTS.mkdir(exist_ok=True)
    give_to_user(REPORTS)

    def erase_one(d, quiet=False):
        # Messpunkte für progress_of(), vor "LÄUFT", damit die GUI sie beim ersten Abfragen schon hat
        d.update(started=time.time(), io_start=io_bytes(d["device"]),
                 ata_minutes=erase_minutes(run(["hdparm", "-I", d["device"]]).stdout) if d["method"] == "ata" else None)
        set_result(d, "LÄUFT")
        print(f"\n{GELB}===== Laufwerk {d['device']} ({d['serial']}) ====={NC}")
        try:
            # Schutz vor Umbenennung (Hot-Plug, Standby): nie ein anderes Laufwerk löschen als bestätigt
            if d["serial"] and lsblk_info(d["device"])["serial"].strip() != d["serial"]:
                die(f"{d['device']} hat nicht mehr die Seriennummer {d['serial']}.")
            set_result(d, erase_disk(d, meta, quiet))
        except Abbruch as e:
            print(f"{ROT}Fehler bei {d['device']}: {e}{NC}")
            set_result(d, "ABGEBROCHEN (kein Report)")

    todo = [d for d in disks if d["result"] is None]
    # ATA/NVMe dauern Minuten und laufen nacheinander, nwipe dauert Stunden und läuft parallel
    for d in todo:
        if d["method"] != "nwipe":
            erase_one(d)
    wipes = [d for d in todo if d["method"] == "nwipe"]
    if len(wipes) > 1:
        print(f"\n{GELB}Starte {len(wipes)} nwipe-Läufe parallel. Fortschritt: tail -f reports/nwipe_*.log{NC}")
    with ThreadPoolExecutor(max(len(wipes), 1)) as pool:
        list(pool.map(lambda d: erase_one(d, quiet or len(wipes) > 1), wipes))


def erase_disk(d, shared_meta, quiet=False):
    """Löscht ein Laufwerk und schreibt seinen Report. Liefert das Ergebnis als Text."""
    device, info, serial, method = d["device"], d["info"], d["serial"], d["method"]
    drive_kind, nist, _, _ = METHODS[method]
    meta = dict(shared_meta)
    if d["comment"]:
        meta["comment"] = d["comment"]
    size = int(info["size"])

    print(f"{GELB}Sammle erweiterte Laufwerksinformationen...{NC}")
    hpa = [line.strip() for line in run(["hdparm", "-N", device]).stdout.splitlines() if "HPA" in line]
    smart_before = run(["smartctl", "-x", device]).stdout
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nwipe_log = REPORTS / f"nwipe_{Path(device).name}_{stamp}.log"

    start = datetime.now()
    if method == "ata":
        ok, fields, extra = erase_ata(device)
    elif method == "nvme":
        ok, fields, extra = erase_nvme(device)
    else:
        ok, fields, extra = erase_nwipe(device, nwipe_log, quiet)
    end = datetime.now()
    seconds = int((end - start).total_seconds())

    result = "ERFOLGREICH" if ok else "FEHLGESCHLAGEN"
    color = GRUEN if ok else ROT
    print(f"{color}{device}: Löschvorgang {result}.{NC}")

    meta.update({
        "title": f"Protokoll zur Datenlöschung ({drive_kind})",
        "subtitle": f"Methode: {fields['erase_method']} ({nist})",
        "author": meta["biz_contact"],
        "date": de_date(end),
        "device": device, "drive_model": info["model"].strip(), "drive_serial": serial,
        "bus_type": info["tran"].upper(), "drive_size_bytes": size, "drive_size_gb": size_gb(size),
        "hpa_dco_status": " ".join(hpa) or "HPA/DCO not detected",
        "start_time": start.strftime("%d.%m.%Y %H:%M:%S"),
        "end_time": end.strftime("%d.%m.%Y %H:%M:%S"),
        "duration": hms(seconds),
        "rounds": "1 (Firmware-intern)", "prng": "Nicht zutreffend",
        "errors": "Nicht zutreffend", "throughput": "Nicht zutreffend",
        "bytes_erased": size,
        "final_result": result,
    })
    meta.update(fields)
    if method == "nwipe" and seconds > 0:
        meta["throughput"] = f"{size // seconds // 1000000} MB/s (inkl. Verifizierung)"

    step(f"Erstelle finalen Report für {device}")
    appendices = [
        ("SMART Werte vor der Löschung", smart_before),
        ("SMART Werte nach erfolgter Löschung", run(["smartctl", "-x", device]).stdout),
        ("Stichproben-Verifizierung (Hexdump nach dem Löschen)",
         run(["hexdump", "-n", str(HEXDUMP_BYTES), "-C", device]).stdout),
        *extra,
    ]
    base = f"erase_report_{re.sub(r'[^A-Za-z0-9_-]', '', serial) or 'unbekannt'}_{stamp}"
    html_file, pdf = REPORTS / (base + ".html"), REPORTS / (base + ".pdf")
    signature = SIGNATURE.read_bytes() if SIGNATURE.exists() else None
    logo = LOGO.read_bytes() if LOGO.exists() else None
    html_file.write_text(build_html(meta, appendices, signature, logo), encoding="utf-8")
    try:
        render_pdf(html_file, pdf, f"{meta['date']} | {meta['drive_model']} | {serial}")
        print(f"{GRUEN}PDF-Report erfolgreich erstellt: {pdf}{NC}")
    except Exception as e:  # noqa: BLE001 - HTML-Report bleibt in jedem Fall erhalten
        print(f"{GELB}PDF-Erstellung fehlgeschlagen ({e}). HTML-Report: {html_file}{NC}")

    give_to_user(html_file, pdf, nwipe_log)
    os.sync()  # Live-Stick (exFAT, kein Journal): Report sicher auf dem Stick, auch wenn er gleich gezogen wird
    return result


if __name__ == "__main__":
    try:
        main()
    except Abbruch as e:
        print(f"{ROT}Fehler: {e}{NC}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{ROT}Abgebrochen (Strg+C).{NC}")
        sys.exit(130)
