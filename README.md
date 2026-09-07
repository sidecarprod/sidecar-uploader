# Sidecar Uploader

Presenter/filmmaker file upload portal. Per-event links, each with its own
title, description, file-type/size restrictions, and an auto-created folder
on the NAS. Built on Uppy (frontend) + tusd (resumable upload transport) +
Flask (validation, routing, admin) + nginx (path routing so one hostname
serves both).

## Layout

```
/volume2/docker/uploader/
├── docker-compose.yml
├── .env                  <- create from .env.example, keep out of git
├── nginx/nginx.conf
├── flask-app/            <- app source, built into a container
├── db/                   <- events.db (created automatically)
└── tusd-data/            <- tusd's own staging dir (created automatically)
```

Finished uploads land in:

```
/volume3/Large Drive/Downloads/<Event Name>/
```

The folder name matches whatever you type as the "Event name" in the admin
form — human-readable, spaces are fine. The URL slug is separate and always
hyphenated/lowercase, e.g. `submit.sidecarprod.com/faith-formation-sf-2026`.

## First-time setup

**A. Get the image built and published (one-time)**

1. Create a new GitHub repo under your org, e.g. `sidecarprod/sidecar-uploader`.
2. Push everything in this folder to that repo, including `.github/workflows/build.yml`.
3. The push triggers the Action automatically — check the **Actions** tab on
   the repo to watch it build and push to `ghcr.io/sidecarprod/sidecar-uploader`.
4. Once it succeeds, go to the package on GitHub (org page → **Packages** →
   `sidecar-uploader`) → **Package settings** → change visibility to
   **Public**. This is what lets the NAS pull it without any registry login,
   same as `ghcr.io/alexta69/metube` in your other stack — no credentials
   to manage on the NAS side.

**B. Deploy on the NAS**

5. Copy this folder to `/volume2/docker/uploader` on SidecarNAS (everything
   except `.github/` and the `flask-app/` source matters here now — those
   only need to exist in the GitHub repo, not on the NAS, since the NAS
   just pulls the built image).

6. Create `.env` from the template and fill in real secrets:
   ```bash
   cd /volume2/docker/uploader
   cp .env.example .env
   openssl rand -hex 32   # run twice, paste into ADMIN_TOKEN and FLASK_SECRET_KEY
   ```

7. Double-check the bind mount path for volume3 matches your actual mount.

8. Import `docker-compose.yml` the same way you imported `metube` — it's
   now a pure `image:` reference with no build step, so it should go
   through UGOS's Project importer cleanly.

8a. Point your Cloudflare Tunnel's `submit.sidecarprod.com` hostname
    (Published application route) at `http://10.10.0.10:8420`.

**C. Making code changes later**

9. Edit the code, push to the `sidecarprod/sidecar-uploader` repo's `main`
   branch. The Action rebuilds and pushes a fresh `:latest` automatically.
10. On the NAS, re-pull and recreate the `uploader-web` container — through
    UGOS's update/re-pull option for the stack, or Portainer's "Re-pull
    image and redeploy" if you're managing it there. No rebuild step on
    the NAS ever again.

## Creating an event link

1. Visit `https://submit.sidecarprod.com/admin` and enter your `ADMIN_TOKEN`.
2. Fill in the form:
   - **Event name** — becomes the folder name, e.g. `Faith Formation SF 2026`
   - **URL slug** — leave blank to auto-generate, or set your own
   - **Title / description** — shown to presenters on the page
   - **Allowed extensions** — comma separated, e.g. `mp4, mov, pdf, pptx, key`
   - **Max file size (MB)**
   - **Confirmation message** — shown after a successful upload
3. Click **Create link**. The folder is created immediately at
   `/volume3/Large Drive/Downloads/<Event Name>/`, and the link is live at
   `submit.sidecarprod.com/<slug>`.

Send that link to presenters. Every event link shares the same branding
(dark theme, Sidecar header) but shows its own title, description, allowed
types, and confirmation message.

## How validation works

- **Client-side** (Uppy): filters the file picker and shows a friendly error
  immediately — good UX, but not trustworthy on its own.
- **Server-side** (tusd `pre-create` hook → Flask `/hooks`): every upload is
  re-checked against the event's allowed extensions and max size before
  tusd accepts a single byte. A request for an unknown/mistyped event slug
  is rejected outright.
- **On completion** (tusd `post-finish` hook): the finished file is copied
  from tusd's staging area into the event's NAS folder, then the staging
  copy is deleted.

## No virus scanning

This build does not scan uploads — files move straight from tusd's staging
area into the event's NAS folder in `handle_post_finish()` in `app.py`.
If you ever want a scan step, that function is the place to add it (call
out to `clamdscan` or similar before or instead of deleting the staging
copy), but nothing here depends on it.

## Notes / things to adjust to taste

- Branding: dark neutral theme with your logo in the header on every page,
  orange (`--brand-cta`) for buttons/upload action, blue (`--brand-blue`)
  for links — pulled straight from `Sidecar_Round.png`. Colors and the logo
  file live in `flask-app/static/`.
- `max_file_size_mb` accepts anything; there's no hard ceiling, so a
  presenter link for raw video footage can be set much higher than one for
  slide decks.
- Deleting an event link from the admin page does **not** delete its
  uploaded files — only the link and its DB entry.
- The `/admin` token is stored in a Flask session cookie after first entry,
  so you won't have to paste it on every visit from the same browser.
