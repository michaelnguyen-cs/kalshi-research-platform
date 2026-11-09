# Migrating kbot to Oracle Cloud (Always Free tier)

**Why**: the Windows scheduled-task watchdog (`kbot-watchdog` + `kbot-monitor`,
see `scripts/install-task.ps1` / `scripts/install-monitor-task.ps1`) is a
mitigation, not a fix — it depends on the laptop being powered on, not
asleep for more than a few minutes at a stretch, and Windows Task Scheduler
behaving itself. A small always-on Linux VM removes the whole class of
"the trading process depends on a laptop's power/sleep state" problems.
Oracle Cloud's Always Free tier includes an ARM VM (`VM.Standard.A1.Flex`,
up to 4 OCPU / 24 GB RAM) that's genuinely free forever (not a 12-month
trial) — no ongoing cost for this workload.

This is a from-scratch walkthrough. Budget under 2 hours; most of that is
waiting for account verification and VM provisioning, not typing.

---

## 1. Create an Oracle Cloud account

1. Go to https://signup.oraclecloud.com and sign up. You'll need a phone
   number and a credit card for identity verification — **Always Free
   resources are never billed**, but Oracle requires a card on file to
   deter abuse.
2. Pick a Home Region carefully during signup — **this cannot be changed
   later** without a new account. Pick whichever region is geographically
   closest to you (lower latency to Kalshi's API, which is US-hosted, so a
   US region is the safe default if you're US-based).
3. Wait for the "Account provisioning" email (can take a few minutes to a
   few hours). Log in to the OCI Console once it arrives:
   https://cloud.oracle.com

---

## 2. Provision the VM

In the OCI Console:

1. **Compute → Instances → Create Instance**.
2. Name: `kbot-prod` (or whatever you like).
3. **Image and shape**:
   - Click "Edit" under Image and select **Canonical Ubuntu 22.04** (Minimal
     or standard — standard is fine).
   - Click "Edit" under Shape, switch to the **Ampere (Arm-based)** family,
     pick **VM.Standard.A1.Flex**. Set **1 OCPU / 6 GB memory** — comfortably
     inside the Always Free allowance (up to 4 OCPU / 24 GB total across all
     A1 instances) and far more than kbot needs.
4. **Networking**: leave the default VCN/subnet (Oracle creates one
   automatically on first VM if you don't have one). Make sure **"Assign a
   public IPv4 address"** is checked.
5. **Add SSH keys**: select "Generate a key pair for me" and **download both
   the private and public key immediately** — Oracle only shows them once.
   Save the private key somewhere safe, e.g. `~/.ssh/<SSH_KEY_FILE>` on
   your own machine (or `C:\Users\<you>\.ssh\<SSH_KEY_FILE>` on Windows).
6. Leave boot volume at the default (50 GB is the Always Free allotment,
   plenty).
7. Click **Create**. Provisioning takes 1-3 minutes. Note the **Public IP
   address** once it's "Running" — call it `<VM_IP>` below.

### Lock down the private key permissions (from your own machine)

```bash
chmod 400 ~/.ssh/<SSH_KEY_FILE>
```

On Windows (PowerShell), OpenSSH is more lenient but if you get a
"permissions too open" error:

```powershell
icacls "$env:USERPROFILE\.ssh\<SSH_KEY_FILE>" /inheritance:r
icacls "$env:USERPROFILE\.ssh\<SSH_KEY_FILE>" /grant:r "$env:USERNAME:R"
```

### First SSH connection

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP>
```

(Ubuntu cloud images default to the `ubuntu` user with passwordless sudo.)

---

## 3. Open the firewall (both Oracle's Security List AND the VM's own iptables)

Oracle Cloud VMs have **two** firewall layers — missing either one blocks
traffic. kbot itself is outbound-only (it calls Kalshi's API/WS and FRED;
it doesn't need any inbound port open except SSH), so the rules below are
deliberately minimal: **SSH in, everything else out**.

### 3a. Security List (OCI Console)

1. **Networking → Virtual Cloud Networks → (your VCN) → Security Lists →
   Default Security List**.
2. Confirm there's an **Ingress Rule** for `0.0.0.0/0`, TCP, destination
   port `22` (SSH) — this exists by default. **Do not open any other
   inbound port**; kbot doesn't listen for inbound connections.
3. Confirm the default **Egress Rule** allows all outbound traffic
   (`0.0.0.0/0`, all protocols) — needed for Kalshi's API/WS, FRED, Google
   Sheets, and any alert webhooks (Discord/Telegram).

### 3b. The VM's own firewall (iptables/netfilter, via Oracle's cloud-init)

Ubuntu images on OCI ship with `iptables` rules that only allow SSH inbound
by default already — matching the Security List. Verify:

```bash
sudo iptables -L -n | head -20
```

You should see `ACCEPT` for `tcp dpt:22` and otherwise a permissive or
default-accept policy for outbound. No changes needed for kbot's outbound-
only traffic pattern. If you later want `ufw` for convenience:

```bash
sudo apt-get install -y ufw
sudo ufw allow OpenSSH
sudo ufw enable
```

---

## 4. Python 3.11 setup

Ubuntu 22.04 ships Python 3.10 by default; kbot requires `>=3.11`
(`pyproject.toml`). Install 3.11 via the deadsnakes PPA:

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev git
```

Verify:

```bash
python3.11 --version   # Python 3.11.x
```

---

## 5. Copy the repo and secrets to the VM

Run these from your **local machine** (not the VM), replacing `<VM_IP>`.

### Option A: clone from your own git remote (recommended if you have one)

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP> \
  "git clone <your-repo-url> /home/ubuntu/kalshi-bot"
```

### Option B: copy the local working tree directly via scp

```bash
# From the kbot project root on your machine:
scp -i ~/.ssh/<SSH_KEY_FILE> -r . ubuntu@<VM_IP>:/home/ubuntu/kalshi-bot
```

(This copies `data/`, `src/`, `tests/`, etc. `.gitignore`d files like `.env`,
`*.db`, and `*.pem` won't be excluded by `scp -r` the way `git clone` would
exclude them — that's fine here since we WANT the secrets copied, see next
step — but do NOT push this same directory to a public git remote as-is.)

### Copy secrets individually (safer — smaller, explicit, auditable transfer)

Whichever option you used above, copy the credential files **explicitly**
so you know exactly what secret material left your machine:

```bash
# Kalshi RSA private key
scp -i ~/.ssh/<SSH_KEY_FILE> kalshi_private_key.pem ubuntu@<VM_IP>:/home/ubuntu/kalshi-bot/

# Google Sheets service account (if using sheets-sync)
scp -i ~/.ssh/<SSH_KEY_FILE> service_account.json ubuntu@<VM_IP>:/home/ubuntu/kalshi-bot/

# Your .env (copy .env.example -> .env locally first if you haven't, fill in real values, THEN copy)
scp -i ~/.ssh/<SSH_KEY_FILE> .env ubuntu@<VM_IP>:/home/ubuntu/kalshi-bot/
```

Lock down permissions on the VM (secrets should not be world-readable):

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP>
chmod 600 /home/ubuntu/kalshi-bot/kalshi_private_key.pem
chmod 600 /home/ubuntu/kalshi-bot/service_account.json
chmod 600 /home/ubuntu/kalshi-bot/.env
```

**Double-check `.env` on the VM before going further**:
- `KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem` (relative to the
  working directory the systemd service will use — see step 7).
- `DRY_RUN=true` — keep this exactly as it was on your Windows box. Cloud
  migration is an infrastructure change, not a decision to go live; don't
  flip `DRY_RUN` as part of this move.
- `KALSHI_ENV=demo` (or whatever it already was).

---

## 6. Install kbot on the VM

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP>
cd /home/ubuntu/kalshi-bot

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Verify the install and credentials:

```bash
kbot markets --limit 5        # no credentials needed -- sanity-checks the API is reachable
kbot balance                  # exercises auth (RSA-PSS signing) end-to-end
```

Run the test suite before trusting this environment with the live process:

```bash
pytest -q      # expect the same pass count as on Windows
```

---

## 7. systemd service (replaces watchdog.ps1 + the scheduled tasks entirely)

systemd's `Restart=always` + `WantedBy=multi-user.target` covers everything
the Windows watchdog/monitor pair existed to work around: it restarts kbot
on crash, and it starts on boot — no "AtLogon doesn't fire after sleep"
problem exists on a server that doesn't sleep.

Create `/etc/systemd/system/kbot.service`:

```ini
[Unit]
Description=kbot paper trading (multi-strategy runner)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kalshi-bot
Environment=PATH=/home/ubuntu/kalshi-bot/.venv/bin:/usr/bin:/bin
ExecStart=/home/ubuntu/kalshi-bot/.venv/bin/kbot paper --strategies all
Restart=always
RestartSec=30
# Analogous to the Windows task's ExecutionTimeLimit=PT0S -- no runtime cap.
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

(If you're still single-strategy, use
`ExecStart=/home/ubuntu/kalshi-bot/.venv/bin/kbot paper --strategy btc_hourly --series KXBTC`
instead — matches whatever your Windows watchdog.ps1 was invoking.)

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable kbot.service
sudo systemctl start kbot.service
```

Check it's actually up:

```bash
sudo systemctl status kbot.service
journalctl -u kbot.service -f      # live-tail logs, Ctrl-C to stop tailing
```

`Restart=always` means a crash restarts within `RestartSec` (30s here,
matching `watchdog.ps1`'s `RestartCooldownSeconds` default) — indefinitely,
with no "watchdog-of-the-watchdog" needed, since systemd itself is a PID-1
managed service, not a second scheduled task that can independently fail.

### Logs

The `paper` command writes its own rotating log file at `logs/paper.log`
(relative to `WorkingDirectory`) via `kbot.log_config` — capped at 100 MB per
file, 7 rotated backups. Do **not** add a `StandardOutput=append:…/paper.log`
redirect to the unit: systemd would write that file unbounded, bypassing
rotation, which is exactly how `logs/` reached 23 GB on the Windows watchdog.
`journalctl -u kbot.service` remains available for stdout via journald.

---

## 8. Cron for sheets-sync

Add a cron job for the `ubuntu` user:

```bash
crontab -e
```

Add (runs every hour, on the hour — adjust to taste):

```cron
0 * * * * cd /home/ubuntu/kalshi-bot && /home/ubuntu/kalshi-bot/.venv/bin/kbot sheets-sync >> /home/ubuntu/kalshi-bot/logs/sheets-sync.log 2>&1
```

Make sure the log directory exists:

```bash
mkdir -p /home/ubuntu/kalshi-bot/logs
```

---

## 9. Checking status from anywhere

SSH in (from any machine with the private key):

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP>
cd /home/ubuntu/kalshi-bot && source .venv/bin/activate
kbot status
```

Or, without a full interactive session, one-shot from your local machine:

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP> \
  "cd /home/ubuntu/kalshi-bot && .venv/bin/kbot status"
```

Check the systemd service state directly (doesn't need the venv activated):

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP> "systemctl is-active kbot.service"
```

Tail live logs remotely:

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP> "journalctl -u kbot.service -n 100 -f"
```

Restart the service manually if you ever need to (e.g. after a config
change):

```bash
ssh -i ~/.ssh/<SSH_KEY_FILE> ubuntu@<VM_IP> "sudo systemctl restart kbot.service"
```

---

## 10. Decommissioning the Windows watchdog

Once the VM has been running cleanly for a few days and `kbot status` looks
healthy from the cloud side, retire the Windows scheduled tasks so you're
not running the bot in two places at once:

```powershell
Unregister-ScheduledTask -TaskName "kbot-watchdog" -Confirm:$false
Unregister-ScheduledTask -TaskName "kbot-monitor" -Confirm:$false
```

Stop any manually-started `watchdog.ps1` / `kbot paper` process on the
Windows box at the same time (check `Get-Process python`), so there is
exactly one copy of the strategy running against your account.
