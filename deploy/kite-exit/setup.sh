#!/usr/bin/env bash
# deploy/kite-exit/setup.sh -- ST-007: turn a fresh Ubuntu 24.04 Lightsail VM into stratrad's static-IP exit.
# Run once as the default user in the Lightsail browser SSH window:   sudo bash setup.sh
# It asks for: the tagged Tailscale auth key (hidden input; skipped when the VM is already on the tailnet) and
# rkneo50q's Tailscale IP (from the homelab project). Safe to re-run.
# Result: a forward proxy (tinyproxy, port 8888) that accepts ONLY rkneo50q's Tailscale address and connects ONLY to
# api.kite.trade:443; host firewall (ufw) allowing 8888 only on tailscale0 and SSH; automatic security updates.
# Nothing secret stays on the box: the auth key is used once and not written to disk by this script.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
JOINED=no
if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
  JOINED=yes; echo "Tailscale already connected as $(tailscale ip -4) -- no auth key needed"
else
  read -rsp "Tailscale auth key (tag:kite-exit, single use): " TS_AUTHKEY; echo
fi
read -rp  "rkneo50q Tailscale IP (100.x.y.z): " RKNEO_TS_IP
[[ $RKNEO_TS_IP =~ ^100\.([0-9]{1,3}\.){2}[0-9]{1,3}$ ]] || { echo "not a Tailscale 100.x address"; exit 1; }

echo "== packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -yq upgrade
apt-get -yq install tinyproxy ufw unattended-upgrades curl
cat > /etc/apt/apt.conf.d/52stratrad-auto-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "21:00";
EOF
# 21:00 UTC = 02:30 IST: reboots, when an update needs one, happen far from market hours.
systemctl enable --now unattended-upgrades

if [[ $JOINED == no ]]; then
  echo "== Tailscale (joins as tag:kite-exit via the tagged key)"
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up --authkey="$TS_AUTHKEY" --hostname=stratrad-kite-exit --advertise-tags=tag:kite-exit --accept-routes=false
  unset TS_AUTHKEY
fi

echo "== tinyproxy: only rkneo50q, only api.kite.trade:443"
cat > /etc/tinyproxy/filter <<'EOF'
^api\.kite\.trade$
EOF
cat > /etc/tinyproxy/tinyproxy.conf <<EOF
User tinyproxy
Group tinyproxy
Port 8888
Timeout 60
LogLevel Notice
MaxClients 20
Allow ${RKNEO_TS_IP}
ConnectPort 443
DisableViaHeader Yes
Filter "/etc/tinyproxy/filter"
FilterType ere
FilterDefaultDeny Yes
FilterURLs No
EOF
systemctl restart tinyproxy
systemctl enable tinyproxy

echo "== host firewall: proxy only on tailscale0; SSH kept for Lightsail browser SSH and the tailnet"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 to any port 8888 proto tcp
ufw allow 22/tcp
ufw --force enable

echo "== status"
tailscale status | head -5
tailscale ip -4
systemctl is-active tinyproxy unattended-upgrades
ufw status verbose | sed -n '1,20p'
echo "public IPv4 seen by the internet: $(curl -4 -s https://api.ipify.org)"
echo "DONE"
