# Quotarr

A self-hosted [Frinkiac](https://frinkiac.com) / [Morbotron](https://morbotron.com) clone for **any show in your Plex library**. Type a quote and you get a grid of screencaps where it's said. Click one to caption it, step through nearby frames, or turn a few lines of dialogue into a GIF or MP4 with the subtitles burned in. Every image has a stable URL you can paste into Discord.

It runs as one Docker container next to Plex. It doesn't modify Plex, and it isn't a Plex plugin, because Plex removed plugin support years ago.

## Does this already exist?

Not quite. As of October 2026:

- **Frinkiac, Morbotron, Master of All Science**: hosted sites for a handful of shows. They aren't open source and can't use your library.
- **[Cliparr](https://github.com/TechSquidTV/Cliparr)**: a good self-hosted clip/GIF editor for Plex and Jellyfin. You pick one episode and scrub its timeline; it doesn't search dialogue across the library.
- **[PlexGifMaker](https://github.com/TechBuckler/PlexGifMaker)**: makes captioned GIFs from Plex items using their subtitle timings. Same limitation as Cliparr, and no commits since late 2024.

Quotarr fills that gap: one full-text index over every subtitle line of the shows you pick, with frames pulled straight from your media files.

## How much disk does it need?

Very little, by default. Morbotron stores every unique frame of Futurama (about 109 GB). Quotarr stores only the **subtitle text** and renders frames **on demand** with ffmpeg, caching what people actually view.

| What | Rough size |
|---|---|
| Search index (subtitle text) | ~100–150 KB per episode. All of Futurama is ~20 MB |
| On-demand frame/meme/GIF cache | Capped by `CACHE_MAX_MB` (default 2 GB); least recently used files are deleted first |
| Optional: pregenerate a thumbnail for every line (`PREGENERATE_THUMBS=true`) | ~5–10 MB per half-hour episode. Futurama ≈ 1–1.5 GB, Invincible S1–S3 ≈ 300–400 MB |

The Admin page shows current usage and how much full pregeneration would take for what you've indexed.

On-demand frames take roughly 0.1–0.3 s each when the media is mounted into the container, and a little longer when streamed from Plex. GIFs take 1–3 s.

## Setup (Unraid)

Mount your media into Quotarr **at the same path your Plex container uses**, so Plex's file paths work unchanged and frames are read straight from disk. The compose defaults assume Plex maps `/mnt/user/Media` → `/data` and keep app data in `/mnt/user/Docker/Quotarr`. If your layout differs, set `MEDIA_DIR` and `CONFIG_DIR` in `.env` (for example `CONFIG_DIR=/mnt/user/appdata/quotarr`).

1. **Get the code onto the server**, e.g. `git clone https://github.com/Conor-Code-Logistic-Tech/quotarr /mnt/user/Docker/quotarr-src`.
2. **Get your Plex token**: follow [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
3. **Create `.env`** next to `docker-compose.yml`:
   ```sh
   cp .env.example .env
   nano .env        # set PLEX_TOKEN=..., optionally AUTO_INDEX_SHOWS=Futurama;Invincible
   ```
4. **Start it**, either from the *Docker Compose Manager* plugin (Community Apps), pointing it at that folder, or over SSH:
   ```sh
   cd /mnt/user/Docker/quotarr-src
   docker compose up -d --build
   ```
5. Open **`http://<unraid-ip>:6464/admin`** and switch on the shows you want. Indexing runs in the background (seconds per episode for external SRTs, longer when subtitles have to be pulled out of the video file). Search works as soon as the first episodes are done.

App data (database, thumbnails, cache) lives in `CONFIG_DIR`. The container drops to user `99:100` (Unraid's nobody:users) after fixing permissions on that folder.

If the Admin page says it can't reach Plex, set `PLEX_URL=http://<unraid-ip>:32400` in `.env`. The default, `host.docker.internal`, assumes Plex runs with host networking on the same machine (the usual Unraid setup).

### Other setups

- **Plex sees media under a different path than this container does**: set `PATH_MAPPINGS="/plex/path=/container/path"` (separate several with `;`).
- **No media mount at all** (e.g. Plex is on another machine): remove the `/data` volume. Quotarr then streams frames and embedded subtitles from Plex over HTTP. It's slower, but it works.
- **Exposing it outside your LAN**: put it behind your reverse proxy (Nginx Proxy Manager), set `ADMIN_PASSWORD`, and set `PUBLIC_URL` so link previews use the public address.

## Subtitles are the whole game

Search only knows what's in the subtitles. For each episode Quotarr picks the best **text** track Plex knows about:

1. Your preferred language (`SUBTITLE_LANGUAGES`, default English), skipping forced and "Signs & Songs" tracks
2. Plain over SDH (hearing-impaired); SDH sound tags like `[LAUGHS]` and `FRY:` speaker labels are stripped anyway (`STRIP_SDH`)
3. External/sidecar SRTs (fast) over tracks embedded in the MKV (pulled out with ffmpeg)

Episodes that only have **image-based** subtitles (PGS/VobSub, common on Blu-ray and DVD remuxes) can't be read without OCR. They show up as *No subs* on the Admin page with the reason. The easy fix is **[Bazarr](https://www.bazarr.media/)**: point it at Sonarr and it downloads matching SRTs next to your files. Episodes marked *No subs* are retried on every rescan (every `RESCAN_HOURS`, or right away when you click *Refresh from Plex*), so new SRTs get picked up once Plex has seen them. To swap the subtitles on an episode that's already indexed, click *Reindex* on the show.

## Using it

- **Search**: words in any order, stemmed (`run` finds *running*), with prefix matching on the last word as you type. Quotes that span two subtitle cues still match as a phrase. Press `/` to jump to the search box.
- **Show filter**: chips under the search bar narrow results to specific shows.
- **Caption page**: edit the caption and the preview updates live. Copy the image link to paste anywhere. `←`/`→` step between lines, and the strip under the frame shows frames up to 2 s either side.
- **GIF / MP4**: tick a run of lines (up to `MAX_CLIP_SECONDS`), then choose burned-in subtitles, your own caption, or no text. MP4s keep the audio.
- **Link previews**: caption page URLs carry Open Graph tags, so Discord, iMessage, etc. show the frame.

### URL scheme

```
/caption/<show>/<S01E02>/<ms>                    caption page
/img/<show>/<S01E02>/<ms>.jpg                    full frame
/thumb/<show>/<S01E02>/<ms>.jpg                  small frame
/meme/<show>/<S01E02>/<ms>.jpg?b64lines=<b64>    frame + caption (URL-safe base64, UTF-8)
/clip/<show>/<S01E02>/<start>-<end>.gif|mp4      clip (?b64lines=… custom text, ?nosubs=1 none)
/api/search?q=…&shows=futurama,invincible
```

`<show>` is a slug made from the Plex title (`futurama`, `invincible`). If you remove a show from Plex and add it back, Quotarr re-links it by title and year, so old links keep working.

## Configuration

All settings are environment variables (put them in `.env` when using compose).

| Variable | Default | Meaning |
|---|---|---|
| `PLEX_URL` | `http://host.docker.internal:32400` (compose) | Plex server URL |
| `PLEX_TOKEN` | — | **Required.** Plex auth token |
| `PLEX_VERIFY_SSL` | `true` | Set `false` for self-signed HTTPS |
| `AUTO_INDEX_SHOWS` | — | `;`-separated titles to switch on automatically (until you toggle them yourself) |
| `PATH_MAPPINGS` | — | `plex=local;…` path prefix rewrites |
| `SUBTITLE_LANGUAGES` | `eng,en` | Preferred languages in order; `*` = any |
| `STRIP_SDH` | `true` | Remove `[sound effects]`, `(V.O.)`, `NAME:` labels |
| `PREGENERATE_THUMBS` | `false` | Grab a thumbnail for every line while indexing |
| `CACHE_MAX_MB` | `2048` | Cap for on-demand frames, memes, clips (and thumbnails unless pregenerated) |
| `THUMB_WIDTH` / `FRAME_WIDTH` | `480` / `1280` | Output widths (never upscaled) |
| `GIF_WIDTH` / `MP4_WIDTH` | `480` / `854` | Clip widths |
| `MAX_CLIP_SECONDS` | `15` | Longest GIF/MP4 |
| `FFMPEG_CONCURRENCY` | `4` | Parallel ffmpeg jobs |
| `RESCAN_HOURS` | `6` | How often to look for new episodes and subtitles |
| `ADMIN_PASSWORD` / `ADMIN_USER` | — / `admin` | Protect the Admin page and API |
| `PUBLIC_URL` | — | Base URL used in link previews |
| `MEME_FONT` | DejaVu Sans Bold | Path to a `.ttf` for captions (mount it in) |
| `PUID` / `PGID` | `99` / `100` | User the app runs as |

## How it works

```
Plex API ──► indexer ──► subtitle track (Plex sidecar download, or ffmpeg extract from the MKV)
                     └─► clean + parse ──► SQLite FTS5 (porter stemming, BM25 ranking)

browser ──► /api/search ──► FTS5 ──► results grid
        └─► /thumb, /img, /meme, /clip ──► ffmpeg seek on the file (local mount, or Plex HTTP stream)
                                           ├─ anamorphic DVD pixels squared up, HDR tone-mapped to SDR
                                           └─ Pillow captions / libass burned-in subtitles ──► LRU disk cache
```

Code: `quotarr/indexer.py` (Plex sync and subtitle selection), `quotarr/media.py` (ffmpeg), `quotarr/search.py` (queries), `quotarr/main.py` (routes), `quotarr/static/` (frontend, plain JS with no build step).

## Development

```sh
docker build -t quotarr:dev .

# unit tests
docker run --rm --entrypoint sh -v "$PWD":/src -w /src quotarr:dev -c "pip install -q pytest && python -m pytest -q tests"

# end-to-end against a fake Plex with generated test videos
mkdir -p /tmp/q/fixtures /tmp/q/data
docker run --rm --entrypoint sh -v "$PWD/dev":/d:ro -v /tmp/q/fixtures:/fixtures quotarr:dev /d/make_fixtures.sh /fixtures
docker network create qtest
docker run -d --name mockplex --network qtest -v "$PWD/dev":/app/dev:ro -v /tmp/q/fixtures:/fixtures:ro \
  --entrypoint uvicorn quotarr:dev dev.mock_plex:app --host 0.0.0.0 --port 32400
docker run -d --name quotarr --network qtest -p 6464:6464 -e PLEX_URL=http://mockplex:32400 -e PLEX_TOKEN=testtoken \
  -e "AUTO_INDEX_SHOWS=Test Show;Another Show" -v /tmp/q/data:/config -v /tmp/q/fixtures:/data:ro quotarr:dev
```

The fixtures cover an external SRT, embedded SRT and ASS tracks, an image-subtitles-only episode, an anamorphic DVD-style file, and an HDR10 file.
