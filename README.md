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
cp .env.example .env      # then edit .env and set your host password
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

## Host login

Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env`, then restart (`docker compose up -d`). On the site, tap **Host tools** at the bottom of any page and sign in. You stay signed in on that browser for 30 days.

Signed in, you can:

- **Set each dish's main photo.** Every dish page shows a host bar under its photo with **Upload main photo**, **Replace** and **Remove**. It uploads at full quality.
- **Delete guest photos.** Tap any photo, then **Delete photo**.

Each dish's main photo is, in order: the one you uploaded as host, then a file in `static/dish-photos/<dish>.jpg` if you added one to the code, then the newest guest photo of that dish.

## Settings

Set these in `.env` (or `docker-compose.yml`):

| Setting | Default | What it does |
| --- | --- | --- |
| `ADMIN_USERNAME` | `host` | Host login username. |
| `ADMIN_PASSWORD` | empty | Host login password. Empty turns host login off. |
| `MAX_UPLOAD_MB` | `100` | Largest photo accepted, per file. Photos upload at full quality. |
| `UPLOADS_PER_10_MIN` | `40` | Upload limit per device's network address, to stop floods. |

To change the port, edit `"8080:8080"` in `docker-compose.yml` (the first number is the port on your computer).

## How it works

- `server.py`: a small Python server that serves the site and stores ratings in SQLite (`data/hungry.db`) and photos in `data/photos/`.
- Photos are kept exactly as uploaded, at full quality. The one change: GPS location is blanked out of JPEGs so guests' home addresses aren't published (done in place, without re-encoding). The server also saves a smaller `_t.jpg` preview next to each photo for the galleries; the full-quality original opens when you tap a photo.
- `static/index.html`: the whole site. Dish text, stories and recipes are in the `D` list near the top of the script.
- Ratings are one per dish per browser. Tapping the same star again takes your vote back. Clearing browser data lets someone vote again; it's a dinner party, not an election.

To back everything up, copy the `data/` folder.
