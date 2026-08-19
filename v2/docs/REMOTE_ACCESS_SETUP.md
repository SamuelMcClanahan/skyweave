# Remote Access Setup — Tailscale Exit Node + Custom DERP Relay

Goal: a proper, self-hosted remote-access path so Samuel no longer needs Cloudflare WARP,
and can reach the home/rig tailnet (and route internet through home) from a restrictive
school network that blocks direct UDP/P2P and likely the default DERP.

Two pieces:

1. **Exit node** on the always-on home Jetson (`skyweave-jetson`, `100.98.123.55`) — lets the
   school laptop route all traffic through home.
2. **Custom DERP relay** on a public host listening on **TCP 443** — gives Tailscale a relay
   the school firewall can't tell apart from normal HTTPS, so the tunnel comes up even when
   UDP/P2P and the default DERP are blocked.

Owner tags used below: **[APPLIED]** = already done in this session on the Jetson.
**[SAMUEL]** = your action (admin console, cloud billing, DNS, or a disruptive restart).

---

## 0. Current state (verified 2026-08-18)

| Item | Value |
|---|---|
| Tailnet owner | porkbuns1964@gmail.com |
| Exit-node host | `skyweave-jetson` = `100.98.123.55` (Jetson Orin Nano, Ubuntu, Linux 5.15.185-tegra aarch64) |
| Jetson internet NIC | `wlP1p1s0` (Wi-Fi) `192.168.4.25/22`, default gw `192.168.4.1`, home public IP `192.195.83.227` |
| Jetson rig NIC | `eno1` `192.168.10.110/24` (rig LAN) |
| Tailscale version | 1.102.2 |
| Mac (main session) | `100.73.148.114` — **do not touch its Tailscale state while the main session is live** |
| netcheck baseline (from home) | UDP: yes, IPv4/IPv6: yes, PortMapping: UPnP, nearest DERP: Seattle 33 ms |

---

## PART A — Exit node on the Jetson

### A.1 What I already applied on the Jetson  [APPLIED]

All three are additive and did **not** restart `tailscaled`; Jetson reachability was
re-verified after each.

1. **IPv4/IPv6 forwarding** — persisted and applied:
   - File `/etc/sysctl.d/99-tailscale.conf`:
     ```
     net.ipv4.ip_forward = 1
     net.ipv6.conf.all.forwarding = 1
     ```
   - Applied with `sudo sysctl -p /etc/sysctl.d/99-tailscale.conf`. Runtime now `= 1 / = 1`.

2. **UDP GRO forwarding tweak** (Tailscale-recommended throughput fix for exit/subnet nodes),
   on the internet-facing NIC:
   ```
   sudo ethtool -K wlP1p1s0 rx-udp-gro-forwarding on rx-gro-list off
   ```
   Verified: `rx-udp-gro-forwarding: on`.
   > This is a runtime setting and resets on reboot. To persist, see A.5.

3. **Advertised the exit node** (used `tailscale set`, not `tailscale up`, so no other prefs reset):
   ```
   sudo tailscale set --advertise-exit-node
   ```
   Verified: prefs `AdvertiseRoutes: ["0.0.0.0/0","::/0"]`.
   `Self.ExitNodeOption` is still **false** — that is expected; it flips to true only after
   you approve the routes in the admin console (step A.3).

### A.2 CRITICAL — firewall-mode fix so the exit node actually NATs  [SAMUEL — requires a tailscaled restart]

**Why this is needed.** On this Jetson the exit node will *advertise* but will **not forward
traffic** until this is fixed. Diagnosis from this session:

- `tailscale status` shows a persistent health warning:
  `adding [-j ts-input] in filter/INPUT: ... RULE_INSERT failed (No such file or directory)`.
- Root cause: **UFW owns the base netfilter chains via `iptables-legacy`**, while Tailscale
  defaults to the **`iptables-nft`** backend (system `update-alternatives` value is
  `/usr/sbin/iptables-nft`). Tailscale's `ts-input` / `ts-forward` / `ts-postrouting` chains
  exist orphaned in the nft tables with **no jump from the base chains and no MASQUERADE rule**
  (`nft list chain ip filter INPUT` → "No such file or directory"). Classic legacy/nft
  split-brain, so Tailscale cannot install its forward/masquerade rules.
- Good news: **UFW is currently inactive** at runtime (legacy `FORWARD`/`nat POSTROUTING`
  policies are `ACCEPT`), so nothing is dropping forwarded packets — the only blocker is that
  Tailscale can't program its NAT. Fixing the backend resolves it.

Pick **one** fix. Both need a `tailscaled` restart, which briefly (~a few seconds) drops the
`tailscale0` tunnel — so run it when the **main Claude session is not mid-operation through the
Jetson**, or just let that session reconnect.

**Fix A (recommended — surgical, fully reversible): force Tailscale to nftables-native mode.**
```
# add the env var to the file tailscaled already sources
echo 'TS_DEBUG_FIREWALL_MODE=nftables' | sudo tee -a /etc/default/tailscaled
sudo systemctl restart tailscaled
```
In nftables mode Tailscale creates its **own** nft table with proper base-chain hooks, so it
never fights UFW's legacy chains. To undo: delete that line from `/etc/default/tailscaled` and
restart.

**Fix B (alternative): unify the whole system on the legacy backend** (matches where the base
chains already live and where UFW is):
```
sudo update-alternatives --set iptables  /usr/sbin/iptables-legacy
sudo update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
sudo systemctl restart tailscaled
```
Note: this is system-wide (also affects Docker, which is installed but inactive, and any future
UFW rules). Reversible with `--set ... /usr/sbin/iptables-nft`.

**After the restart, confirm the NAT rules now install** (should be non-empty once the routes
are also approved in A.3 — re-check after approval):
```
sudo tailscale status                              # health warning should be gone
# Fix A (nftables mode) — look for Tailscale's own table + a masquerade rule:
sudo nft list ruleset | grep -iE 'ts-|masquerade|table ip ts'
# Fix B (legacy) — the jumps land in the legacy chains:
sudo iptables-legacy -t nat -S POSTROUTING | grep -i ts-postrouting
sudo iptables-legacy -t nat -S ts-postrouting | grep -i MASQUERADE
```

> If you ever run `ufw enable` on the Jetson, you must also allow the exit-node forwarding,
> because UFW defaults to `DEFAULT_FORWARD_POLICY="DROP"`:
> ```
> sudo ufw route allow in on tailscale0 out on wlP1p1s0
> sudo ufw allow in on tailscale0
> # and set DEFAULT_FORWARD_POLICY="ACCEPT" in /etc/default/ufw, then: sudo ufw reload
> ```
> Leaving UFW inactive (current state) also works.

### A.3 Approve the exit node in the admin console  [SAMUEL]

The Jetson is advertising `0.0.0.0/0` and `::/0`, but Tailscale hides exit nodes until the
routes are approved.

1. Go to **https://login.tailscale.com/admin/machines**.
2. Find **skyweave-jetson** → row menu (**⋯**) → **Edit route settings**.
3. Toggle **Use as exit node** ON (approves both the IPv4 and IPv6 default routes) → **Save**.

After saving, `tailscale status` on any peer will list `skyweave-jetson` with
`offers exit node`, and `Self.ExitNodeOption` becomes true.

### A.4 (Optional) keep the Jetson's own internet stable

The Jetson uses Tailscale key-expiry like any node. For an always-on relay/exit box you may
want to **disable key expiry** for `skyweave-jetson` in the admin console (Machines → ⋯ →
Disable key expiry) so it never drops off the tailnet mid-semester. [SAMUEL]

### A.5 (Optional) persist the UDP GRO tweak across reboots  [SAMUEL]

The `ethtool` setting is runtime-only. To reapply on every boot, create a
`networkd-dispatcher` hook (Tailscale's documented method):
```
sudo tee /etc/networkd-dispatcher/routable.d/50-tailscale >/dev/null <<'SH'
#!/bin/sh
ethtool -K wlP1p1s0 rx-udp-gro-forwarding on rx-gro-list off || true
SH
sudo chmod 755 /etc/networkd-dispatcher/routable.d/50-tailscale
```
(If `networkd-dispatcher` isn't installed, a small `systemd` oneshot unit or an `@reboot`
root cron entry running the same `ethtool` line works too.)

---

## PART B — Custom DERP relay (TCP 443)

### B.1 Host recommendation

Requirements for a DERP relay: an **always-on** public host with a **stable public IP**, a
**DNS name**, a **TLS cert**, listening on **TCP 443** (this is the part that beats the school
firewall) and ideally **UDP 3478** (STUN).

| Option | Verdict |
|---|---|
| **Restart the existing Azure "gate" VM (eastus) and run derper on it** | **Not recommended as the permanent relay.** That VM is the authoritative test-suite gate and is kept *deallocated* to save money; a relay must be up 24/7 whenever you're at school, which fights that posture, adds cost, and couples relay uptime to the gate. Fine only as a short-term stopgap. Do **not** repurpose it destructively. |
| **Small dedicated always-on VM (recommended)** | Cleanest. Isolated from the gate. Azure `Standard_B1s` (~$8–10/mo) or `B1ls` (~$4/mo). Or **GCP `e2-micro`** in a free-tier region (`us-west1`/`us-central1`/`us-east1`) which is **$0** within the always-free monthly limit — ideal for a hobby relay. derper is tiny (a few MB RAM). |
| GCP `skyweave-sim` project | Good home for the `e2-micro` above; you already have the project. |

**Recommendation:** a dedicated **GCP `e2-micro`** (free-tier region) in `skyweave-sim`, or an
Azure **`B1s`** if you prefer to stay on Azure. Keep it separate from the gate VM.

> **Quick win to try first (free, 5 min):** the *default* Tailscale DERP servers already listen
> on **443/TCP**. Before deploying anything, at school run `tailscale netcheck` and
> `tailscale ping skyweave-jetson`. If a default DERP shows a latency and ping succeeds "via
> DERP", you may not need a custom relay at all. You need the custom DERP only if the school does
> SNI/domain allow-listing or TLS inspection that blocks `*.tailscale.com` / the default DERP
> IPs. Everything below is that fallback.

### B.2 Provision the VM  [SAMUEL — billing/infra]

Do not let me start billable cloud resources; run these yourself.

**GCP option** (from `skyweave-sim`):
```
gcloud compute instances create skyweave-derp \
  --project=skyweave-sim \
  --zone=us-west1-b \
  --machine-type=e2-micro \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --address='' \                      # or attach a reserved static IP (recommended)
  --tags=derp
# Reserve a static IP so the DNS record never breaks:
gcloud compute addresses create skyweave-derp-ip --project=skyweave-sim --region=us-west1
```

**Azure option:**
```
az vm create -g <your-rg> -n skyweave-derp \
  --image Ubuntu2404 --size Standard_B1s \
  --public-ip-sku Standard --public-ip-address-allocation static \
  --admin-username samuel --generate-ssh-keys
```

Record the VM's **public IPv4** (and IPv6 if assigned) — call it `<DERP_IPV4>` / `<DERP_IPV6>`.

### B.3 DNS  [SAMUEL]

Create an **A** record (and **AAAA** if you have IPv6) for a hostname you control, pointing at
the VM's public IP. The hostname must be real and resolvable — derper's Let's Encrypt cert is
issued for it.

```
derp.<your-domain>        A     <DERP_IPV4>
derp.<your-domain>        AAAA  <DERP_IPV6>   (optional)
```

No domain? A free `*.duckdns.org` A record works fine with Let's Encrypt. Use whatever hostname
you pick consistently below as `<DERP_HOST>` (e.g. `derp.skyweave.systems` or
`skyweave-derp.duckdns.org`).

> Tip: pick a hostname on a domain the **school is unlikely to block**. Avoid `*.tailscale.com`
> (that's the point of self-hosting the relay).

### B.4 Install Go and build `derper` on the VM  [SAMUEL]

There is no official prebuilt derper binary; you build it with Go (one command).
```
# on the derper VM
sudo apt-get update && sudo apt-get install -y curl
# install a current Go toolchain
curl -fsSL https://go.dev/dl/go1.23.6.linux-amd64.tar.gz -o /tmp/go.tgz
sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz
export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin
go version

# build derper
go install tailscale.com/cmd/derper@latest
sudo install -m0755 "$HOME/go/bin/derper" /usr/local/bin/derper
sudo mkdir -p /var/lib/derper/certs
```
(For an Azure `arm64` VM, use the `linux-arm64` Go tarball instead.)

### B.5 systemd unit for derper  [SAMUEL]

Create `/etc/systemd/system/derper.service`. Replace `<DERP_HOST>`.
```
[Unit]
Description=Tailscale DERP relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/derper \
  -hostname <DERP_HOST> \
  -a :443 \
  -http-port -1 \
  -stun \
  -stun-port 3478 \
  -certmode letsencrypt \
  -certdir /var/lib/derper/certs
# Allow binding 443/3478 without running as full root:
AmbientCapabilities=CAP_NET_BIND_SERVICE
DynamicUser=yes
StateDirectory=derper
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Flag notes:
- `-a :443` — DERP over **TCP 443** (the school-firewall-beating listener).
- `-http-port -1` — disables the plaintext HTTP listener; Let's Encrypt uses TLS-ALPN-01 over
  the 443 listener, so port 80 is not required.
- `-stun -stun-port 3478` — STUN for NAT traversal (helps when you're *not* on the locked-down
  school net; the school likely blocks UDP so STUN won't help there, but it's harmless and
  useful elsewhere).
- `-certmode letsencrypt` + `-certdir` — auto-provisions/renews a real cert for `<DERP_HOST>`.

If you used `DynamicUser=yes`, point `-certdir` at the `StateDirectory` instead:
`-certdir /var/lib/derper` (owned by the dynamic user). Either works as long as the dir is
writable by the service user.

Enable it:
```
sudo systemctl daemon-reload
sudo systemctl enable --now derper
sudo systemctl status derper --no-pager
journalctl -u derper -n 50 --no-pager        # watch the cert get issued
```

### B.6 Open the ports  [SAMUEL]

**Cloud firewall / security group:** allow inbound **443/tcp** and **3478/udp** from anywhere.

GCP:
```
gcloud compute firewall-rules create derp-allow \
  --project=skyweave-sim --direction=INGRESS --action=ALLOW \
  --rules=tcp:443,udp:3478 --target-tags=derp --source-ranges=0.0.0.0/0
```
Azure NSG:
```
az network nsg rule create -g <your-rg> --nsg-name <nsg> -n derp-tcp443 \
  --priority 1001 --access Allow --protocol Tcp --destination-port-ranges 443 --direction Inbound
az network nsg rule create -g <your-rg> --nsg-name <nsg> -n derp-udp3478 \
  --priority 1002 --access Allow --protocol Udp --destination-port-ranges 3478 --direction Inbound
```
**OS firewall:** if UFW is enabled on the VM: `sudo ufw allow 443/tcp && sudo ufw allow 3478/udp`.

> This exposes a public port. It's the intended, outward-facing step — trigger it yourself.
> Harden against open-relay abuse with B.7.

### B.7 (Recommended) restrict the relay to your tailnet  [SAMUEL]

Without this, anyone can use your DERP as an open relay. To lock it to your tailnet, run
Tailscale on the DERP VM and add `-verify-clients` to the unit:
```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up        # (join skyweave-derp to the tailnet; approve/authorize in console)
# then add this flag to ExecStart in derper.service and restart:
#   -verify-clients
sudo systemctl daemon-reload && sudo systemctl restart derper
```
`-verify-clients` makes derper ask the local `tailscaled` whether each connecting client is in
your tailnet, and reject the rest.

### B.8 Register the custom DERP in the tailnet policy  [SAMUEL — paste in admin console]

Admin console → **Access Controls** (the tailnet policy file / ACL editor). Add this top-level
`derpMap` key (merge it alongside your existing `acls` etc.). Replace `<DERP_HOST>`,
`<DERP_IPV4>`, and `<DERP_IPV6>` (drop the IPv6 line if you have none).

```json
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
          "HostName": "<DERP_HOST>",
          "IPv4":     "<DERP_IPV4>",
          "IPv6":     "<DERP_IPV6>",
          "DERPPort": 443,
          "STUNPort": 3478
        }
      ]
    }
  }
}
```
- `"OmitDefaultRegions": false` keeps Tailscale's default DERP servers as **fallback**, so you
  don't lose connectivity if your relay is down.
- Custom region IDs must be **≥ 900** (Tailscale reserves the lower numbers).
- `HostName` must match the cert / DNS name from B.3–B.5.

Save the policy. Within a minute all nodes pick up the new region.

---

## PART C — Using it from the school laptop  [SAMUEL, at school]

> These run on the **Mac**. Do **not** run them while a Claude session on this Mac is depending
> on Tailscale — they change the Mac's Tailscale state. Do it when you're the one at the keyboard
> at school.

1. **Turn WARP off** (it full-tunnels `utun`/`172.16.0.2` and breaks Tailscale's control
   connection). Make sure Tailscale is running: `tailscale status`.
2. Confirm the relay is reachable and the tunnel forms over it:
   ```
   tailscale netcheck            # expect UDP: false at school, but region "skyweave" shows a latency
   tailscale ping skyweave-jetson   # expect: pong ... via DERP(skyweave)
   ```
3. Route your internet through home (the exit node):
   ```
   tailscale set --exit-node=skyweave-jetson --exit-node-allow-lan-access=true
   ```
   Undo when back on a normal network:
   ```
   tailscale set --exit-node=
   ```

---

## Verification checklist / expected output

**On the Jetson (after A.2 fix + A.3 approval):**
```
sudo tailscale status
#  -> no "RULE_INSERT failed" health warning
#  -> Self line shows "offers exit node"
# NAT rule present (Fix A / nftables mode):
sudo nft list ruleset | grep -i masquerade        # a masquerade rule referencing tailscale0
# or (Fix B / legacy):
sudo iptables-legacy -t nat -S ts-postrouting | grep -i MASQUERADE
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding    # both = 1
```

**On the DERP VM:**
```
systemctl status derper                # active (running)
journalctl -u derper | grep -i cert    # cert issued for <DERP_HOST>
curl -sI https://<DERP_HOST>/          # TLS handshake succeeds (real cert), derper responds
```

**From any tailnet node once derpMap is applied:**
```
tailscale netcheck
#  DERP latency list includes:  - skyweave: XXms  (SkyWeave DERP)
```

**From the school network (the real test):**
```
tailscale netcheck            # UDP: false (school blocks UDP) BUT "skyweave" region has a latency
tailscale ping skyweave-jetson    # "pong ... via DERP(skyweave)"  <- proves 443/TCP relay works
# then set the exit node (Part C) and confirm your public IP is the home IP (192.195.83.227):
curl -4 ifconfig.me
```
Success = tunnel comes up with UDP disabled (relayed over your 443 DERP), you can reach the rig
tailnet, and with the exit node set your egress IP is the home IP.

---

## Rollback / undo

- **Un-advertise exit node:** `sudo tailscale set --advertise-exit-node=false` on the Jetson,
  and untoggle the route in the admin console.
- **Undo firewall-mode change:** remove the `TS_DEBUG_FIREWALL_MODE=nftables` line from
  `/etc/default/tailscaled` (Fix A) or `update-alternatives --set iptables /usr/sbin/iptables-nft`
  (Fix B); `sudo systemctl restart tailscaled`.
- **Disable forwarding:** `sudo rm /etc/sysctl.d/99-tailscale.conf` and reboot (or
  `sudo sysctl -w net.ipv4.ip_forward=0 net.ipv6.conf.all.forwarding=0`).
- **Tear down DERP:** `sudo systemctl disable --now derper`; delete the VM and firewall rule;
  remove the `derpMap` block from the policy file.

---

## Quick reference — ownership summary

| Step | Who | Status |
|---|---|---|
| IPv4/IPv6 forwarding (sysctl.d) | me | **APPLIED** |
| UDP GRO ethtool tweak | me | **APPLIED** (runtime; persist via A.5) |
| `tailscale set --advertise-exit-node` | me | **APPLIED** |
| Firewall-mode fix + tailscaled restart (A.2) | Samuel | pending (disruptive restart) |
| Approve exit node in admin console (A.3) | Samuel | pending |
| Provision DERP VM + DNS (B.2–B.3) | Samuel | pending (billing/infra) |
| Build derper + systemd + open ports (B.4–B.6) | Samuel | pending |
| Paste `derpMap` policy (B.8) | Samuel | pending |
| From-school test (Part C) | Samuel | pending |
