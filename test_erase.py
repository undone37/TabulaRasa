import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import erase

# Echte 'hdparm -I'-Ausgabe (gekürzt): positive Zeilen nur eingerückt, negative mit "not"
HDPARM_NOT_ENABLED = """\
/dev/sda:

ATA device, with non-removable media
Security:
\tMaster password revision code = 65534
\t\tsupported
\tnot\tenabled
\tnot\tlocked
\tnot\tfrozen
\tnot\texpired: security count
\tnot\tsupported: enhanced erase
\t2min for SECURITY ERASE UNIT.
Logical Unit WWN Device Identifier: 5002538e40000000
"""

HDPARM_ENABLED_FROZEN = """\
Security:
\t\tsupported
\t\tenabled
\tnot\tlocked
\t\tfrozen
\t\tsupported: enhanced erase

"""


def sample_meta(**overrides):
    meta = {k: "-" for k in (
        "title", "subtitle", "author", "date", "biz_name", "biz_address", "biz_contact", "biz_phone",
        "cust_name", "cust_address", "cust_contact", "cust_phone", "drive_model", "drive_serial",
        "drive_size_gb", "drive_size_bytes", "bus_type", "start_time", "end_time", "duration",
        "erase_method", "prng", "rounds", "verification", "bytes_erased", "throughput",
        "hpa_dco_status", "errors")}
    meta["final_result"] = "ERFOLGREICH"
    meta.update(overrides)
    return meta


class Logic(unittest.TestCase):
    def test_pick_method(self):
        self.assertEqual(erase.pick_method("nvme", False), "nvme")
        self.assertEqual(erase.pick_method("sata", True), "nwipe")
        self.assertEqual(erase.pick_method("sata", "1"), "nwipe")
        self.assertEqual(erase.pick_method("sata", "0"), "ata")
        self.assertEqual(erase.pick_method("sata", False), "ata")
        self.assertEqual(erase.pick_method("", "false"), "ata")

    def test_parse_security_negative_lines(self):
        s = erase.parse_security(HDPARM_NOT_ENABLED)
        self.assertEqual(s, {"supported": True, "enabled": False, "frozen": False, "enhanced": False})

    def test_parse_security_positive_lines(self):
        s = erase.parse_security(HDPARM_ENABLED_FROZEN)
        self.assertEqual(s, {"supported": True, "enabled": True, "frozen": True, "enhanced": True})

    def test_parse_security_missing_block(self):
        self.assertFalse(erase.parse_security("/dev/sdb:\n USB bridge, nothing here\n")["supported"])

    def test_formatting(self):
        self.assertEqual(erase.size_gb(160041885696), "160.04 GB")
        self.assertEqual(erase.hms(3725), "01:02:05")
        self.assertEqual(erase.de_date(datetime(2026, 3, 5)), "05. März 2026")

    def test_find_by_serial(self):
        out = ('{"blockdevices": [{"path": "/dev/sda", "type": "disk", "serial": "AAA"},'
               '{"path": "/dev/sdb", "type": "disk", "serial": "TEST0000SN01 "},'
               '{"path": "/dev/sr0", "type": "rom", "serial": "TEST0000SN01"},'
               '{"path": "/dev/sdc", "type": "disk", "serial": null}]}')
        self.assertEqual(erase.find_by_serial(out, "TEST0000SN01"), "/dev/sdb")
        self.assertIsNone(erase.find_by_serial(out, "FEHLT"))
        self.assertIsNone(erase.find_by_serial(out, ""))  # leere Seriennummer passt sonst auf sdc
        doppelt = out.replace('"AAA"', '"TEST0000SN01"')
        self.assertIsNone(erase.find_by_serial(doppelt, "TEST0000SN01"))  # mehrdeutig: lieber gar keins

    def test_config(self):
        text = Path(erase.HERE, "config.example.toml").read_text(encoding="utf-8")
        d = erase.parse_config(text + 'unbekannt = "x"\n')
        self.assertEqual(d["biz_name"], "Musterfirma GmbH")
        self.assertEqual(d["place"], "Musterstadt")
        self.assertNotIn("unbekannt", d)
        self.assertEqual(erase.parse_config(""), {})
        page = erase.build_html(sample_meta(place="Musterstadt", date="30. Oktober 2026"), [])
        self.assertIn("Musterstadt, den 30. Oktober 2026", page)
        self.assertNotIn(", den", erase.build_html(sample_meta(), []))  # ohne Ort nur das Datum

    def test_erase_minutes(self):
        self.assertEqual(erase.erase_minutes(HDPARM_NOT_ENABLED), 2)
        both = HDPARM_ENABLED_FROZEN + "\t4min for SECURITY ERASE UNIT. 6min for ENHANCED SECURITY ERASE UNIT.\n"
        self.assertEqual(erase.erase_minutes(both), 6)  # enhanced unterstützt -> enhanced-Angabe
        self.assertIsNone(erase.erase_minutes("Security:\n\t\tsupported\n"))

    def test_progress(self):
        now, gb = datetime(2026, 10, 30, 12, 0), 10**9
        # halb geschrieben nach 100 s: 25 % gesamt, 5 MB/s, Rest 1,5 GB -> 300 s
        frac, text = erase.progress("nwipe", gb, 100, (0, gb // 2), now=now)
        self.assertEqual(frac, 0.25)
        self.assertEqual(text, "25% schreibt · 5 MB/s · noch 00:05:00 · fertig ca. 12:05")
        frac, text = erase.progress("nwipe", gb, 300, (gb // 2, gb), now=now)
        self.assertEqual(frac, 0.75)
        self.assertTrue(text.startswith("75% prüft"))
        frac, _ = erase.progress("ata", gb, 60, ata_minutes=2, now=now)
        self.assertEqual(frac, 0.5)
        frac, text = erase.progress("ata", gb, 999, ata_minutes=2, now=now)  # überzogen: bleibt bei 99 %
        self.assertEqual(frac, 0.99)
        self.assertIn("noch 00:00:00", text)
        _, text = erase.progress("nwipe", gb, 100, (0, 10**6), now=now)  # sehr langsam: endet erst morgen
        self.assertRegex(text, r"fertig ca\. \d\d\.\d\d\. \d\d:\d\d$")
        self.assertEqual(erase.progress("nvme", gb, 10), (None, ""))
        self.assertEqual(erase.progress("nwipe", gb, 10, None), (None, ""))  # keine Zähler: Animation

    def test_parse_disks_marks_mounted(self):
        out = ('{"blockdevices": ['
               '{"path": "/dev/sda", "type": "disk", "serial": "A", "mountpoint": null, "children": ['
               '{"path": "/dev/sda1", "type": "part", "mountpoint": null, "children": ['
               '{"path": "/dev/mapper/root", "type": "crypt", "mountpoint": "/"}]}]},'
               '{"path": "/dev/sdb", "type": "disk", "serial": null, "mountpoint": null},'
               '{"path": "/dev/sr0", "type": "rom", "serial": "C", "mountpoint": null}]}')
        disks = erase.parse_disks(out)
        self.assertEqual([d["path"] for d in disks], ["/dev/sda", "/dev/sdb"])
        self.assertTrue(disks[0]["mounted"])  # tief verschachtelt (LUKS) zählt auch
        self.assertFalse(disks[1]["mounted"])
        self.assertEqual(disks[1]["serial"], "")

    def test_html_escapes_and_fills_everything(self):
        page = erase.build_html(
            sample_meta(cust_name="Müller & Söhne <GmbH>", comment="Raum 3", final_result="FEHLGESCHLAGEN"),
            [("Log", "a < b\n")], b"\x89PNG")
        self.assertIn("Müller &amp; Söhne &lt;GmbH&gt;", page)
        self.assertIn("a &lt; b", page)
        self.assertIn("Raum 3", page)
        self.assertIn('class="v status fail"', page)
        self.assertIn("data:image/png;base64,", page)
        self.assertNotIn("${", page)

    def test_html_without_comment_or_signature(self):
        page = erase.build_html(sample_meta(), [])
        self.assertNotIn("Bemerkung", page)
        self.assertNotIn("<img", page)

    def test_missing_field_fails_loudly(self):
        meta = sample_meta()
        del meta["errors"]
        with self.assertRaises(KeyError):
            erase.build_html(meta, [])


@unittest.skipUnless(importlib.util.find_spec("playwright"), "playwright nicht installiert")
class Pdf(unittest.TestCase):
    def test_render_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            html_file, pdf = Path(d, "r.html"), Path(d, "r.pdf")
            html_file.write_text(erase.build_html(sample_meta(), [("Hexdump", "00000000  00 00\n*\n")]),
                                 encoding="utf-8")
            erase.render_pdf(html_file, pdf, "30. Oktober 2026 | MODEL | SERIAL")
            self.assertEqual(pdf.read_bytes()[:5], b"%PDF-")


if __name__ == "__main__":
    unittest.main()
