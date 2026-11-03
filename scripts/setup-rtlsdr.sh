#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo 'Mit sudo ausführen.' >&2; exit 1; }
id meshcore-ai >/dev/null 2>&1 || { echo 'Zuerst install-pi.sh ausführen.' >&2; exit 1; }
apt-get update
apt-get install -y rtl-sdr librtlsdr0
cat > /etc/udev/rules.d/70-meshcore-rtlsdr.rules <<'RULES'
SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2832", GROUP="meshcore-ai", MODE="0660"
SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2838", GROUP="meshcore-ai", MODE="0660"
RULES
udevadm control --reload-rules
printf '%s\n' 'RTL-SDR-Treiber installiert. Stick erneut einstecken.' 'Empfangstest: sudo -u meshcore-ai rtl_test -d 0 -s 2048000' 'Ein ASA-fähiger Decoderadapter muss separat eingerichtet werden. Kein Funkdienst wurde gestartet.'
