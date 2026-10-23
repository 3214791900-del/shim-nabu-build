#!/bin/bash
# Mount the freshly built root image read-write.
#
#   mount-rw.sh IMG MOUNTPOINT
#
# Linux's hfsplus driver mounts a volume it considers dirty read-only, even
# with `-o force`.  mkiosroot.sh unmounts and fsck's the image, but the image
# still comes up read-only often enough that the userland tweak step used to
# die with "Read-only file system" and silently lose the whole build.  Repair
# once and remount.  Run as root.
set -euo pipefail

img=$1
mnt=$2
[[ -n $img && -n $mnt ]] || { sed -n '2,4p' "$0"; exit 2; }

mkdir -p "$mnt"
mount -o loop,rw "$img" "$mnt" || true
if ! touch "$mnt/.rwtest" 2>/dev/null; then
	echo "mount-rw: $img came up read-only; fsck.hfsplus -f -y then remount" >&2
	umount "$mnt" || true
	fsck.hfsplus -f -y "$img" || true
	mount -o loop,rw "$img" "$mnt"
	touch "$mnt/.rwtest"
fi
rm -f "$mnt/.rwtest"
echo "mount-rw: $mnt is writable"
