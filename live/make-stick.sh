#!/bin/sh
# Writes the live image to a USB stick and adds the REPORTS partition (exFAT) behind it.
#
#   sudo live/make-stick.sh live/tabularasa.iso /dev/sdX            new stick: EVERYTHING on it is erased
#   sudo live/make-stick.sh --update live/tabularasa.iso /dev/sdX   only renew the system, reports + settings stay
#
# Layout: system (ISO) at the start, REPORTS partition fixed at 4 GiB. An update only overwrites the area
# before it and then re-adds the partition entry, so its content is left untouched.
set -eu
START_MIB=4096

update=no
[ "${1:-}" = "--update" ] && { update=yes; shift; }
[ $# = 2 ] || { sed -n '2,7p' "$0"; exit 1; }
iso=$1 dev=$2
[ "$(id -u)" = 0 ] || { echo "Please run with sudo."; exit 1; }
[ -f "$iso" ] || { echo "$iso not found."; exit 1; }
[ "$(lsblk -dno TYPE "$dev" 2>/dev/null)" = disk ] || { echo "$dev is not a whole drive."; exit 1; }
if lsblk -no MOUNTPOINT "$dev" | grep -q .; then
	echo "$dev has mounted partitions, please unmount them first:"; lsblk "$dev"; exit 1
fi
[ "$(stat -c %s "$iso")" -lt $((START_MIB * 1024 * 1024)) ] || { echo "Image is larger than ${START_MIB} MiB."; exit 1; }
[ "$(lsblk -dbno SIZE "$dev")" -gt $(((START_MIB + 256) * 1024 * 1024)) ] || { echo "Stick too small (min. 5 GB)."; exit 1; }

lsblk -o NAME,SIZE,TRAN,MODEL,LABEL "$dev"
[ "$(lsblk -dno TRAN "$dev")" = usb ] || echo "WARNING: $dev is NOT a USB device!"
if [ $update = yes ]; then
	lsblk -lno LABEL "$dev" | grep -qx REPORTS || { echo "No REPORTS partition on $dev, create the stick without --update."; exit 1; }
	echo "The system on $dev will be renewed, the REPORTS partition is kept."
else
	echo "ALL DATA on $dev will be erased."
fi
printf "Type the device name to confirm (%s): " "$dev"
read -r answer
[ "$answer" = "$dev" ] || { echo "Aborted."; exit 1; }

dd if="$iso" of="$dev" bs=4M conv=fsync status=progress

# The ISO brings its own partition table (MBR or GPT). Add the REPORTS partition behind it.
sfdisk --relocate gpt-bak-std "$dev" >/dev/null 2>&1 || true   # GPT only: move the backup header to the end of the stick
if sfdisk -d "$dev" | grep -q '^label: gpt'; then
	type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7   # Basic Data (readable by Windows/macOS)
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
	# Take your own settings from the repo folder (not in git), otherwise the template
	repo=$(dirname "$0")/..
	if [ -f "$repo/config.toml" ]; then cp "$repo/config.toml" "$tmp/"; else cp "$repo/config.example.toml" "$tmp/config.toml"; fi
	for f in logo.png signature.png; do
		if [ -f "$repo/$f" ]; then cp "$repo/$f" "$tmp/"; fi
	done
	mkdir "$tmp/reports"
	umount "$tmp"
	rmdir "$tmp"
	echo "Done."
	[ -f "$repo/config.toml" ] || echo "Note: config.toml on REPORTS is only the template, please adjust it."
	for f in logo.png signature.png; do
		[ -f "$repo/$f" ] || echo "Note: $f is missing, copy it onto REPORTS if needed."
	done
else
	lsblk -lno LABEL "$part" | grep -qx REPORTS || { echo "ERROR: $part is not the REPORTS partition!"; exit 1; }
	echo "Done. System renewed, REPORTS partition $part unchanged."
fi
sync
