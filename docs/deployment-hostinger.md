# Deploying to the Hostinger VPS with ForgeCI

ForgeCI tests a commit, builds the image on the build machine, sends the image over SSH and starts it behind the shared edge Caddy
(ForgeCI "images mode"). **No source code is put on the server**: no git checkout, no deploy key, and the image holds compiled
bytecode only (`.pyc`), no `.py` files. Bytecode can still be decompiled, so treat server access as access to the program.

Not yet done: no deployment through ForgeCI or to the server has been run.

## What is on the server

| In `/opt/apps/setuhaul` | From |
|---|---|
| `.env` (mode 600) | written by hand on the server; never in git, the image or ForgeCI's settings |
| `compose.server.yml` | written by hand on the server |
| `releases/<commit>/docker-compose.yml` | sent by ForgeCI with each deployment |

## 1. `.env`

Compose passes it to the container (`env_file`). `docker-compose.yml` sets the database path, Redis address, fallback file and
maintenance switch itself. Names are explained in `.env.example`.

```
SETUHAUL_PUBLIC_URL=https://setuhaul.yuktelos.com
SETUHAUL_TIMEZONE=Asia/Kolkata
SETUHAUL_MODEL_PROVIDER=
SETUHAUL_MODEL_ID=
SETUHAUL_MODEL_API_KEY=
SETUHAUL_GOOGLE_CLIENT_ID=
SETUHAUL_GOOGLE_CLIENT_SECRET=
SETUHAUL_SESSION_SECRET=
SETUHAUL_BOOTSTRAP_ADMIN_EMAIL=
SETUHAUL_SETTINGS_KEY=
```

`SETUHAUL_SESSION_SECRET` needs at least 32 bytes. Put values containing `$` or JSON in single quotes. Optional: the Telegram,
webhook, partner-mail and LangSmith settings.

### Changing settings without logging in

Set `SETUHAUL_SETTINGS_KEY` once, before the first start (or restart the container after adding it):

```bash
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Then **Admin > Configuration** changes the model provider, ID, key, reasoning effort and prices, LangSmith, and the Telegram bot
token and webhook secret, from the next message. **Register webhook** there replaces the manual `setWebhook` command.

- A saved value overrides `.env` until **Reset to .env**. Editing that line in `.env` has no effect while a saved value exists.
- Keys are stored encrypted in the database and never shown again. The backup copies both the database and `.env`, so a full backup
  holds the keys and the means to decrypt them.
- Losing or changing `SETUHAUL_SETTINGS_KEY` means entering the saved keys again; until then the model is treated as unavailable
  if its saved key cannot be read.
- Every save, reset and test is in the audit log, without key values. Any admin can replace keys or switch tracing on.

## 2. `compose.server.yml`

```yaml
services:
  app:
    image: setuhaul:${IMAGE_TAG:-latest}
    networks:
      default: {}
      edge: { aliases: [setuhaul-app] }
networks:
  edge: { external: true }
```

## 3. Edge Caddy

Point the host name at the server, back up `/opt/edge/Caddyfile` (it serves every site), add this and reload Caddy:

```
setuhaul.yuktelos.com {
    reverse_proxy setuhaul-app:8000
}
```

## 4. Backup and status scripts (Pravetra repository, `deploy/hostinger`)

Both hold fixed lists of applications. Without these changes a deployment passes while SetuHaul is neither backed up nor checked.

1. **First**, on the server: `docker volume create setuhaul_state`. The backup fails on a missing volume, which would block every
   application's deployment.
2. `backup-now.sh`: add `setuhaul_state:setuhaul` to the volumes, and `/opt/apps/setuhaul/.env` and
   `/opt/apps/setuhaul/compose.server.yml` to the configuration files.
3. `status.sh`: add the container `setuhaul-app-1`, the certificate host and the public check
   `https://setuhaul.yuktelos.com/ready`.
4. Confirm how a changed script reaches the server (it was linked into Pravetra's folder, and Pravetra now deploys images).

## 5. ForgeCI `config/forgeci.yaml`

Add `setuhaul: {}` under `builds.isolated`, and this target:

```yaml
  hostinger-setuhaul:
    name: 'Hostinger: SetuHaul'
    provider: ssh-compose
    endpoint: ssh://deploy@<server>:22
    config:
      application: setuhaul
      remote_path: /opt/apps/setuhaul
      compose_files: [docker-compose.yml, compose.server.yml]
      health_url: https://setuhaul.yuktelos.com/health/ready
      version_url: https://setuhaul.yuktelos.com/version
      pre_deploy: /opt/deploy/hostinger/backup-now.sh
      pre_deploy_timeout_seconds: 1800
      verify_command: /opt/deploy/hostinger/status.sh
      deploy_mode: images
      images: setuhaul
      build_images: setuhaul=Dockerfile
```

`engineering.yaml` expects `python3.12` on the build machine.

## 6. First deployment

1. Push the commit; add the project in ForgeCI (**+ Add Project**, From GitHub); run a Full build.
2. Take a Hostinger snapshot.
3. A first image deployment uses ForgeCI's two steps or **Build latest, then deploy** with the `hostinger-setuhaul` target.
4. Check `/health/ready` and `/version` from outside.
5. Add `https://setuhaul.yuktelos.com/auth/callback` to the Google OAuth client, and register the Telegram webhook if used
   (section 7a of [running-and-demonstrating.md](running-and-demonstrating.md)).

There is no earlier release to roll back to, so leave auto-deploy off.

Break-glass admin password, on the server (set `SETUHAUL_LOCAL_ADMIN=1` to enable the form):

```bash
docker exec -it setuhaul-app-1 python scripts/set_admin_password.pyc you@example.com
```

## Limits

- Run **one** application container: SQLite, and a second copy could send duplicate Telegram notices.
- The data is the dated sample (August 2026): with the real clock its slots are in the past, and delay requests go to a person
  because labour and consignee evidence is missing.
- Partner email in live mode also needs the jobs in `src/setuhaul/jobs/` scheduled; this set-up does not schedule them.
- Never run `docker compose down -v` or prune volumes: that deletes the database.
