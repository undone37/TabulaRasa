# TabulaRasa

Löscht HDDs, SATA-SSDs und NVMe-SSDs und erstellt einen PDF-Report (Deckblatt, Zertifikat, Anhänge).

| Laufwerk | Methode | NIST 800-88 |
|---|---|---|
| HDD | `nwipe`: 1× Nullen + Verifizierung | Clear |
| SATA-SSD | ATA Secure Erase (Enhanced, falls unterstützt) | Purge |
| NVMe-SSD | `nvme format --ses=1` | Purge |

## Einrichtung (Live-System)

```sh
sudo apt install smartmontools hdparm nwipe nvme-cli bsdextrautils python3-venv python3-tk imagemagick fonts-comfortaa fonts-quicksand
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m playwright install --with-deps chromium
```

## Datenordner

Alles Firmenspezifische liegt in einem Ordner, standardmäßig neben `erase.py`, auf dem Live-Stick auf der Reports-Partition (Umgebungsvariable `ERASE_HOME`):

| Datei | Inhalt | fehlt sie |
|---|---|---|
| `config.toml` | Vorbelegung Firma + Ort, Vorlage: `config.example.toml` | Felder leer |
| `logo.png` | Logo für Report und Fenster | ohne Logo |
| `unterschrift.png` | Unterschrift (nicht ins Repo) | ohne Unterschrift |
| `reports/` | erzeugte Reports und nwipe-Logs | wird angelegt |

```sh
cp config.example.toml config.toml
```

Die Fensterversion verkleinert das Logo mit ImageMagick (`apt install imagemagick`), ohne wirkt es pixelig.

## Benutzung

```sh
sudo .venv/bin/python gui.py     # Fenster
sudo .venv/bin/python erase.py   # Terminal
```

Startet das Fenster unter Wayland nicht (`Kein Fenster möglich`), einmalig als normaler Benutzer `xhost +SI:localuser:root` ausführen.

Mehrere Laufwerke auf einmal: Gerätenamen mit Leerzeichen getrennt angeben (`/dev/sda /dev/sdb`). Methode und Bemerkung werden je Laufwerk abgefragt. ATA/NVMe laufen nacheinander, mehrere nwipe-Läufe parallel (Fortschritt dann in `reports/nwipe_*.log`).

Je Laufwerk ein Report: `erase_report_<Seriennummer>_<Zeitstempel>.pdf` (+ `.html`, bei nwipe auch das Log) im Ordner `reports/` des Datenordners.

## Live-Stick

Bootfähiger USB-Stick (Debian 13), der direkt in die Oberfläche startet (Kiosk mit Cage, kein Desktop). Bootet mit BIOS, UEFI und Secure Boot.

```sh
sudo apt install live-build
sudo live/build.sh                                          # -> live/tabularasa.iso
sudo live/make-stick.sh live/tabularasa.iso /dev/sdX        # neuer Stick, löscht ALLES darauf
sudo live/make-stick.sh --update live/tabularasa.iso /dev/sdX   # nur System erneuern
```

Der Stick bekommt ab 4 GiB eine exFAT-Partition `REPORTS` (unter Windows/macOS lesbar). Sie ist der Datenordner: dort `config.toml` anpassen und `unterschrift.png` (optional `logo.png`) ablegen, die Reports landen in `reports/`. `--update` überschreibt nur den Bereich davor. Wird ein Update unterbrochen, bootet der Stick erst nach erneutem Update wieder, die Reports bleiben erhalten.

Auf dem Stick: Zeitzone Europe/Berlin, die Hardware-Uhr gilt als Ortszeit (wie bei Windows-Rechnern), deutsche Tastatur. Ausschalten über den Knopf in der Oberfläche oder den Netzschalter.

## Tests

```sh
.venv/bin/python -m unittest -v
```

## Lizenz

GPL-3.0, siehe [LICENSE](LICENSE).
