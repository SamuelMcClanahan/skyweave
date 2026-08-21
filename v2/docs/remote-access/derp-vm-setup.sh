#!/usr/bin/env bash
# SkyWeave DERP relay + exit-node VM setup.
# Companion to v2/docs/REMOTE_ACCESS_SETUP.md §B — run ON the cloud VM as root/sudo.
#
# Usage:
#   sudo ./derp-vm-setup.sh install <DERP_HOST>   # Go + derper + systemd unit + sysctl + tailscale; prints tailnet auth URL
#   sudo ./derp-vm-setup.sh finish                # after the auth URL is approved: exit node + -verify-clients + restart
#   sudo ./derp-vm-setup.sh derpmap               # print the filled-in derpMap JSON for the tailnet policy (region 900)
#   sudo ./derp-vm-setup.sh status                # verification block
#
# Cloud firewall (NOT done here — do it in the cloud console/CLI): inbound 443/tcp + 3478/udp.
set -euo pipefail

ENVFILE=/etc/default/derper
UNIT=/etc/systemd/system/derper.service
GO_FALLBACK=go1.25.0

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"

arch() {
  case "$(uname -m)" in
    x86_64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) die "unsupported arch $(uname -m)" ;;
  esac
}

write_unit() {  # $1 = DERP_HOST, $2 = extra flags ("" or "-verify-clients")
  cat > "$UNIT" <<EOF
[Unit]
Description=Tailscale DERP relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/derper \\
  -hostname $1 \\
  -a :443 \\
  -http-port -1 \\
  -stun \\
  -stun-port 3478 \\
  -certmode letsencrypt \\
  -certdir /var/lib/derper $2
AmbientCapabilities=CAP_NET_BIND_SERVICE
DynamicUser=yes
StateDirectory=derper
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

cmd_install() {
  DERP_HOST="${1:-}"; [ -n "$DERP_HOST" ] || die "usage: install <DERP_HOST>"
  echo "DERP_HOST=$DERP_HOST" > "$ENVFILE"

  apt-get update -qq
  apt-get install -y -qq curl >/dev/null

  # Go toolchain (latest stable, pinned fallback)
  GOVER="$(curl -fsSL 'https://go.dev/VERSION?m=text' 2>/dev/null | head -1 || true)"
  [ -n "$GOVER" ] || GOVER="$GO_FALLBACK"
  echo "Installing $GOVER ($(arch))"
  curl -fsSL "https://go.dev/dl/${GOVER}.linux-$(arch).tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz && rm /tmp/go.tgz

  # Build derper
  export PATH=$PATH:/usr/local/go/bin GOPATH=/root/go
  go install tailscale.com/cmd/derper@latest
  install -m0755 /root/go/bin/derper /usr/local/bin/derper
  echo "derper built: $(/usr/local/bin/derper --version 2>&1 | head -1 || true)"

  # Unit (no -verify-clients yet — the VM isn't on the tailnet until 'finish')
  write_unit "$DERP_HOST" ""
  systemctl enable --now derper

  # IP forwarding for the exit-node role
  printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > /etc/sysctl.d/99-tailscale.conf
  sysctl -p /etc/sysctl.d/99-tailscale.conf

  # OS firewall (cloud firewall is separate — see header)
  if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow 443/tcp && ufw allow 3478/udp
  fi

  # Tailscale
  curl -fsSL https://tailscale.com/install.sh | sh
  echo
  echo "=== Joining the tailnet — approve this URL as porkbuns1964@gmail.com: ==="
  (tailscale up --hostname=skyweave-derp >/tmp/ts-up.log 2>&1 &)
  sleep 8
  grep -o 'https://login.tailscale.com/[a-zA-Z0-9/]*' /tmp/ts-up.log || cat /tmp/ts-up.log
  echo
  echo "After approving, run: sudo $0 finish"
}

cmd_finish() {
  [ -f "$ENVFILE" ] || die "run install first"
  . "$ENVFILE"
  tailscale status >/dev/null 2>&1 || die "tailscaled not logged in yet — approve the auth URL first"
  tailscale set --advertise-exit-node
  write_unit "$DERP_HOST" "-verify-clients"
  systemctl restart derper
  echo "Exit node advertised (needs admin-console route approval) and -verify-clients enabled."
  cmd_status
}

cmd_derpmap() {
  [ -f "$ENVFILE" ] || die "run install first"
  . "$ENVFILE"
  V4="$(curl -4 -fsS --max-time 10 ifconfig.me || die 'no public IPv4?')"
  V6="$(curl -6 -fsS --max-time 10 ifconfig.me 2>/dev/null || true)"
  IPV6LINE=""
  [ -n "$V6" ] && IPV6LINE="\"IPv6\":     \"$V6\","
  cat <<EOF
"derpMap": {
  "OmitDefaultRegions": false,
  "Regions": {
    "900": {
      "RegionID":   900,
      "RegionCode": "skyweave",
      "RegionName": "SkyWeave DERP",
      "Nodes": [
        {
          "Name":     "1",
          "RegionID": 900,
          "HostName": "$DERP_HOST",
          "IPv4":     "$V4",
          $IPV6LINE
          "DERPPort": 443,
          "STUNPort": 3478
        }
      ]
    }
  }
}
EOF
}

cmd_status() {
  . "$ENVFILE" 2>/dev/null || true
  systemctl --no-pager --lines=0 status derper || true
  echo "--- cert / TLS check:"
  curl -sSI --max-time 15 "https://${DERP_HOST:-localhost}/" | head -3 || echo "(TLS not up yet — cert issuance can take ~a minute; journalctl -u derper)"
  echo "--- forwarding:"
  sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
  echo "--- tailscale:"
  tailscale status 2>&1 | head -5 || true
}

case "${1:-}" in
  install) shift; cmd_install "$@" ;;
  finish)  cmd_finish ;;
  derpmap) cmd_derpmap ;;
  status)  cmd_status ;;
  *) die "usage: $0 {install <DERP_HOST>|finish|derpmap|status}" ;;
esac
