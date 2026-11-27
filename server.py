"""The Hungry Games: a tiny self-hosted server for the Thanksgiving recipe site.

Serves the site, stores ratings and guest photos. Standard library only.
Data lives in DATA_DIR (default /data): hungry.db (SQLite) and photos/.
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static")).resolve()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024
UPLOADS_PER_10_MIN = int(os.environ.get("UPLOADS_PER_10_MIN", "40"))

DISH_IDS = {
    "turkey", "ham", "stuffing", "mac-and-cheese", "potato-casserole",
    "scalloped-potatoes", "sweet-potato", "green-beans", "corn-pudding",
    "cranberry-sauce", "rolls", "pie-crust", "pecan-pie", "pumpkin-pie", "sangria",
}
PHOTO_TAGS = DISH_IDS | {"general"}
VOTER_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\.(jpg|png|webp|gif)$")

TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
}

PHOTO_DIR = DATA_DIR / "photos"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "hungry.db"
db_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


with db() as con:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS ratings (
            dish TEXT NOT NULL, voter TEXT NOT NULL, stars INTEGER NOT NULL,
            updated REAL NOT NULL, PRIMARY KEY (dish, voter));
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY, dish TEXT NOT NULL, filename TEXT NOT NULL,
            created REAL NOT NULL);
        """
    )

upload_log = {}  # ip -> [timestamps]
upload_lock = threading.Lock()


def sniff(data: bytes):
    """Return a file extension for supported image bytes, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def allowed_upload(ip: str) -> bool:
    now = time.time()
    with upload_lock:
        log = [t for t in upload_log.get(ip, []) if now - t < 600]
        if len(log) >= UPLOADS_PER_10_MIN:
            upload_log[ip] = log
            return False
        log.append(now)
        upload_log[ip] = log
        return True


def photo_json(row):
    return {"id": row["id"], "dish": row["dish"], "url": "/photos/" + row["filename"],
            "created": row["created"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "HungryGames/1.0"

    # ---------- helpers ----------
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, cache="no-cache"):
        try:
            data = path.read_bytes()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0]) or "?"

    def log_request(self, code="-", size="-"):
        # Only log problems, so the console stays readable during the party.
        try:
            if int(code) >= 400:
                super().log_request(code, size)
        except (TypeError, ValueError):
            pass

    # ---------- routes ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        p = url.path

        if p == "/api/ratings":
            with db() as con:
                rows = con.execute(
                    "SELECT dish, AVG(stars) a, COUNT(*) n FROM ratings GROUP BY dish").fetchall()
                out = {r["dish"]: {"avg": round(r["a"], 2), "count": r["n"]} for r in rows}
                voter = (q.get("voter") or [""])[0]
                mine = {}
                if VOTER_RE.match(voter):
                    mine = {r["dish"]: r["stars"] for r in con.execute(
                        "SELECT dish, stars FROM ratings WHERE voter=?", (voter,))}
            return self.send_json({"ratings": out, "mine": mine})

        if p == "/api/photos":
            dish = (q.get("dish") or [""])[0]
            with db() as con:
                if dish:
                    rows = con.execute(
                        "SELECT * FROM photos WHERE dish=? ORDER BY created DESC", (dish,)).fetchall()
                else:
                    rows = con.execute("SELECT * FROM photos ORDER BY created DESC LIMIT 500").fetchall()
            return self.send_json({"photos": [photo_json(r) for r in rows]})

        if p == "/api/dish-photos":
            # Official photos you drop into static/dish-photos/<dish id>.jpg (or .png/.webp)
            out = {}
            folder = STATIC_DIR / "dish-photos"
            if folder.is_dir():
                for f in sorted(folder.iterdir()):
                    if f.stem in DISH_IDS and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                        out[f.stem] = "dish-photos/" + f.name
            return self.send_json({"photos": out})

        if p == "/api/admin/check":
            ok = bool(ADMIN_TOKEN) and secrets.compare_digest(
                self.headers.get("X-Admin-Token", ""), ADMIN_TOKEN)
            return self.send_json({"ok": ok})

        if p.startswith("/photos/"):
            name = p[len("/photos/"):]
            if not FILE_RE.match(name):
                return self.send_error(404)
            return self.send_file(PHOTO_DIR / name, cache="public, max-age=31536000, immutable")

        # static site
        rel = "index.html" if p in ("", "/") else p.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            return self.send_error(404)
        return self.send_file(target)

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)

        if url.path == "/api/rate":
            raw = self.read_body(2048)
            try:
                d = json.loads(raw or b"")
                dish, voter, stars = d["dish"], d["voter"], int(d["stars"])
            except (ValueError, KeyError, TypeError):
                return self.send_json({"error": "bad request"}, 400)
            if dish not in DISH_IDS or not VOTER_RE.match(str(voter)) or not 0 <= stars <= 5:
                return self.send_json({"error": "bad request"}, 400)
            with db_lock, db() as con:
                if stars == 0:
                    con.execute("DELETE FROM ratings WHERE dish=? AND voter=?", (dish, voter))
                else:
                    con.execute(
                        "INSERT INTO ratings (dish, voter, stars, updated) VALUES (?,?,?,?) "
                        "ON CONFLICT(dish, voter) DO UPDATE SET stars=excluded.stars, updated=excluded.updated",
                        (dish, voter, stars, time.time()))
            return self.send_json({"ok": True})

        if url.path == "/api/photos":
            dish = (q.get("dish") or ["general"])[0]
            if dish not in PHOTO_TAGS:
                return self.send_json({"error": "unknown dish"}, 400)
            if not allowed_upload(self.client_ip()):
                return self.send_json({"error": "Too many uploads. Try again in a few minutes."}, 429)
            data = self.read_body(MAX_UPLOAD)
            if data is None:
                return self.send_json({"error": f"Photos must be under {MAX_UPLOAD // 1048576} MB."}, 413)
            ext = sniff(data)
            if not ext:
                return self.send_json({"error": "That file isn't a JPEG, PNG, WebP or GIF image."}, 415)
            pid = secrets.token_urlsafe(12)
            filename = f"{pid}.{ext}"
            (PHOTO_DIR / filename).write_bytes(data)
            with db_lock, db() as con:
                con.execute("INSERT INTO photos (id, dish, filename, created) VALUES (?,?,?,?)",
                            (pid, dish, filename, time.time()))
                row = con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            return self.send_json({"photo": photo_json(row)}, 201)

        return self.send_error(404)

    def do_DELETE(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/photos/"):
            return self.send_error(404)
        if not ADMIN_TOKEN or not secrets.compare_digest(
                self.headers.get("X-Admin-Token", ""), ADMIN_TOKEN):
            return self.send_json({"error": "not allowed"}, 403)
        pid = url.path.rsplit("/", 1)[1]
        with db_lock, db() as con:
            row = con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            if not row:
                return self.send_json({"error": "not found"}, 404)
            con.execute("DELETE FROM photos WHERE id=?", (pid,))
        try:
            (PHOTO_DIR / row["filename"]).unlink()
        except OSError:
            pass
        return self.send_json({"ok": True})


if __name__ == "__main__":
    print(f"The Hungry Games is serving on port {PORT}. Data in {DATA_DIR}.", flush=True)
    if not ADMIN_TOKEN:
        print("ADMIN_TOKEN is not set, so photos can't be deleted from the site.", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
