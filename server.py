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
import struct
import time
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static")).resolve()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "host")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024
THUMB_PX = 1200          # gallery previews
MAIN_PREVIEW_PX = 2400   # the big photo at the top of a dish page
SESSION_DAYS = 30
COOKIE = "hg_session"

try:  # Pillow makes small previews for the galleries. Originals are always kept as uploaded.
    from PIL import Image, ImageOps
except ImportError:  # still works without it: galleries just show the originals
    Image = None

try:  # reportlab builds the recipe PDFs and the QR code sheet
    import pdfs
except ImportError:
    pdfs = None
UPLOADS_PER_10_MIN = int(os.environ.get("UPLOADS_PER_10_MIN", "40"))

# The menu (names, stories, recipes) lives in static/dishes.json, shared with the page.
DISHES = json.loads((STATIC_DIR / "dishes.json").read_text(encoding="utf-8"))
DISH_BY_ID = {d["id"]: d for d in DISHES}
DISH_IDS = set(DISH_BY_ID)
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
    if "thumb" not in [r[1] for r in con.execute("PRAGMA table_info(photos)")]:
        con.execute("ALTER TABLE photos ADD COLUMN thumb TEXT")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS main_photos (
            dish TEXT PRIMARY KEY, filename TEXT NOT NULL, preview TEXT, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, created REAL NOT NULL);
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


def scrub_gps(data: bytes) -> bytes:
    """Blank out GPS location in a JPEG's EXIF, in place. Nothing else changes:
    the image itself is not re-encoded, so quality is untouched."""
    if data[:2] != b"\xff\xd8":
        return data
    buf = bytearray(data)
    i = 2
    while i + 4 <= len(buf) and buf[i] == 0xFF:
        marker = buf[i + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of image data
            break
        seg_len = struct.unpack(">H", buf[i + 2:i + 4])[0]
        start, end = i + 4, i + 2 + seg_len
        if marker == 0xE1 and buf[start:start + 6] == b"Exif\x00\x00":
            try:
                _blank_gps(buf, start + 6, end)
            except (struct.error, IndexError):
                pass
        i = end
    return bytes(buf)


def _blank_gps(buf: bytearray, tiff: int, limit: int):
    bo = "<" if buf[tiff:tiff + 2] == b"II" else ">"
    u16 = lambda o: struct.unpack(bo + "H", buf[o:o + 2])[0]
    u32 = lambda o: struct.unpack(bo + "I", buf[o:o + 4])[0]
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
    ifd0 = tiff + u32(tiff + 4)
    for k in range(u16(ifd0)):
        entry = ifd0 + 2 + 12 * k
        if u16(entry) != 0x8825:  # GPSInfo pointer
            continue
        gps = tiff + u32(entry + 8)
        for j in range(u16(gps)):
            e = gps + 2 + 12 * j
            nbytes = sizes.get(u16(e + 2), 1) * u32(e + 4)
            if nbytes <= 4:
                buf[e + 8:e + 12] = b"\x00" * 4
            else:
                off = tiff + u32(e + 8)
                if off + nbytes <= limit:
                    buf[off:off + nbytes] = b"\x00" * nbytes
        buf[gps:gps + 2] = b"\x00\x00"  # GPS directory now lists no entries


def make_thumb(src: Path, dest: Path, px: int = THUMB_PX) -> bool:
    if Image is None:
        return False
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((px, px))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dest, "JPEG", quality=82, optimize=True)
        return True
    except Exception as exc:  # odd file: fall back to showing the original
        print(f"Couldn't make a preview for {src.name}: {exc}", flush=True)
        return False


class BadImage(Exception):
    pass


def save_image(data: bytes, preview_px: int = THUMB_PX):
    """Validate and store an uploaded image at full quality. Returns (filename, preview or None)."""
    ext = sniff(data)
    if not ext:
        raise BadImage("That file isn't a JPEG, PNG, WebP or GIF image. "
                       "(iPhone: Settings → Camera → Formats → Most Compatible.)")
    if ext == "jpg":
        data = scrub_gps(data)
    pid = secrets.token_urlsafe(12)
    filename = f"{pid}.{ext}"
    (PHOTO_DIR / filename).write_bytes(data)
    preview = None
    if ext != "gif":  # keep GIFs animated
        preview = f"{pid}_t.jpg"
        if not make_thumb(PHOTO_DIR / filename, PHOTO_DIR / preview, preview_px):
            preview = None
    return pid, filename, preview


def remove_files(*names):
    for name in names:
        if name:
            try:
                (PHOTO_DIR / name).unlink()
            except OSError:
                pass


login_log = {}
login_lock = threading.Lock()


def allowed_login(ip: str) -> bool:
    now = time.time()
    with login_lock:
        log = [t for t in login_log.get(ip, []) if now - t < 600]
        log.append(now)
        login_log[ip] = log
        return len(log) <= 10


def main_photo_path(dish_id: str):
    """The dish's main photo for PDFs: the host's upload, then static/dish-photos, then newest guest photo."""
    with db() as con:
        r = con.execute("SELECT * FROM main_photos WHERE dish=?", (dish_id,)).fetchone()
        if r:
            return PHOTO_DIR / (r["preview"] or r["filename"])
        folder = STATIC_DIR / "dish-photos"
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            if (folder / (dish_id + ext)).is_file():
                return folder / (dish_id + ext)
        r = con.execute("SELECT * FROM photos WHERE dish=? ORDER BY created DESC LIMIT 1", (dish_id,)).fetchone()
        if r:
            return PHOTO_DIR / (r["thumb"] or r["filename"])
    return None


def pdf_image_bytes(path):
    if not path:
        return None
    try:
        if Image is None:
            return path.read_bytes()
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1800, 1800))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = BytesIO()
            im.save(out, "JPEG", quality=85)
            return out.getvalue()
    except Exception:
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
    url = "/photos/" + row["filename"]
    return {"id": row["id"], "dish": row["dish"], "url": url,
            "thumb": "/photos/" + row["thumb"] if row["thumb"] else url,
            "created": row["created"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "HungryGames/1.0"

    # ---------- helpers ----------
    def send_json(self, obj, status=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_pdf(self, data: bytes, filename: str, inline=False):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def site_base(self):
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"localhost:{PORT}"
        return f"{'https' if proto == 'https' else 'http'}://{host.split(',')[0].strip()}"

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

    def session_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def is_admin(self):
        tok = self.session_token()
        if not tok or not ADMIN_PASSWORD:
            return False
        with db() as con:
            row = con.execute("SELECT created FROM sessions WHERE token=?", (tok,)).fetchone()
        return bool(row) and time.time() - row["created"] < SESSION_DAYS * 86400

    def admin_write_ok(self):
        # Logged in, and the request came from the site's own page (blocks cross-site forgery).
        return self.headers.get("X-HG") == "1" and self.is_admin()

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
            # Main photo per dish: one the host uploaded on the site wins, then a file
            # dropped into static/dish-photos/<dish id>.jpg (or .png/.webp).
            out, set_on_site = {}, []
            folder = STATIC_DIR / "dish-photos"
            if folder.is_dir():
                for f in sorted(folder.iterdir()):
                    if f.stem in DISH_IDS and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                        out[f.stem] = {"preview": "dish-photos/" + f.name, "full": "dish-photos/" + f.name}
            with db() as con:
                for r in con.execute("SELECT * FROM main_photos"):
                    full = "photos/" + r["filename"]
                    out[r["dish"]] = {"preview": "photos/" + r["preview"] if r["preview"] else full, "full": full}
                    set_on_site.append(r["dish"])
            return self.send_json({"photos": out, "setOnSite": set_on_site})

        if p == "/api/me":
            return self.send_json({"admin": self.is_admin(), "loginEnabled": bool(ADMIN_PASSWORD)})

        m = re.match(r"^/recipes/([a-z0-9-]+)\.pdf$", p)
        if m:
            dish = DISH_BY_ID.get(m.group(1))
            if not dish:
                return self.send_error(404)
            if pdfs is None:
                return self.send_error(503, "PDFs need reportlab")
            number = DISHES.index(dish) + 1
            data = pdfs.recipe_pdf(dish, number, pdf_image_bytes(main_photo_path(dish["id"])))
            return self.send_pdf(data, f"{dish['id']}-recipe.pdf")

        if p == "/qr-sheet.pdf":
            if pdfs is None:
                return self.send_error(503, "PDFs need reportlab")
            base = (q.get("base") or [""])[0].strip() or self.site_base()
            if not re.match(r"^https?://[A-Za-z0-9.\-:\[\]]{1,200}(/[\w\-./]*)?$", base):
                return self.send_error(400, "base must look like http://192.168.1.20:8080")
            return self.send_pdf(pdfs.qr_sheet(DISHES, base), "hungry-games-qr-codes.pdf", inline=True)

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

        if url.path == "/api/login":
            if not ADMIN_PASSWORD:
                return self.send_json({"error": "Host login is off. Set ADMIN_PASSWORD in .env and restart."}, 403)
            if not allowed_login(self.client_ip()):
                return self.send_json({"error": "Too many tries. Wait a few minutes."}, 429)
            try:
                d = json.loads(self.read_body(4096) or b"")
                user, pw = str(d["username"]), str(d["password"])
            except (ValueError, KeyError, TypeError):
                return self.send_json({"error": "bad request"}, 400)
            ok_user = secrets.compare_digest(user.strip().lower().encode(), ADMIN_USERNAME.strip().lower().encode())
            ok_pw = secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())
            if not (ok_user and ok_pw):
                return self.send_json({"error": "Wrong username or password."}, 401)
            tok = secrets.token_urlsafe(32)
            with db_lock, db() as con:
                con.execute("DELETE FROM sessions WHERE created < ?", (time.time() - SESSION_DAYS * 86400,))
                con.execute("INSERT INTO sessions (token, created) VALUES (?, ?)", (tok, time.time()))
            cookie = f"{COOKIE}={tok}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"
            return self.send_json({"ok": True}, cookie=cookie)

        if url.path == "/api/logout":
            tok = self.session_token()
            if tok:
                with db_lock, db() as con:
                    con.execute("DELETE FROM sessions WHERE token=?", (tok,))
            return self.send_json({"ok": True}, cookie=f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")

        if url.path == "/api/main-photo":
            if not self.admin_write_ok():
                return self.send_json({"error": "Sign in as the host first."}, 403)
            dish = (q.get("dish") or [""])[0]
            if dish not in DISH_IDS:
                return self.send_json({"error": "unknown dish"}, 400)
            data = self.read_body(MAX_UPLOAD)
            if data is None:
                return self.send_json({"error": f"Photos must be under {MAX_UPLOAD // 1048576} MB."}, 413)
            try:
                _, filename, preview = save_image(data, MAIN_PREVIEW_PX)
            except BadImage as e:
                return self.send_json({"error": str(e)}, 415)
            with db_lock, db() as con:
                old = con.execute("SELECT * FROM main_photos WHERE dish=?", (dish,)).fetchone()
                con.execute("INSERT INTO main_photos (dish, filename, preview, updated) VALUES (?,?,?,?) "
                            "ON CONFLICT(dish) DO UPDATE SET filename=excluded.filename, "
                            "preview=excluded.preview, updated=excluded.updated",
                            (dish, filename, preview, time.time()))
            if old:
                remove_files(old["filename"], old["preview"])
            return self.send_json({"ok": True}, 201)

        if url.path == "/api/photos":
            dish = (q.get("dish") or ["general"])[0]
            if dish not in PHOTO_TAGS:
                return self.send_json({"error": "unknown dish"}, 400)
            if not allowed_upload(self.client_ip()):
                return self.send_json({"error": "Too many uploads. Try again in a few minutes."}, 429)
            data = self.read_body(MAX_UPLOAD)
            if data is None:
                return self.send_json({"error": f"Photos must be under {MAX_UPLOAD // 1048576} MB."}, 413)
            try:
                pid, filename, thumb = save_image(data)
            except BadImage as e:
                return self.send_json({"error": str(e)}, 415)
            with db_lock, db() as con:
                con.execute("INSERT INTO photos (id, dish, filename, created, thumb) VALUES (?,?,?,?,?)",
                            (pid, dish, filename, time.time(), thumb))
                row = con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            return self.send_json({"photo": photo_json(row)}, 201)

        return self.send_error(404)

    def do_DELETE(self):
        url = urlparse(self.path)
        if not url.path.startswith(("/api/photos/", "/api/main-photo/")):
            return self.send_error(404)
        if not self.admin_write_ok():
            return self.send_json({"error": "Sign in as the host first."}, 403)
        key = url.path.rsplit("/", 1)[1]
        if url.path.startswith("/api/main-photo/"):
            with db_lock, db() as con:
                row = con.execute("SELECT * FROM main_photos WHERE dish=?", (key,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                con.execute("DELETE FROM main_photos WHERE dish=?", (key,))
            remove_files(row["filename"], row["preview"])
            return self.send_json({"ok": True})
        pid = key
        with db_lock, db() as con:
            row = con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            if not row:
                return self.send_json({"error": "not found"}, 404)
            con.execute("DELETE FROM photos WHERE id=?", (pid,))
        remove_files(row["filename"], row["thumb"])
        return self.send_json({"ok": True})


if __name__ == "__main__":
    print(f"The Hungry Games is serving on port {PORT}. Data in {DATA_DIR}.", flush=True)
    if not ADMIN_PASSWORD:
        print("ADMIN_PASSWORD is not set, so host login is off. Set it in .env to manage photos.", flush=True)
    else:
        print(f"Host login is on. Username: {ADMIN_USERNAME}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
