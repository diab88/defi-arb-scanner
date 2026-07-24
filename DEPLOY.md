# Deploying to Divio

Divio is a Docker-native PaaS: it builds your **Dockerfile** and runs the container.
This repo is already Divio-ready — the container listens on **port 80** (Divio's requirement).

## Steps
1. **Create a Divio account** and log in to the Control Panel (<https://control.divio.com>).
2. **Create a new application** → choose to **connect an existing Git repository**, and point
   it at `https://github.com/diab88/defi-arb-scanner` (authorize Divio's GitHub access; the repo
   is private).
3. Divio detects the **`Dockerfile`** and builds the image. No `docker-compose.yml` is used in the
   cloud (it's for local dev only) — the container just needs to listen on **port 80**, which it does.
4. **Set environment variables** (Control Panel → your app → **Env Variables**), for the Test and/or
   Live environment:
   ```
   TELEGRAM_BOT_TOKEN = <your rotated bot token>
   TELEGRAM_CHAT_ID   = 797814228
   DEFI_MONITOR_INTERVAL = 3600
   ```
   (You do **not** need `PORT` — the image defaults to 80. Divio also injects `DATABASE_URL`,
   `DEFAULT_STORAGE_DSN`, `DOMAIN`, `SECRET_KEY`; this app ignores them.)
5. **Deploy** the environment from the Control Panel. Divio gives you a public URL (`*.divioapp.com`
   or your custom domain).

## ⚠️ Two Divio-specific caveats

**1. Local file storage is NOT persistent on Divio.** Divio containers are stateless, so the
`data/` folder (portfolio + notifications) **resets on every deploy/restart**. Two options:
   - **Accept resets** — fine if you just want the live scanner and don't rely on the saved portfolio.
   - **Persist properly** — move `data/` persistence to a Divio **object-storage** or **database**
     addon (`DEFAULT_STORAGE_DSN` / `DATABASE_URL`, which Divio injects). This needs a small code
     change to `dashboard.py`'s load/save functions. Ask and I'll implement it.

**2. No authentication + public URL.** Divio exposes the app on a public domain, and the dashboard
has no login — anyone with the URL can view/modify your portfolio. Before going live, add auth
(basic-auth inside the app, or restrict access). Ask and I'll add it.

## Local dev with the Divio-style setup
The container now listens on 80; locally you still reach it at the same URL because compose maps it:
```bash
docker compose up -d --build     # http://localhost:8765  (host 8765 -> container 80)
```

---

# Deploying to Oracle Cloud (Always Free)

Run the scanner 24/7 on a free Oracle Cloud VM so portfolio monitoring and Telegram
alerts keep working independently of your laptop.

> ⚠️ **Security first.** The dashboard has **no login**. This guide locks the app port
> to *your IP only* so it isn't open to the internet. Do **not** open it to everyone
> until you've added an auth layer (see "Making it safely public" at the end).

---

## 1. Create the Always Free account
1. Go to <https://www.oracle.com/cloud/free/> → **Start for free**, sign up.
   (Requires a card for identity check; the Always Free resources below are never charged.)
2. Pick a **Home Region** close to you.

## 2. Create the VM
1. Console → **Menu → Compute → Instances → Create instance**.
2. **Image & shape:**
   - Image: **Canonical Ubuntu 22.04**.
   - Shape: **Change shape → Ampere (ARM)** `VM.Standard.A1.Flex` (1 OCPU / 6 GB is plenty),
     or if ARM capacity is unavailable, **AMD** `VM.Standard.E2.1.Micro` (1 OCPU / 1 GB — enough; the app uses ~110 MB).
   - Both are **Always Free** eligible ("Always Free-eligible" tag shows on the shape).
3. **SSH keys:** choose **Generate a key pair** (download the private key) or paste your own public key.
4. **Networking:** keep the default VCN/subnet, assign a **public IPv4**.
5. **Create.** Note the **public IP** once it's running.

## 3. Open the app port to YOUR IP only
Find your current public IP: <https://ifconfig.me> (e.g. `203.0.113.5`).

Console → **Networking → Virtual Cloud Networks → (your VCN) → Security Lists → Default Security List → Add Ingress Rules:**
- Source Type: **CIDR**
- Source CIDR: **`YOUR.IP.HERE/32`**  ← just your IP
- IP Protocol: **TCP**
- Destination Port Range: **`8765`**
- Save.

*(SSH port 22 is already open in the default rules.)*

## 4. Connect and install Docker
```bash
# from your Mac (adjust key path + IP)
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR.PUBLIC.IP
```
On the VM:
```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
newgrp docker          # apply group without re-login

# Ubuntu on OCI blocks non-SSH ports at the OS level too — open 8765
sudo iptables -I INPUT 6 -p tcp --dport 8765 -j ACCEPT
sudo netfilter-persistent save
```

## 5. Get the code onto the VM
**Option A — copy straight from your Mac (simplest, no GitHub needed).** Run on your Mac:
```bash
cd /Users/adam.diab/Documents/GitHub
rsync -av -e "ssh -i ~/Downloads/ssh-key-*.key" \
  --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
  defi-arb-scanner ubuntu@YOUR.PUBLIC.IP:~/
```

**Option B — via a private GitHub repo.** On your Mac, inside `defi-arb-scanner`:
```bash
git init && git add -A && git commit -m "deploy"
# create a PRIVATE repo on github.com, then:
git remote add origin git@github.com:YOURNAME/defi-arb-scanner.git
git push -u origin main
```
On the VM: `git clone https://github.com/YOURNAME/defi-arb-scanner.git`
(`.env`, `data/`, `snapshots/` are git-ignored, so they won't be pushed — good.)

## 6. Configure secrets + run
On the VM:
```bash
cd ~/defi-arb-scanner

# Telegram alerts (same values you used locally)
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=your-new-bot-token
TELEGRAM_CHAT_ID=797814228
DEFI_MONITOR_INTERVAL=3600
EOF

docker compose up -d --build
docker compose ps           # should show "Up"
docker compose logs --tail 20
```
> Use a **freshly rotated** bot token here (the old one was shared in chat). @BotFather → `/token`.

## 7. Open it
In your browser (from the same network as the IP you allow-listed):
```
http://YOUR.PUBLIC.IP:8765
```
Portfolio + Telegram monitoring now run 24/7 on the VM.

---

## Operating it

```bash
docker compose logs -f            # live logs
docker compose restart            # restart
docker compose down               # stop
docker compose up -d --build      # update after code changes / re-copy
```
- **Auto-restart on reboot:** already handled — `restart: unless-stopped` in the compose file, and Docker starts on boot.
- **Persistence:** portfolio + notifications live in `~/defi-arb-scanner/data/` (a Docker volume). Back it up with:
  ```bash
  tar czf defi-data-backup.tgz -C ~/defi-arb-scanner data
  ```
- **Your IP changed?** Update the ingress rule (step 3) with the new `/32`.

## Making it safely public (only if you need access from anywhere)
Instead of allow-listing one IP, add authentication + HTTPS and *then* open port 443:
1. Point a domain at the VM's IP.
2. Put **Caddy** in front (auto-HTTPS + basic-auth) — a ~10-line `Caddyfile` reverse-proxying to `dashboard:8765`, added as a second service in `docker-compose.yml`.
3. Open **443** (not 8765) to `0.0.0.0/0`, keep 8765 internal.

Ask and I'll add the Caddy service + Caddyfile to the compose stack.
