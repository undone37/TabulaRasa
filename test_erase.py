import importlib.util
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import erase

# Real 'hdparm -I' output (shortened): positive lines only indented, negative ones with "not"
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
    meta["ok"] = True
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
        self.assertEqual(erase.format_date(datetime(2026, 3, 5), "de"), "05. März 2026")
        self.assertEqual(erase.format_date(datetime(2026, 3, 5), "en"), "5 March 2026")

    def test_find_by_serial(self):
        out = ('{"blockdevices": [{"path": "/dev/sda", "type": "disk", "serial": "AAA"},'
               '{"path": "/dev/sdb", "type": "disk", "serial": "TEST0000SN01 "},'
               '{"path": "/dev/sr0", "type": "rom", "serial": "TEST0000SN01"},'
               '{"path": "/dev/sdc", "type": "disk", "serial": null}]}')
        self.assertEqual(erase.find_by_serial(out, "TEST0000SN01"), "/dev/sdb")
        self.assertIsNone(erase.find_by_serial(out, "MISSING"))
        self.assertIsNone(erase.find_by_serial(out, ""))  # an empty serial would otherwise match sdc
        twice = out.replace('"AAA"', '"TEST0000SN01"')
        self.assertIsNone(erase.find_by_serial(twice, "TEST0000SN01"))  # ambiguous: rather none at all

    def test_config(self):
        text = Path(erase.HERE, "config.example.toml").read_text(encoding="utf-8")
        d = erase.parse_config(text + 'unknown = "x"\n')
        self.assertEqual(d["biz_name"], "Example Ltd")
        self.assertEqual(d["place"], "Exampletown")
        self.assertEqual(d["language"], "en")
        self.assertNotIn("unknown", d)
        self.assertEqual(erase.parse_config(""), {"language": "de"})
        with self.assertRaisesRegex(ValueError, "supported: de, en"):
            erase.parse_config('language = "fr"')

    def test_place_and_date(self):
        page = erase.build_html(sample_meta(place="Greven", date="30. Oktober 2026", language="de"), [])
        self.assertIn("Greven, den 30. Oktober 2026", page)
        page = erase.build_html(sample_meta(place="Greven", date="30 October 2026", language="en"), [])
        self.assertIn("Greven, 30 October 2026", page)
        self.assertNotIn(", den", erase.build_html(sample_meta(), []))  # without a place only the date

    def test_report_languages(self):
        de, en = erase.REPORT_TEXT["de"], erase.REPORT_TEXT["en"]
        self.assertEqual(de.keys(), en.keys())  # every text exists in every language
        for lang, foreign in (("de", en), ("en", de)):
            page = erase.build_html(sample_meta(language=lang, comment="x"), [])
            self.assertIn(f'<html lang="{lang}">', page)
            self.assertIn(erase.REPORT_TEXT[lang]["cert_title"], page)
            self.assertNotIn(foreign["cert_title"], page)
            self.assertNotIn(foreign["section_customer"], page)
        self.assertIn("SUCCESSFUL", erase.build_html(sample_meta(language="en"), []))
        self.assertIn("FEHLGESCHLAGEN", erase.build_html(sample_meta(language="de", ok=False), []))
        template = erase.TEMPLATE.read_text(encoding="utf-8")
        for key in re.findall(r"\$\{t_(\w+)\}", template):
            self.assertIn(key, de)

    def test_erase_minutes(self):
        self.assertEqual(erase.erase_minutes(HDPARM_NOT_ENABLED), 2)
        both = HDPARM_ENABLED_FROZEN + "\t4min for SECURITY ERASE UNIT. 6min for ENHANCED SECURITY ERASE UNIT.\n"
        self.assertEqual(erase.erase_minutes(both), 6)  # enhanced supported -> enhanced estimate
        self.assertIsNone(erase.erase_minutes("Security:\n\t\tsupported\n"))

    def test_progress(self):
        now, gb = datetime(2026, 10, 30, 12, 0), 10**9
        # half written after 100 s: 25 % overall, 5 MB/s, 1.5 GB left -> 300 s
        frac, text = erase.progress("nwipe", gb, 100, (0, gb // 2), now=now)
        self.assertEqual(frac, 0.25)
        self.assertEqual(text, "25% writing · 5 MB/s · 00:05:00 left · done ~12:05")
        frac, text = erase.progress("nwipe", gb, 300, (gb // 2, gb), now=now)
        self.assertEqual(frac, 0.75)
        self.assertTrue(text.startswith("75% verifying"))
        frac, _ = erase.progress("ata", gb, 60, ata_minutes=2, now=now)
        self.assertEqual(frac, 0.5)
        frac, text = erase.progress("ata", gb, 999, ata_minutes=2, now=now)  # overrun: stays at 99 %
        self.assertEqual(frac, 0.99)
        self.assertIn("00:00:00 left", text)
        _, text = erase.progress("nwipe", gb, 100, (0, 10**6), now=now)  # very slow: finishes another day
        self.assertRegex(text, r"done ~\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(erase.progress("nvme", gb, 10), (None, ""))
        self.assertEqual(erase.progress("nwipe", gb, 10, None), (None, ""))  # no counters: animation

    def test_parse_disks_marks_mounted(self):
        out = ('{"blockdevices": ['
               '{"path": "/dev/sda", "type": "disk", "serial": "A", "mountpoint": null, "children": ['
               '{"path": "/dev/sda1", "type": "part", "mountpoint": null, "children": ['
               '{"path": "/dev/mapper/root", "type": "crypt", "mountpoint": "/"}]}]},'
               '{"path": "/dev/sdb", "type": "disk", "serial": null, "mountpoint": null},'
               '{"path": "/dev/sr0", "type": "rom", "serial": "C", "mountpoint": null}]}')
        disks = erase.parse_disks(out)
        self.assertEqual([d["path"] for d in disks], ["/dev/sda", "/dev/sdb"])
        self.assertTrue(disks[0]["mounted"])  # deeply nested (LUKS) counts too
        self.assertFalse(disks[1]["mounted"])
        self.assertEqual(disks[1]["serial"], "")

    def test_html_escapes_and_fills_everything(self):
        page = erase.build_html(
            sample_meta(cust_name="Müller & Söhne <GmbH>", comment="Room 3", ok=False),
            [("Log", "a < b\n")], b"\x89PNG")
        self.assertIn("Müller &amp; Söhne &lt;GmbH&gt;", page)
        self.assertIn("a &lt; b", page)
        self.assertIn("Room 3", page)
        self.assertIn('class="v status fail"', page)
        self.assertIn("data:image/png;base64,", page)
        self.assertNotIn("${", page)

    def test_html_without_comment_or_signature(self):
        page = erase.build_html(sample_meta(), [])
        self.assertNotIn(erase.REPORT_TEXT["de"]["section_comment"], page)
        self.assertNotIn("<img", page)

    def test_missing_field_fails_loudly(self):
        meta = sample_meta()
        del meta["errors"]
        with self.assertRaises(KeyError):
            erase.build_html(meta, [])


@unittest.skipUnless(importlib.util.find_spec("playwright"), "playwright not installed")
class Pdf(unittest.TestCase):
    def test_render_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            html_file, pdf = Path(d, "r.html"), Path(d, "r.pdf")
            html_file.write_text(erase.build_html(sample_meta(), [("Hexdump", "00000000  00 00\n*\n")]),
                                 encoding="utf-8")
            erase.render_pdf(html_file, pdf, "30 October 2026 | MODEL | SERIAL")
            self.assertEqual(pdf.read_bytes()[:5], b"%PDF-")


if __name__ == "__main__":
    unittest.main()
