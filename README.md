# The Hungry Games 2026

The Thanksgiving 2026 recipe site: every dish has its own page with its (fictional, unhinged) backstory and its (real) recipe. Guests can:

- **Rate each dish** from 1 to 5 stars. Each dish page shows its average, and the home page has a live ranking of all fifteen.
- **Upload photos**, either of a specific dish (on that dish's page) or of the night in general ("From the arena" on the home page).

No accounts needed. Everything runs in one small Docker container, and ratings and photos are saved in the `data/` folder next to this file.

## Run it

You need [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on a Mac or Windows PC is fine).

```sh
git clone https://github.com/bgsamz/hungry-games-2026.git
cd hungry-games-2026
cp .env.example .env      # then edit .env and pick your own admin phrase
docker compose up -d --build
```

Open <http://localhost:8080> on that machine.

To stop it: `docker compose down`. Ratings and photos stay in `data/` and come back next time you start it.

To update after pulling new changes: `git pull && docker compose up -d --build`.

## Let guests in

**Same Wi-Fi (easiest).** Find the computer's local IP address (Mac: System Settings → Wi-Fi → Details; Windows: `ipconfig`). Guests open `http://<that-ip>:8080`, for example `http://192.168.1.20:8080`. If it doesn't load on a phone, allow port 8080 through the computer's firewall.

**From anywhere (guests on cell data, or a nicer link).** Put a tunnel in front of it. Two free options:

- Cloudflare quick tunnel, no account needed, gives a random `https://….trycloudflare.com` link that changes each time you run it:
  ```sh
  cloudflared tunnel --url http://localhost:8080
  ```
  For a link that stays the same (better for printed QR codes), set up a named Cloudflare Tunnel with your own domain.
- Tailscale Funnel, which gives a stable `https://<machine>.<tailnet>.ts.net` link:
  ```sh
  tailscale funnel 8080
  ```

Make the QR codes only once you know the final address. Each dish's link is the address plus `#` and the dish name, for example `http://192.168.1.20:8080/#turkey`.

Dish names for links: `turkey`, `ham`, `stuffing`, `mac-and-cheese`, `potato-casserole`, `scalloped-potatoes`, `sweet-potato`, `green-beans`, `corn-pudding`, `cranberry-sauce`, `rolls`, `pie-crust`, `pecan-pie`, `pumpkin-pie`, `sangria`.

## Your own dish photos

To set the main photo for a dish yourself, put it in `static/dish-photos/` named after the dish, for example `static/dish-photos/turkey.jpg` (`.png` and `.webp` work too), then run `docker compose up -d --build`. Without one, each dish uses its newest guest photo.

## Deleting guest photos

Anyone with the link can upload, so you can remove photos. Set `ADMIN_TOKEN` in `.env` (and restart). On the site, tap **Host tools** at the bottom of any page and enter that phrase. That browser can then delete any photo from its full-size view.

## Settings

Set these in `.env` (or `docker-compose.yml`):

| Setting | Default | What it does |
| --- | --- | --- |
| `ADMIN_TOKEN` | empty | Phrase that unlocks photo deletion. Empty means nobody can delete. |
| `MAX_UPLOAD_MB` | `40` | Largest photo accepted, per file. Photos upload at full quality. |
| `UPLOADS_PER_10_MIN` | `40` | Upload limit per device's network address, to stop floods. |

To change the port, edit `"8080:8080"` in `docker-compose.yml` (the first number is the port on your computer).

## How it works

- `server.py`: a small Python server that serves the site and stores ratings in SQLite (`data/hungry.db`) and photos in `data/photos/`.
- Photos are kept exactly as uploaded, at full quality. The one change: GPS location is blanked out of JPEGs so guests' home addresses aren't published (done in place, without re-encoding). The server also saves a smaller `_t.jpg` preview next to each photo for the galleries; the full-quality original opens when you tap a photo.
- `static/index.html`: the whole site. Dish text, stories and recipes are in the `D` list near the top of the script.
- Ratings are one per dish per browser. Tapping the same star again takes your vote back. Clearing browser data lets someone vote again; it's a dinner party, not an election.

To back everything up, copy the `data/` folder.
