# Deploying the VAD comparison app (public, AWS VM + Docker)

The app runs as a **single container** (FastAPI server + all VAD engines + the
timeline UI). Recording uses the **visitor's own browser microphone** streamed
to `/api/record` — there is no softphone client and no SIP in the hosted setup,
so nothing but port 80/443 needs to be reachable.

> **Microphone needs HTTPS.** Browsers only allow `getUserMedia` on HTTPS or
> `localhost`. Over plain `http://<ip>` the **WAV-upload** path works but **live
> mic recording is blocked**. To enable recording publicly you need a **domain**
> pointing at the VM so Caddy can get a TLS certificate (steps below).

---

## What you need to sign up for

1. **AWS account** — for the VM (Lightsail is the simplest; EC2 works too).
2. **A domain name** *(only if you want live mic recording)* — any registrar, or
   Route 53. Point an `A` record at the VM's public IP. Without a domain the app
   still runs on `http://<ip>` with WAV upload only.

---

## 1. Create the VM

**Lightsail (recommended):** Console → Create instance → Linux → **Ubuntu 22.04**,
2 GB RAM / 1 vCPU ($5–7/mo) or larger. Create a **static IP** and attach it.
Networking → open TCP **80** and **443** (and 22 for SSH).

**EC2 alternative:** `t3.small`, Ubuntu 22.04; security group inbound TCP 22/80/443.

## 2. Install Docker

SSH in, then:

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

## 3. Get the code onto the VM

There is no git remote configured yet. Either push the repo to GitHub first and
`git clone` it, or copy it straight from your Mac:

```sh
# from your Mac, in the project's parent dir
rsync -av --exclude .venv --exclude data --exclude '.git' \
  voice_speech_activity_detection_application/ ubuntu@<VM_IP>:~/vad/
```

## 4. Run it

```sh
cd ~/vad

# A) with a domain -> HTTPS, so the microphone works:
SITE_ADDRESS=vad.example.com docker compose up -d --build

# B) no domain -> http://<VM_IP> (WAV upload works, live mic does not):
docker compose up -d --build
```

Caddy fetches a Let's Encrypt certificate automatically for the domain. Open
`https://vad.example.com` (or `http://<VM_IP>`), click **Record**, allow the mic,
speak, click **Stop** — every engine runs live on the timeline.

## Operating it

- **Logs:** `docker compose logs -f vad`
- **Update after code changes:** re-sync/pull, then `docker compose up -d --build`
- **Recordings** persist in the `vad-data` volume; remove with `docker compose down -v`.
- **Access is open to anyone** (as chosen). Each visit can upload audio and run
  four engines — real CPU. To limit exposure later, put Caddy `basic_auth` in the
  `Caddyfile`, or restrict the security group to known IPs.

## Notes / limits

- One recording at a time, mirroring the original single-call design; concurrent
  visitors recording simultaneously will interfere. Fine for a demo / small team.
- `ten_vad` installs from a prebuilt wheel that may not exist for every CPU arch;
  if so it reports "unavailable" and the other three engines still run.
- Build the image on the **same CPU architecture** you deploy on (an Apple-Silicon
  Mac builds arm64; most AWS VMs are x86_64 — building on the VM itself, as above,
  avoids any mismatch).
