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
        "hpa_dco_status", "errors", "standard_text")}
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
        for standard in ("bsi", "bsi-high"):  # BSI: random data for every drive type
            self.assertEqual(erase.pick_method("nvme", False, standard), "random")
            self.assertEqual(erase.pick_method("sata", "1", standard), "random")

    def test_standards(self):
        self.assertEqual(erase.standard_text("ata", "nist"), "NIST SP 800-88 Rev. 1, Purge")
        self.assertEqual(erase.standard_text("nwipe", "nist"), "NIST SP 800-88 Rev. 1, Clear")
        self.assertEqual(erase.standard_text("random", "bsi-high", "de"),
                         "BSI IT-Grundschutz M 2.167 / M 2.433, Schutzbedarf hoch")
        self.assertEqual(erase.STANDARDS["bsi"]["rounds"], 1)
        self.assertEqual(erase.STANDARDS["bsi-high"]["rounds"], 3)  # M 2.433: "three are better"
        for std in erase.STANDARDS.values():
            self.assertTrue(set(std["methods"]) <= set(erase.METHODS))

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
        self.assertEqual(d["standard"], "nist")
        self.assertEqual(erase.parse_config(""), {"language": "de", "gui_language": "en", "standard": "nist"})
        self.assertEqual(erase.parse_config('gui_language = "de"')["gui_language"], "de")
        with self.assertRaisesRegex(ValueError, "gui_language"):
            erase.parse_config('gui_language = "fr"')
        self.assertEqual(erase.parse_config('standard = "bsi-high"')["standard"], "bsi-high")
        with self.assertRaisesRegex(ValueError, "supported: de, en"):
            erase.parse_config('language = "fr"')
        with self.assertRaisesRegex(ValueError, "supported: nist, bsi, bsi-high"):
            erase.parse_config('standard = "dod"')

    def test_place_and_date(self):
        page = erase.build_html(sample_meta(place="Exampletown", date="30. Oktober 2026", language="de"), [])
        self.assertIn("Exampletown, den 30. Oktober 2026", page)
        page = erase.build_html(sample_meta(place="Exampletown", date="30 October 2026", language="en"), [])
        self.assertIn("Exampletown, 30 October 2026", page)
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

    def test_gui_texts_complete(self):
        import string

        import gui
        en, de = gui.UI_TEXT["en"], gui.UI_TEXT["de"]
        self.assertEqual(en.keys(), de.keys())
        for key, value in en.items():
            if isinstance(value, dict):
                self.assertEqual(value.keys(), de[key].keys(), key)
            elif isinstance(value, list):
                self.assertEqual([len(r) for r in value] if isinstance(value[0], tuple) else len(value),
                                 [len(r) for r in de[key]] if isinstance(de[key][0], tuple) else len(de[key]), key)
            else:  # same {placeholders} in both languages
                fields = lambda s: sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)
                self.assertEqual(fields(value), fields(de[key]), key)
        self.assertEqual(en["fields"].keys(), {k for k, _ in erase.META_FIELDS})
        self.assertEqual(en["std_labels"].keys(), erase.STANDARDS.keys())
        self.assertEqual(en["method_warn"].keys(), erase.METHODS.keys())
        self.assertEqual(en["progress"].keys(), de["progress"].keys())

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
        # BSI high: 3 random passes + verify = 4x the drive size
        frac, text = erase.progress("random", gb, 100, (0, gb // 2), now=now, rounds=3)
        self.assertEqual(frac, 0.125)
        self.assertTrue(text.startswith("12% writing pass 1/3"))
        frac, text = erase.progress("random", gb, 100, (0, 2 * gb + gb // 2), now=now, rounds=3)
        self.assertTrue(text.startswith("62% writing pass 3/3"))
        frac, text = erase.progress("random", gb, 100, (gb // 2, 3 * gb), now=now, rounds=3)
        self.assertEqual(frac, 0.875)
        self.assertTrue(text.startswith("88% verifying"))
        words = {"writing": "schreibt", "writing_pass": "schreibt Durchgang {n}/{rounds}", "verifying": "prüft",
                 "eta": "noch {left} · fertig ca. {end}"}
        _, text = erase.progress("random", gb, 100, (0, gb // 2), now=now, rounds=3, words=words)
        self.assertEqual(text, "12% schreibt Durchgang 1/3 · 5 MB/s · noch 00:11:40 · fertig ca. 12:11")

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
