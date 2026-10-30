#!/usr/bin/env python3
"""TabulaRasa: erase HDDs, SATA SSDs and NVMe SSDs and write one PDF report per drive.

Methods (suggested from the drive type, can be overridden):
  ata   = ATA Secure Erase (SATA SSD)          -> NIST 800-88 Purge
  nvme  = NVMe Format, User Data Erase (NVMe)  -> NIST 800-88 Purge
  nwipe = 1 pass of zeros + verify (HDD)       -> NIST 800-88 Clear

!! RUN AS ROOT AND FROM A LIVE SYSTEM ONLY !!
The user interface (terminal and gui.py) is English; reports are German or English (config.toml).
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

PASSWORD = "EraseMyDisk"  # temporary ATA password, removed again by the erase itself
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "report.html"
# Data folder with everything company-specific: config.toml, logo.png, signature.png, reports/.
# On the live stick this is the REPORTS partition (ERASE_HOME), otherwise the script folder.
DATA = Path(os.environ.get("ERASE_HOME", HERE))
CONFIG = DATA / "config.toml"
SIGNATURE = DATA / "signature.png"
LOGO = DATA / "logo.png"
REPORTS = DATA / "reports"
HEXDUMP_BYTES = 1024000

RED, GREEN, YELLOW, NC = "\033[0;31m", "\033[0;32m", "\033[1;33m", "\033[0m"

# (key, label) shared by terminal and GUI, prefilled from config.toml
META_FIELDS = [
    ("biz_name", "Company name"),
    ("biz_address", "Company address"),
    ("biz_contact", "Company contact person"),
    ("biz_phone", "Company phone"),
    ("cust_name", "Customer name"),
    ("cust_address", "Customer address"),
    ("cust_contact", "Customer contact person"),
    ("cust_phone", "Customer phone"),
]
# [company] section in config.toml -> report key
CONFIG_KEYS = {"name": "biz_name", "address": "biz_address", "contact": "biz_contact",
               "phone": "biz_phone", "place": "place"}

# method: (drive kind, NIST 800-88 level, warning shown before erasing, required tool)
METHODS = {
    "ata": ("SSD", "NIST 800-88 Purge", "ATA SECURE ERASE", "hdparm"),
    "nvme": ("NVMe-SSD", "NIST 800-88 Purge", "NVMe FORMAT / USER DATA ERASE", "nvme"),
    "nwipe": ("HDD", "NIST 800-88 Clear", "OVERWRITE WITH ZEROS + VERIFY", "nwipe"),
}

# All report texts. report.html only contains ${t_<key>} placeholders; the erase functions return
# codes (e.g. "ata_enhanced") that are looked up here. New language = new column with the same keys.
REPORT_TEXT = {
    "de": {
        "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
                   "August", "September", "Oktober", "November", "Dezember"],
        "date": "{day:02d}. {month} {year}", "timestamp": "%d.%m.%Y %H:%M:%S", "place_date": "{place}, den {date}",
        "title": "Protokoll zur Datenlöschung ({kind})", "subtitle": "Methode: {method} ({nist})",
        "ok": "ERFOLGREICH", "fail": "FEHLGESCHLAGEN", "na": "Nicht zutreffend",
        "cover_title": "Protokoll zur<br>Datenlöschung", "performed_by": "Durchgeführt von",
        "cert_title": "Zertifikat zur Datenlöschung",
        "section_business": "Ausführende Organisation", "section_customer": "Kundendetails",
        "section_device": "Geräteinformationen", "section_erase": "Details zur Löschung",
        "section_comment": "Bemerkung", "appendices": "Anhänge",
        "company": "Firma", "contact": "Kontaktperson", "address": "Adresse", "phone": "Telefon", "name": "Name",
        "model": "Modell", "serial": "Seriennummer", "capacity": "Kapazität", "date_label": "Datum",
        "size": "Größe", "bus": "Bus", "start": "Startzeit", "end": "Endzeit", "duration": "Dauer",
        "status": "Status", "method": "Methode", "prng": "PRNG Algorithmus", "rounds": "Durchgänge",
        "verification": "Verifizierung", "bytes_erased": "Gelöschte Bytes", "throughput": "Durchsatz",
        "hpa_dco": "HPA/DCO Status", "errors": "Fehler",
        "confirm_title": "Bestätigung",
        "confirm_text": "Hiermit wird bestätigt, dass die oben genannten Löschmaßnahmen am angegebenen Datum "
                        "erfolgreich durchgeführt wurden.",
        "rounds_firmware": "1 (Firmware-intern)", "hpa_none": "HPA/DCO nicht erkannt",
        "throughput_verify": "{mbs} MB/s (inkl. Verifizierung)",
        "ata_enhanced": "ATA Secure Erase (Enhanced)", "ata_standard": "ATA Secure Erase (Standard)",
        "nvme_format": "NVMe Format (User Data Erase)", "nwipe_zero": "Überschreiben mit Nullen (nwipe)",
        "ata_verified": "Finaler Sicherheitsstatus geprüft", "nvme_verified": "Rückmeldung des Controllers geprüft",
        "nwipe_verified": "Letzter Durchgang vollständig zurückgelesen",
        "prng_zero": "Nicht zutreffend (Nullen)", "nwipe_exit": "nwipe Exit-Code {code}",
        "no_log": "(kein Protokoll erzeugt)",
        "appx_smart_before": "SMART Werte vor der Löschung", "appx_smart_after": "SMART Werte nach erfolgter Löschung",
        "appx_hexdump": "Stichproben-Verifizierung (Hexdump nach dem Löschen)",
        "appx_hdparm": "Ausgabe hdparm", "appx_nvme": "Ausgabe nvme format",
        "appx_nwipe": "Vollständiges nwipe-Protokoll",
    },
    "en": {
        "months": ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"],
        "date": "{day} {month} {year}", "timestamp": "%Y-%m-%d %H:%M:%S", "place_date": "{place}, {date}",
        "title": "Data Erasure Report ({kind})", "subtitle": "Method: {method} ({nist})",
        "ok": "SUCCESSFUL", "fail": "FAILED", "na": "Not applicable",
        "cover_title": "Data Erasure<br>Report", "performed_by": "Performed by",
        "cert_title": "Certificate of Data Erasure",
        "section_business": "Performing Organisation", "section_customer": "Customer Details",
        "section_device": "Device Information", "section_erase": "Erasure Details",
        "section_comment": "Comment", "appendices": "Appendices",
        "company": "Company", "contact": "Contact person", "address": "Address", "phone": "Phone", "name": "Name",
        "model": "Model", "serial": "Serial number", "capacity": "Capacity", "date_label": "Date",
        "size": "Size", "bus": "Bus", "start": "Start time", "end": "End time", "duration": "Duration",
        "status": "Status", "method": "Method", "prng": "PRNG algorithm", "rounds": "Passes",
        "verification": "Verification", "bytes_erased": "Bytes erased", "throughput": "Throughput",
        "hpa_dco": "HPA/DCO status", "errors": "Errors",
        "confirm_title": "Confirmation",
        "confirm_text": "This is to confirm that the erasure described above was carried out successfully "
                        "on the date stated.",
        "rounds_firmware": "1 (firmware-internal)", "hpa_none": "HPA/DCO not detected",
        "throughput_verify": "{mbs} MB/s (incl. verification)",
        "ata_enhanced": "ATA Secure Erase (Enhanced)", "ata_standard": "ATA Secure Erase (Standard)",
        "nvme_format": "NVMe Format (User Data Erase)", "nwipe_zero": "Overwrite with zeros (nwipe)",
        "ata_verified": "Final security state checked", "nvme_verified": "Controller response checked",
        "nwipe_verified": "Last pass fully read back",
        "prng_zero": "Not applicable (zeros)", "nwipe_exit": "nwipe exit code {code}",
        "no_log": "(no log written)",
        "appx_smart_before": "SMART values before erasure", "appx_smart_after": "SMART values after erasure",
        "appx_hexdump": "Spot check (hexdump after erasure)",
        "appx_hdparm": "hdparm output", "appx_nvme": "nvme format output",
        "appx_nwipe": "Complete nwipe log",
    },
}


# --- Pure helpers (tested in test_erase.py) ---

def format_date(dt, lang="de"):
    """Month names come from REPORT_TEXT, not from the locale (live systems often have none installed)."""
    t = REPORT_TEXT[lang]
    return t["date"].format(day=dt.day, month=t["months"][dt.month - 1], year=dt.year)


def size_gb(size_bytes):
    return f"{size_bytes / 1000**3:.2f} GB"


def hms(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def is_true(value):
    """Depending on its version, lsblk reports ROTA as bool, "1"/"0" or "true"/"false"."""
    return str(value).strip().lower() in ("1", "true")


def pick_method(tran, rota):
    if (tran or "").lower() == "nvme":
        return "nvme"
    return "nwipe" if is_true(rota) else "ata"


def parse_security(hdparm_i):
    """Parse the 'Security:' block of 'hdparm -I'.

    Positive lines are only indented ("\\t\\tenabled"), negative ones are prefixed
    with "not" ("\\tnot\\tenabled"), hence the match at the start of the line.
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
    """Device path for a serial number; after a hot-plug the name changes (sda -> sdb)."""
    paths = [d["path"] for d in json.loads(lsblk_json)["blockdevices"]
             if d.get("type") == "disk" and (d.get("serial") or "").strip() == serial]
    return paths[0] if serial and len(paths) == 1 else None


def parse_config(text):
    """Defaults from config.toml. Raises ValueError (incl. TOMLDecodeError) for invalid content."""
    cfg = tomllib.loads(text)
    language = cfg.get("language", "de")
    if language not in REPORT_TEXT:
        raise ValueError(f"language = {language!r}, supported: {', '.join(REPORT_TEXT)}")
    company = cfg.get("company", {})
    return {"language": language, **{CONFIG_KEYS[k]: str(v) for k, v in company.items() if k in CONFIG_KEYS}}


def erase_minutes(hdparm_i):
    """Vendor estimate from 'hdparm -I', e.g. '2min for SECURITY ERASE UNIT' (enhanced or normal)."""
    enhanced = "ENHANCED " if parse_security(hdparm_i)["enhanced"] else ""
    m = re.search(rf"(\d+)min for {enhanced}SECURITY ERASE UNIT", hdparm_i)
    return int(m.group(1)) if m else None


def progress(method, size, elapsed, io_delta=None, ata_minutes=None, now=None):
    """(fraction 0..1 or None, text) for the GUI progress display.

    nwipe: bytes written + read (pass + verify) against 2x the drive size.
    ata:   the firmware erases internally, so only elapsed time against the vendor estimate, capped at 99 %.
    """
    def eta(seconds):
        now_ = now or datetime.now()
        end = now_ + timedelta(seconds=seconds)
        fmt = "%H:%M" if end.date() == now_.date() else "%Y-%m-%d %H:%M"
        return f"{hms(int(seconds))} left · done ~{end:{fmt}}"

    if method == "nwipe" and io_delta and elapsed > 0:
        read, written = io_delta
        done = min(written, size) + min(read, size)
        frac = done / (2 * size)
        phase = "verifying" if written >= size else "writing"
        rate = done / elapsed
        text = f"{frac:.0%} {phase} · {rate / 1e6:.0f} MB/s"
        return frac, f"{text} · {eta((2 * size - done) / rate)}" if rate else text
    if method == "ata" and ata_minutes:
        total = ata_minutes * 60
        frac = min(elapsed / total, 0.99)
        return frac, f"~{frac:.0%} · {eta(max(total - elapsed, 0))}"
    return None, ""


def parse_disks(lsblk_json):
    """Whole disks from 'lsblk -J --tree -b -o PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,MOUNTPOINT'."""
    def mounted(dev):
        return bool(dev.get("mountpoint")) or any(mounted(c) for c in dev.get("children", []))
    return [{**{k: ("" if v is None else v) for k, v in d.items() if k != "children"}, "mounted": mounted(d)}
            for d in json.loads(lsblk_json)["blockdevices"] if d.get("type") == "disk"]


def build_html(meta, appendices, signature_png=None, logo_png=None):
    """meta: fields for report.html (incl. "ok" and "language"), appendices: list of (title, text)."""
    t = REPORT_TEXT[meta.get("language", "de")]
    values = {k: html.escape(str(v)) for k, v in meta.items()}
    values.update({f"t_{k}": v for k, v in t.items() if isinstance(v, str)})  # our own texts, not escaped
    values["lang"] = meta.get("language", "de")
    values["final_result"] = t["ok"] if meta["ok"] else t["fail"]
    values["status_class"] = "ok" if meta["ok"] else "fail"
    comment = meta.get("comment")
    values["comment_block"] = (
        f'<table><tr><th class="section">{t["section_comment"]}</th></tr>'
        f"<tr><td>{html.escape(comment)}</td></tr></table>" if comment else "")
    values["place_date"] = html.escape(
        t["place_date"].format(place=meta["place"], date=meta["date"]) if meta.get("place") else meta["date"])
    values["signature"] = png_img(signature_png)
    values["logo"] = png_img(logo_png, "logo")
    values["appendices"] = "".join(
        f"<h3>{html.escape(title)}</h3><pre>{html.escape(text.rstrip())}</pre>"
        for title, text in appendices)
    # substitute, not safe_substitute: a missing field fails right here instead of "${x}" ending up in the PDF
    return Template(TEMPLATE.read_text(encoding="utf-8")).substitute(values)


PDF_LOCK = threading.Lock()  # parallel nwipe runs: never several Chromium instances at once


def render_pdf(html_file, pdf_file, footer):
    from playwright.sync_api import sync_playwright  # only needed for rendering, not for testing the logic
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


# --- System interaction ---

def run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=check)


class Abort(Exception):
    pass


def die(msg):
    # An exception instead of sys.exit: with several drives only the affected one is aborted
    raise Abort(msg)


def need(cmd):
    if not shutil.which(cmd):
        die(f"'{cmd}' not found. Please install it.")


def ask(prompt, default=""):
    import readline  # only needed interactively; provides the prefilled, editable default
    readline.set_startup_hook(lambda: readline.insert_text(default))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


def step(msg):
    print(f"\n{YELLOW}--- {msg} ---{NC}")


def lsblk_info(device):
    out = run(["lsblk", "-J", "-d", "-b", "-o", "TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL", device], check=True)
    info = json.loads(out.stdout)["blockdevices"][0]
    return {k: ("" if v is None else v) for k, v in info.items()}


def security_status(device):
    return parse_security(run(["hdparm", "-I", device]).stdout)


def ask_continue(msg):
    return ask(f"\n{msg}, then press Enter ('abort' to give up): ").strip() != "abort"


def io_bytes(device):
    """(read, written) bytes since boot according to the kernel; /sys/block/*/stat always counts 512-byte sectors."""
    try:
        v = Path("/sys/block", Path(os.path.realpath(device)).name, "stat").read_text().split()
        return int(v[2]) * 512, int(v[6]) * 512
    except (OSError, IndexError, ValueError):
        return None


def progress_of(d):
    """Progress of a drive running in run_all (fields: started, io_start, ata_minutes)."""
    now_io = io_bytes(d["device"]) if d["io_start"] else None
    delta = (now_io[0] - d["io_start"][0], now_io[1] - d["io_start"][1]) if now_io else None
    return progress(d["method"], int(d["info"]["size"]), time.time() - d["started"], delta, d["ata_minutes"])


def list_disks():
    # --tree: newer lsblk versions print flat JSON otherwise, then partitions are not attached to their disk
    cols = "PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,MOUNTPOINT"
    return parse_disks(run(["lsblk", "-J", "--tree", "-b", "-o", cols]).stdout)


def unfreeze(device, serial, prompt=ask_continue):
    """Lift the 'frozen' state and return the device name (it may change after a hot-plug)."""
    if not security_status(device)["frozen"]:
        return device
    step("Drive is FROZEN")
    # Only real standby (S3 "deep") powers the drive off and lifts "frozen".
    # With s2idle the drive stays powered, so rtcwake -m mem achieves nothing.
    mem_sleep = Path("/sys/power/mem_sleep")
    modes = mem_sleep.read_text().strip() if mem_sleep.exists() else "unknown"
    if "deep" in modes:
        print("Putting the system into standby (S3) for 5 seconds...")
        mem_sleep.write_text("deep")
        run(["rtcwake", "-m", "mem", "-s", "5"])
        time.sleep(5)  # give the drive time to come back after waking up
        if not security_status(device)["frozen"]:
            return device
        print("Standby (S3) did not help, the BIOS freezes the drive again on wake-up.")
    else:
        print(f"Standby does not help here: no S3 available (mem_sleep: {modes}), the drive stays powered.")
    if not serial:
        die("Drive has no serial number and cannot be found reliably after a hot-plug. Use method 'nwipe'.")
    while True:
        if not prompt(f"Unplug and replug the SATA power cable of {serial} while the machine is running"):
            die("Aborted by user, drive is still frozen.")
        time.sleep(3)  # the kernel needs a moment after plugging in
        new = find_by_serial(run(["lsblk", "-J", "-d", "-o", "PATH,TYPE,SERIAL"]).stdout, serial)
        if not new:
            print("Drive not detected (yet). Is hot-plug enabled in the BIOS? Wait a moment and press Enter again.")
        elif run(["lsblk", "-n", "-o", "MOUNTPOINT", new]).stdout.strip():
            print(f"{new} was mounted automatically, please unmount it.")
        elif security_status(new)["frozen"]:
            print(f"{new} is still frozen.")
        else:
            print(f"{GREEN}{new} is no longer frozen.{NC}")
            return new


# The erase_* functions return (ok, fields, appendices). Report texts in fields and appendix
# titles are REPORT_TEXT codes, translated in erase_disk() into the report language.

def erase_ata(device):
    step("Step 1: initial status check")
    sec = security_status(device)
    if not sec["supported"]:
        die("The drive does not support ATA Security (USB adapter? Then use method 'nwipe').")
    if sec["frozen"]:  # unfreeze() in run_all() normally prevents getting here
        die("The security state is FROZEN.")
    print("State 'not frozen' confirmed.")

    step("Step 2: set and verify password")
    run(["hdparm", "--user-master", "u", "--security-set-pass", PASSWORD, device])
    time.sleep(2)
    if not security_status(device)["enabled"]:
        die("The drive did not accept the password.")
    print("State 'enabled' confirmed.")

    step("Step 3: send erase command")
    if sec["enhanced"]:
        cmd, method = "--security-erase-enhanced", "ata_enhanced"
    else:
        cmd, method = "--security-erase", "ata_standard"
    print(f"Running {REPORT_TEXT['en'][method]}... (may take a while depending on the drive)")
    erase = run(["hdparm", "--user-master", "u", cmd, PASSWORD, device])

    step("Step 4: final verification")
    time.sleep(5)
    ok = not security_status(device)["enabled"]
    if not ok:
        # Otherwise the drive stays locked with our password after the next reboot
        run(["hdparm", "--user-master", "u", "--security-disable", PASSWORD, device])
        print(f"{RED}Password removed again. hdparm output:{NC}\n{erase.stdout}{erase.stderr}")
    fields = {"erase_method": method, "verification": "ata_verified"}
    return ok, fields, [("appx_hdparm", erase.stdout + erase.stderr)]


def erase_nvme(device):
    step("NVMe Format with User Data Erase (SES=1)")
    r = run(["nvme", "format", device, "--ses=1", "--force"])
    output = r.stdout + r.stderr
    print(output)
    fields = {"erase_method": "nvme_format", "verification": "nvme_verified"}
    return r.returncode == 0, fields, [("appx_nvme", output)]


def erase_nwipe(device, log_file, quiet=False):
    step(f"Overwrite with zeros + verification (nwipe) on {device}")
    print("This can take many hours. Please wait...")
    # Single run: pass the output through, so nwipe shows its progress directly.
    # Parallel (quiet): discard stdout or the runs get mixed up; the log is in log_file.
    code = subprocess.run(["nwipe", "--autonuke", "--nogui", "--method=zero", "--rounds=1",
                           "--verify=last", f"--logfile={log_file}", device],
                          stdout=subprocess.DEVNULL if quiet else None, check=False).returncode
    log = log_file.read_text(errors="replace") if log_file.exists() else None
    fields = {"erase_method": "nwipe_zero", "rounds": "1", "prng": "prng_zero",
              "errors": ("nwipe_exit", code), "verification": "nwipe_verified"}
    return code == 0, fields, [("appx_nwipe", log)]


def main():
    os.chdir(HERE)  # template and default data folder live next to the script
    os.system("clear")
    print(f"{YELLOW}Running initial checks...{NC}")
    preflight()
    print(f"{GREEN}Basic dependencies are installed.{NC}\n")

    print(f"{YELLOW}--- Report details (Enter keeps the default) ---{NC}")
    defaults = load_defaults()
    meta = {key: ask(f"{label}: ", defaults.get(key, "-")) for key, label in META_FIELDS}
    meta["place"], meta["language"] = defaults.get("place", ""), defaults["language"]
    print("\nAvailable drives:")
    subprocess.run(["lsblk", "-d", "-o", "NAME,SIZE,ROTA,TRAN,MODEL,SERIAL"], check=False)
    names = ask("\nDevice names of the drives to erase, separated by spaces (e.g. /dev/sda /dev/sdb): ").split()
    if not names:
        die("No drive given.")
    disks, seen = [], set()
    for device in names:
        if not Path(device).is_block_device() or lsblk_info(device)["type"] != "disk":
            die(f"'{device}' is not a whole drive (partition or invalid).")
        if run(["lsblk", "-n", "-o", "MOUNTPOINT", device]).stdout.strip():
            subprocess.run(["lsblk", device], check=False)
            die(f"'{device}' has mounted partitions. Aborting.")
        info = lsblk_info(device)
        serial = info["serial"].strip()
        key = serial or os.path.realpath(device)
        if key in seen:
            die(f"'{device}' was given twice.")
        seen.add(key)

        auto = pick_method(info["tran"], info["rota"])
        print(f"\n{YELLOW}{device}{NC}: {info['model']} / {serial or '?'} / {size_gb(int(info['size']))}")
        print(f"Detected: bus={YELLOW}{info['tran'].upper() or '?'}{NC}, "
              f"rotational={YELLOW}{'yes' if is_true(info['rota']) else 'no'}{NC} -> suggestion: {YELLOW}{auto}{NC}")
        while True:
            method = ask("Erase method (ata/nvme/nwipe): ", auto).strip()
            if method not in METHODS:
                print(f"{RED}Unknown method '{method}'.{NC}")
                continue
            need(METHODS[method][3])
            # Check up front, not halfway through the run: then nwipe can be chosen right away
            if method == "ata" and not security_status(device)["supported"]:
                print(f"{RED}{device} does not support ATA Security (USB adapter?). Please choose another method.{NC}")
                auto = "nwipe"
                continue
            break
        comment = ask("Comment (e.g. location: accounting PC 3, empty = none): ")
        disks.append({"device": device, "info": info, "serial": serial, "method": method, "comment": comment})

    print(f"\n{RED}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!{NC}")
    print(f"You selected {len(disks)} drive(s):")
    for d in disks:
        _, nist, warn, _ = METHODS[d["method"]]
        print(f"  {YELLOW}{d['device']}{NC} ({d['info']['model']} / {d['serial']} / "
              f"{size_gb(int(d['info']['size']))}): {RED}{warn} ({nist}){NC}")
    print(f"This will {RED}IRREVERSIBLY DESTROY ALL DATA ON THESE DRIVES{NC}.")
    print(f"{RED}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!{NC}\n")
    if input("Are you absolutely sure you want to continue? (yes/no): ").strip() != "yes":
        print("Aborted by user.")
        return

    run_all(disks, meta)
    print(f"\n{YELLOW}--- SUMMARY ---{NC}")
    for d in disks:
        print(f"  {d['device']} ({d['serial']}): {GREEN if d['result'] == 'SUCCESSFUL' else RED}{d['result']}{NC}")


def give_to_user(*paths):
    """Under sudo, hand the files to the calling user. As plain root (live stick) there is nothing to do,
    and exFAT has no owners (chown fails there and is ignored)."""
    if "SUDO_UID" not in os.environ:
        return
    for f in paths:
        if f.exists():
            with contextlib.suppress(OSError):
                os.chown(f, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))


def load_defaults():
    if not CONFIG.exists():
        return {"language": "de"}
    try:
        # utf-8-sig: Windows Notepad likes to put a BOM in front
        return parse_config(CONFIG.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        die(f"{CONFIG} is invalid: {e}")


def preflight():
    if os.geteuid() != 0:
        die("Please run with 'sudo'.")
    # Live stick: without the REPORTS partition, reports end up in RAM and are gone after power-off
    if os.environ.get("ERASE_KIOSK") and not os.path.ismount(DATA):
        die(f"The REPORTS partition of the stick is not mounted ({DATA}).\nWas the stick created with live/make-stick.sh?")
    for cmd in ("lsblk", "smartctl", "hexdump"):
        need(cmd)
    try:
        import playwright  # noqa: F401
    except ImportError:
        die("Python package 'playwright' missing: pip install -r requirements.txt && playwright install chromium")


def run_all(disks, meta, prompt=ask_continue, quiet=False, on_status=lambda d: None):
    """Erase all drives; afterwards the result is in d["result"]. on_status reports every change."""
    def set_result(d, result):
        d["result"] = result
        on_status(d)

    # Sort out frozen drives first, after that everything runs without intervention
    for d in disks:
        d["result"] = None
        if d["method"] == "ata":
            try:
                d["device"] = unfreeze(d["device"], d["serial"], prompt)
            except Abort as e:
                print(f"{RED}Error: {e}{NC}")
                set_result(d, "SKIPPED (frozen, no report)")

    REPORTS.mkdir(exist_ok=True)
    give_to_user(REPORTS)

    def erase_one(d, quiet=False):
        # Measuring points for progress_of(), set before "RUNNING" so the GUI has them on its first poll
        d.update(started=time.time(), io_start=io_bytes(d["device"]),
                 ata_minutes=erase_minutes(run(["hdparm", "-I", d["device"]]).stdout) if d["method"] == "ata" else None)
        set_result(d, "RUNNING")
        print(f"\n{YELLOW}===== Drive {d['device']} ({d['serial']}) ====={NC}")
        try:
            # Guard against renaming (hot-plug, standby): never erase a drive other than the confirmed one
            if d["serial"] and lsblk_info(d["device"])["serial"].strip() != d["serial"]:
                die(f"{d['device']} no longer has serial number {d['serial']}.")
            set_result(d, erase_disk(d, meta, quiet))
        except Abort as e:
            print(f"{RED}Error on {d['device']}: {e}{NC}")
            set_result(d, "ABORTED (no report)")

    todo = [d for d in disks if d["result"] is None]
    # ATA/NVMe take minutes and run one after another, nwipe takes hours and runs in parallel
    for d in todo:
        if d["method"] != "nwipe":
            erase_one(d)
    wipes = [d for d in todo if d["method"] == "nwipe"]
    if len(wipes) > 1:
        print(f"\n{YELLOW}Starting {len(wipes)} nwipe runs in parallel. Progress: tail -f reports/nwipe_*.log{NC}")
    with ThreadPoolExecutor(max(len(wipes), 1)) as pool:
        list(pool.map(lambda d: erase_one(d, quiet or len(wipes) > 1), wipes))


def erase_disk(d, shared_meta, quiet=False):
    """Erase one drive and write its report. Returns the result as UI text."""
    device, info, serial, method = d["device"], d["info"], d["serial"], d["method"]
    drive_kind, nist, _, _ = METHODS[method]
    meta = dict(shared_meta)
    if d["comment"]:
        meta["comment"] = d["comment"]
    lang = meta.get("language", "de")
    t = REPORT_TEXT[lang]
    size = int(info["size"])

    print(f"{YELLOW}Collecting drive information...{NC}")
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

    result = "SUCCESSFUL" if ok else "FAILED"
    print(f"{GREEN if ok else RED}{device}: erasure {result}.{NC}")

    def translate(value):
        """REPORT_TEXT code -> text; (code, arg) -> formatted text; anything else unchanged."""
        if isinstance(value, tuple):
            return t[value[0]].format(code=value[1])
        return t.get(value, value) if isinstance(value, str) else value

    meta.update({
        "title": t["title"].format(kind=drive_kind),
        "subtitle": t["subtitle"].format(method=translate(fields["erase_method"]), nist=nist),
        "author": meta["biz_contact"],
        "date": format_date(end, lang),
        "device": device, "drive_model": info["model"].strip(), "drive_serial": serial,
        "bus_type": info["tran"].upper(), "drive_size_bytes": size, "drive_size_gb": size_gb(size),
        "hpa_dco_status": " ".join(hpa) or t["hpa_none"],
        "start_time": start.strftime(t["timestamp"]),
        "end_time": end.strftime(t["timestamp"]),
        "duration": hms(seconds),
        "rounds": t["rounds_firmware"], "prng": t["na"], "errors": t["na"], "throughput": t["na"],
        "bytes_erased": size,
        "ok": ok,
    })
    meta.update({k: translate(v) for k, v in fields.items()})
    if method == "nwipe" and seconds > 0:
        meta["throughput"] = t["throughput_verify"].format(mbs=size // seconds // 1000000)

    step(f"Writing report for {device}")
    appendices = [
        (t["appx_smart_before"], smart_before),
        (t["appx_smart_after"], run(["smartctl", "-x", device]).stdout),
        (t["appx_hexdump"], run(["hexdump", "-n", str(HEXDUMP_BYTES), "-C", device]).stdout),
        *[(t[title], t["no_log"] if text is None else text) for title, text in extra],
    ]
    base = f"erase_report_{re.sub(r'[^A-Za-z0-9_-]', '', serial) or 'unknown'}_{stamp}"
    html_file, pdf = REPORTS / (base + ".html"), REPORTS / (base + ".pdf")
    signature = SIGNATURE.read_bytes() if SIGNATURE.exists() else None
    logo = LOGO.read_bytes() if LOGO.exists() else None
    html_file.write_text(build_html(meta, appendices, signature, logo), encoding="utf-8")
    try:
        render_pdf(html_file, pdf, f"{meta['date']} | {meta['drive_model']} | {serial}")
        print(f"{GREEN}PDF report written: {pdf}{NC}")
    except Exception as e:  # noqa: BLE001 - the HTML report is kept in any case
        print(f"{YELLOW}PDF creation failed ({e}). HTML report: {html_file}{NC}")

    give_to_user(html_file, pdf, nwipe_log)
    os.sync()  # live stick (exFAT, no journal): report safely on the stick even if it is pulled right away
    return result


if __name__ == "__main__":
    try:
        main()
    except Abort as e:
        print(f"{RED}Error: {e}{NC}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{RED}Aborted (Ctrl+C).{NC}")
        sys.exit(130)
