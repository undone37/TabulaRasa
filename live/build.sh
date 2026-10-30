#!/bin/sh
# Builds the live image live/tabularasa.iso. Requires: sudo apt install live-build
# Usage: sudo live/build.sh
set -eu
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "Please run with sudo."; exit 1; }

# Program from the repo into the image (only what it needs to run, nothing company-specific)
app=config/includes.chroot/opt/tabularasa
rm -rf "$app"
mkdir -p "$app"
cp ../erase.py ../gui.py ../report.html ../requirements.txt ../config.example.toml "$app/"

lb clean
lb config
lb build
mv tabularasa-amd64.hybrid.iso tabularasa.iso
echo "Done: $(pwd)/tabularasa.iso"
