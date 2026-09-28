# Always-on machine: Ubuntu Server on the home desktop

The home desktop (i7-4770K, 16 GB RAM, 1 TB SSD + 1 TB HDD) runs the research collectors and, later, the BTC book.
The hardware is far more than needed. What matters is that it stays on, stays online and restarts everything by itself.

| Service | What | Started by `setup.sh` |
| --- | --- | --- |
| `cryptoquant-liquidations` | Binance + Bybit liquidation streams (public, no keys) | yes |
| `cryptoquant-option-chains` | Hourly Deribit BTC/ETH chains (public, ~2 MB a day) | yes |
| `cryptoquant-backup.timer` | Nightly 02:30 UTC: SQLite `.backup` of the DB (tax ledger), portfolio state, config | yes |
| `cryptoquant-paper` | BTC book in **paper** mode on real candles (no orders) | only with `--with-paper` |
| live book | Real orders | **never**: manual, behind the live gates (below) |

Every service restarts 30-60 s after a crash and starts at boot. Logs go to the journal (`journalctl -u <name>`)
and the app's own `logs/`, rotated weekly.

## 1. Install Ubuntu Server 24.04 LTS (once, ~30 min)

1. Back up anything you want from Windows. Write the Ubuntu Server 24.04 LTS ISO to a USB stick (balenaEtcher or
   Rufus) and boot from it.
2. Install on the **SSD** (use the whole disk). Tick **"Install OpenSSH server"**. Pick a username and a strong
   password. Skip the extra snaps.
3. In the BIOS: set **"Restore on AC power loss" = Power On**, so it comes back after a power cut. Use wired Ethernet.
4. After the first boot, format and mount the HDD for backups and bulky data:
   ```bash
   lsblk                                              # find the HDD, e.g. /dev/sdb
   sudo mkfs.ext4 -L data /dev/sdb                    # ERASES that disk
   sudo mkdir -p /mnt/hdd && echo 'LABEL=data /mnt/hdd ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
   sudo mount -a && sudo chown "$USER" /mnt/hdd
   ```
5. From the MacBook: `ssh <user>@<desktop-ip>` (find the IP with `ip a` on the desktop, and give it a fixed lease in
   the router). Add your key with `ssh-copy-id <user>@<desktop-ip>`, then turn off password login
   (`PasswordAuthentication no` in `/etc/ssh/sshd_config`, then `sudo systemctl restart ssh`).
   Security updates install automatically (Ubuntu's `unattended-upgrades` is on by default).

## 2. Put the repo and data on it

On the desktop:
```bash
git clone <your GitHub repo URL> ~/CryptoQuantMFT
```
From the Mac (the caches, the database and the recordings aren't in git; ~1.3 GB, do it at home):
```bash
rsync -a --info=progress2 ~/Odysseus/CryptoQuantMFT/data/ <user>@<desktop-ip>:~/CryptoQuantMFT/data/
scp ~/Odysseus/CryptoQuantMFT/.env <user>@<desktop-ip>:~/CryptoQuantMFT/.env     # only needed for paper/live
```
Commits made on the Mac need to be pushed first (you push yourself) and pulled there.

## 3. Run the setup

```bash
cd ~/CryptoQuantMFT
bash deploy/setup.sh                 # collectors + backups
echo 'BACKUP_DIR=/mnt/hdd/cryptoquant-backups' > deploy/backup.env   # backups on the other disk
```
It installs Miniforge and the `CryptoArb` env, masks sleep, turns on time sync, installs the units, and runs a few
tests. It is safe to re-run after a `git pull` (it updates the env and reloads the units).

Then **stop the Mac's collectors**, so the two machines don't record the same thing into different folders:
```bash
launchctl bootout gui/$(id -u)/com.cryptoquant.collectors && rm ~/Library/LaunchAgents/com.cryptoquant.collectors.plist
```

## 4. Daily use

```bash
systemctl status 'cryptoquant-*'                          # everything at a glance
journalctl -u cryptoquant-liquidations --since today      # a service's log
cd ~/CryptoQuantMFT && ~/miniforge3/envs/CryptoArb/bin/python main.py --market-data-status   # rows and recording gaps
sudo systemctl restart cryptoquant-option-chains          # after a git pull that changes code
```
Put `HEALTHCHECK_URL` in `.env` (runbook, dead-man's switch) to get a Telegram-free alert if the paper or live book stops.

## 5. Paper soak, then live

1. `bash deploy/setup.sh --with-paper` starts the BTC book in paper mode with its own state
   (`data/portfolio/btc-live-paper`). Watch it for a few days (TODO.MD section 1), then run the crash drill:
   `sudo systemctl kill -s KILL cryptoquant-paper` mid-cycle, and pull the network cable for a few minutes. It
   should come back by itself with no duplicate orders and a consistent book.
2. Live is deliberately not a service here. Before it: API keys locked to your home IP with no withdrawal
   permission, the kill switch checked, and the checklist in `docs/runbook.md` ("Live portfolio trading"). Start
   it by hand the first time, supervised. Only after that is it worth making it a unit.

## Backups

`deploy/backup.sh` keeps 30 days in `BACKUP_DIR`. The HDD protects against the SSD dying, not against theft or
fire: set `BACKUP_REMOTE=user@host:/path` in `deploy/backup.env` (a NAS, another machine, or a cloud box you can
rsync to) for a copy off the machine. The database holds the tax ledger, so the off-machine copy matters from the
first live trade. Restore: `docs/runbook.md`, "Backup procedure" (the DB file and `state.tar.gz`).
