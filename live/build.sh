#!/bin/sh
# Baut das Live-Image live/tabularasa.iso. Voraussetzung: sudo apt install live-build
# Aufruf: sudo live/build.sh
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo ausführen."; exit 1; }

# Programm aus dem Repo ins Image (nur was zum Laufen nötig ist, nichts Firmenspezifisches)
app=config/includes.chroot/opt/tabularasa
rm -rf "$app"
mkdir -p "$app"
cp ../erase.py ../gui.py ../report.html ../requirements.txt ../config.example.toml "$app/"

lb clean
lb config
lb build
mv tabularasa-amd64.hybrid.iso tabularasa.iso
echo "Fertig: $(pwd)/tabularasa.iso"
