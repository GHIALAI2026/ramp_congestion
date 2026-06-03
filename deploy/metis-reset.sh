#!/usr/bin/env bash
# ==========================================================================
# Metis NPU device reset — root-owned privileged helper.
# ==========================================================================
# This is the ONLY privileged operation start.sh needs for hardware recovery.
# It is invoked through a single narrow sudoers rule
# (deploy/axelera-metis-nopasswd.in) and takes NO arguments, so the caller
# cannot influence which device is touched (security observation #8).
#
# Scope is strictly the Axelera Metis NPU: it only removes/rescans PCI devices
# currently bound to the `metis` kernel driver and reloads that module. It
# never writes to a path derived from user input. This replaces the previous
# approach, which granted the application user passwordless sudo on general
# tools (tee/lsof/modprobe) and could remove ANY PCI device.
#
# Must be installed root-owned and NOT writable by the application user:
#   sudo install -o root -g root -m 0755 deploy/metis-reset.sh \
#       /usr/local/sbin/metis-reset.sh
# ==========================================================================
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

DRIVER_DIR=/sys/bus/pci/drivers/metis

# 1. Free any processes still holding the Metis device nodes.
if ls /dev/metis* >/dev/null 2>&1; then
    fuser -k -9 /dev/metis* 2>/dev/null || true
fi
sleep 1

# 2. Remove ONLY the PCI devices currently bound to the metis driver.
#    The glob matches PCI BDF directories (e.g. 0000:01:00.0) and skips the
#    driver's control files (bind, unbind, uevent, ...).
if [[ -d "$DRIVER_DIR" ]]; then
    for dev in "$DRIVER_DIR"/[0-9a-fA-F]*:*; do
        [[ -e "$dev/remove" ]] || continue
        echo 1 > "$dev/remove" 2>/dev/null || true
    done
fi
sleep 1

# 3. Reload the kernel module.
modprobe -r metis 2>/dev/null || true
modprobe metis 2>/dev/null || true

# 4. Rescan the PCI bus so the Metis device re-enumerates and rebinds cleanly.
echo 1 > /sys/bus/pci/rescan 2>/dev/null || true

exit 0
