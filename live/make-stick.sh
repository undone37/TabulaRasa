#!/bin/sh
# Schreibt das Live-Image auf einen USB-Stick und legt dahinter die Reports-Partition (exFAT, "REPORTS") an.
#
#   sudo live/make-stick.sh live/tabularasa.iso /dev/sdX            neuer Stick: ALLES auf dem Stick wird gelöscht
#   sudo live/make-stick.sh --update live/tabularasa.iso /dev/sdX   nur das System erneuern, Reports + Einstellungen bleiben
#
# Aufbau: System (ISO) am Anfang, Reports-Partition fest ab 4 GiB. Beim Update wird nur der Bereich davor
# überschrieben und die Partition danach wieder eingetragen, ihr Inhalt bleibt unberührt.
set -eu
START_MIB=4096

update=no
[ "${1:-}" = "--update" ] && { update=yes; shift; }
[ $# = 2 ] || { sed -n '2,7p' "$0"; exit 1; }
iso=$1 dev=$2
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo ausführen."; exit 1; }
[ -f "$iso" ] || { echo "$iso nicht gefunden."; exit 1; }
[ "$(lsblk -dno TYPE "$dev" 2>/dev/null)" = disk ] || { echo "$dev ist kein ganzes Laufwerk."; exit 1; }
if lsblk -no MOUNTPOINT "$dev" | grep -q .; then
	echo "$dev hat eingehängte Partitionen, bitte erst aushängen:"; lsblk "$dev"; exit 1
fi
[ "$(stat -c %s "$iso")" -lt $((START_MIB * 1024 * 1024)) ] || { echo "Image ist größer als ${START_MIB} MiB."; exit 1; }
[ "$(lsblk -dbno SIZE "$dev")" -gt $(((START_MIB + 256) * 1024 * 1024)) ] || { echo "Stick zu klein (mind. 5 GB)."; exit 1; }

lsblk -o NAME,SIZE,TRAN,MODEL,LABEL "$dev"
[ "$(lsblk -dno TRAN "$dev")" = usb ] || echo "WARNUNG: $dev ist KEIN USB-Gerät!"
if [ $update = yes ]; then
	lsblk -lno LABEL "$dev" | grep -qx REPORTS || { echo "Keine REPORTS-Partition auf $dev, ohne --update neu anlegen."; exit 1; }
	echo "System auf $dev wird erneuert, die Reports-Partition bleibt erhalten."
else
	echo "ALLE DATEN auf $dev werden gelöscht."
fi
printf "Zum Bestätigen den Gerätenamen eingeben (%s): " "$dev"
read -r answer
[ "$answer" = "$dev" ] || { echo "Abgebrochen."; exit 1; }

dd if="$iso" of="$dev" bs=4M conv=fsync status=progress

# Das ISO bringt seine eigene Partitionstabelle mit (MBR oder GPT). Reports-Partition dahinter eintragen.
sfdisk --relocate gpt-bak-std "$dev" >/dev/null 2>&1 || true   # nur bei GPT nötig: Backup-Header ans Stick-Ende
if sfdisk -d "$dev" | grep -q '^label: gpt'; then
	type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7   # Basic Data (Windows/macOS lesen sie)
else
	type=7                                      # exFAT/NTFS
fi
echo "${START_MIB}MiB,,$type" | sfdisk --append --no-reread "$dev"
partprobe "$dev" 2>/dev/null || blockdev --rereadpt "$dev"
udevadm settle
part=$(lsblk -lnpo PATH,TYPE "$dev" | awk '$2 == "part" { p = $1 } END { print p }')

if [ $update = no ]; then
	mkfs.exfat -L REPORTS "$part"
	tmp=$(mktemp -d)
	mount "$part" "$tmp"
	# Eigene Einstellungen aus dem Repo-Ordner mitnehmen (nicht im Git), sonst die Vorlage
	repo=$(dirname "$0")/..
	if [ -f "$repo/config.toml" ]; then cp "$repo/config.toml" "$tmp/"; else cp "$repo/config.example.toml" "$tmp/config.toml"; fi
	for f in logo.png unterschrift.png; do
		if [ -f "$repo/$f" ]; then cp "$repo/$f" "$tmp/"; fi
	done
	mkdir "$tmp/reports"
	umount "$tmp"
	rmdir "$tmp"
	echo "Fertig."
	[ -f "$repo/config.toml" ] || echo "Hinweis: config.toml auf REPORTS ist nur die Vorlage, bitte anpassen."
	for f in logo.png unterschrift.png; do
		[ -f "$repo/$f" ] || echo "Hinweis: $f fehlt, bei Bedarf auf REPORTS kopieren."
	done
else
	lsblk -lno LABEL "$part" | grep -qx REPORTS || { echo "FEHLER: $part ist nicht die REPORTS-Partition!"; exit 1; }
	echo "Fertig. System erneuert, Reports-Partition $part unverändert."
fi
sync
