# -*- coding: utf-8 -*-
import http.server
import socketserver
import webbrowser
import os
import re
import sys
import threading
import time
import json
import base64
import shutil
import subprocess
import urllib.parse
import socket
from datetime import datetime

PORT = 8000

APP_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(APP_DIR, "data")
POIS_FILE    = os.path.join(DATA_DIR, "pois.json")
FOLDERS_FILE = os.path.join(DATA_DIR, "folders.json")
ROUTES_FILE  = os.path.join(DATA_DIR, "routes.json")
MEDIA_DIR    = os.path.join(DATA_DIR, "media")
ADMIN_HTML   = os.path.join(APP_DIR,  "world_map_app.html")
VIEWER_HTML  = os.path.join(APP_DIR,  "viewer.html")

os.makedirs(MEDIA_DIR, exist_ok=True)

# Extensions the browser can play natively — served with HTTP range support
BROWSER_NATIVE = {".mp4", ".webm", ".ogg", ".ogv", ".m4v"}

# Extensions that need server-side transcoding (ffmpeg preferred, VLC fallback)
TRANSCODE_FORMATS = {
    ".avi", ".mkv", ".flv", ".wmv", ".mov", ".ts",
    ".m2ts", ".mts", ".3gp", ".rmvb", ".divx", ".xvid",
    ".mpg", ".mpeg", ".m2v", ".vob", ".asf", ".rm",
}

CHUNK_SIZE = 1024 * 1024  # 1 MB

MIME_MAP = {
    "html": "text/html; charset=utf-8",
    "css":  "text/css",
    "js":   "application/javascript",
    "jpg":  "image/jpeg",  "jpeg": "image/jpeg",
    "png":  "image/png",   "gif":  "image/gif",
    "webp": "image/webp",  "bmp":  "image/bmp",
    "mp4":  "video/mp4",   "webm": "video/webm",
    "ogg":  "video/ogg",   "ogv":  "video/ogg",
    "ico":  "image/x-icon",
}

# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------

def _find_tool(names):
    """Return path to first found executable from a list of names, or None."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    candidates = {
        "ffmpeg": ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"],
        "vlc":    [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
            "/Applications/VLC.app/Contents/MacOS/VLC",
            "/usr/bin/vlc", "/usr/local/bin/vlc",
        ],
    }
    for name in names:
        for path in candidates.get(name, []):
            if os.path.isfile(path):
                return path
    return None

def find_ffmpeg():
    return _find_tool(["ffmpeg"])

def find_vlc():
    return _find_tool(["vlc", "VLC"])

# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

def stream_native_ranged(filepath, range_header):
    """
    Generator that yields bytes for HTTP range request.
    Returns (status, headers_dict, generator).
    """
    file_size = os.path.getsize(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    mime_map = {
        ".mp4": "video/mp4", ".webm": "video/webm",
        ".ogg": "video/ogg", ".ogv": "video/ogg", ".m4v": "video/mp4",
    }
    mime = mime_map.get(ext, "video/mp4")

    if not range_header:
        def gen_full():
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        headers = {
            "Content-Type": mime,
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
        }
        return 200, headers, gen_full()

    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        return 416, {}, iter([])

    start = int(match.group(1))
    end   = int(match.group(2)) if match.group(2) else file_size - 1
    end   = min(end, file_size - 1)
    length = end - start + 1

    def gen_range():
        with open(filepath, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Type": mime,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
    }
    return 206, headers, gen_range()


# Cache duration results so repeated requests don't re-run ffmpeg
_duration_cache = {}

def get_duration(filepath):
    """Return video duration in seconds. Uses ffmpeg -i stderr parse (no ffprobe needed)."""
    filepath = str(filepath)
    if filepath in _duration_cache:
        return _duration_cache[filepath]

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    # Try ffprobe first (same dir as ffmpeg)
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.isfile(ffprobe):
        ffprobe = shutil.which("ffprobe")

    if ffprobe and os.path.isfile(ffprobe):
        try:
            result = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", str(filepath)],
                capture_output=True, timeout=15
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                dur = float(data.get("format", {}).get("duration", 0))
                if dur > 0:
                    _duration_cache[filepath] = dur
                    return dur
        except Exception:
            pass

    # Fallback: parse "Duration: HH:MM:SS.xx" from ffmpeg -i stderr
    try:
        result = subprocess.run(
            [ffmpeg, "-i", filepath],
            capture_output=True, timeout=15
        )
        # ffmpeg -i always "fails" (no output specified) but writes info to stderr
        stderr = result.stderr.decode("utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if dur > 0:
                _duration_cache[filepath] = dur
                return dur
    except Exception:
        pass

    return None


def build_transcode_process(filepath, seek=0):
    """
    Spawn ffmpeg (preferred) or VLC to transcode video to fMP4 on stdout.
    seek: start offset in seconds (0 = from beginning).
    Returns (process, mime) or raises RuntimeError if neither tool found.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        cmd = [ffmpeg]
        if seek and seek > 0:
            cmd += ["-ss", str(seek)]   # seek BEFORE -i for speed
        cmd += [
            "-i", filepath,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "frag_keyframe+empty_moov+faststart",
            "-f", "mp4",
            "pipe:1",
        ]
        label = f"{os.path.basename(filepath)}" + (f" @{seek}s" if seek else "")
        print(f"  [stream] ffmpeg -> {label}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=10 * CHUNK_SIZE,
        )
        return proc, "video/mp4"

    vlc = find_vlc()
    if vlc:
        cmd = [
            vlc,
            str(filepath),
            "--intf", "dummy",
            "--no-video-title-show",
            "--sout", (
                "#transcode{"
                "vcodec=h264,vb=2000,"
                "acodec=mp4a,ab=192,channels=2,samplerate=44100"
                "}:std{access=file,mux=mp4,dst=-}"
            ),
            "--sout-mp4-faststart",
            "vlc://quit",
        ]
        print(f"  [stream] VLC -> {os.path.basename(filepath)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=10 * CHUNK_SIZE,
            close_fds=(sys.platform != "win32"),
        )
        return proc, "video/mp4"

    raise RuntimeError(
        "Neither ffmpeg nor VLC is installed. "
        "Install ffmpeg: https://ffmpeg.org  or  VLC: https://videolan.org/vlc/"
    )

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] {path}: {e}")
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_pois():    return load_json(POIS_FILE,    [])
def load_folders(): return load_json(FOLDERS_FILE, [])
def load_routes():  return load_json(ROUTES_FILE,  [])
def new_id():       return int(time.time() * 1000)

# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    def get_path(self):
        path = urllib.parse.urlparse(self.path).path
        path = urllib.parse.unquote(path)
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, filepath):
        """Simple whole-file send for HTML/JSON etc."""
        ext  = os.path.splitext(filepath)[1].lower().lstrip(".")
        mime = MIME_MAP.get(ext, "application/octet-stream")
        try:
            data = open(filepath, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return None

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.get_path()

        # ── API ──
        if path == "/api/ping":
            self.send_json({
                "ok": True,
                "ffmpeg": bool(find_ffmpeg()),
                "vlc":    bool(find_vlc()),
            })
            return

        # /duration/<b64> — return video duration in seconds (ffprobe)
        if path.startswith("/duration/"):
            encoded = path[len("/duration/"):]
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                raw = base64.urlsafe_b64decode(padded)
                try:    key = raw.decode("utf-8")
                except: key = raw.decode("latin-1")
                if os.path.isabs(key):
                    filepath = os.path.abspath(key)
                else:
                    filepath = os.path.join(MEDIA_DIR, os.path.basename(key))
                if not os.path.isfile(filepath):
                    self.send_json({"duration": None, "error": "file not found"}); return
                dur = get_duration(filepath)
                self.send_json({"duration": dur})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # /api/scan/<b64path> — list video files in a server folder
        if path.startswith("/api/scan/"):
            encoded = path[len("/api/scan/"):]
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                folder = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                folder = os.path.abspath(folder)
                if not os.path.isdir(folder):
                    self.send_json({"error": f"Not a directory: {folder}"}, 404); return
                all_exts = BROWSER_NATIVE | TRANSCODE_FORMATS
                files = []
                for f in sorted(os.listdir(folder)):
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in all_exts: continue
                    fp = os.path.join(folder, f)
                    files.append({"name": f, "path": fp, "ext": ext,
                                  "size": os.path.getsize(fp)})
                self.send_json({"folder": folder, "count": len(files), "files": files})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # /api/browse/<b64path|"root"> — navigable directory listing
        if path.startswith("/api/browse/"):
            token = path[len("/api/browse/"):]
            try:
                all_exts = BROWSER_NATIVE | TRANSCODE_FORMATS
                if token == "root":
                    # Windows: return drive letters; Unix: return /
                    if sys.platform == "win32":
                        import string
                        drives = [d + ":\\" for d in string.ascii_uppercase
                                  if os.path.exists(d + ":\\")]
                        self.send_json({
                            "path": "", "parent": None,
                            "dirs": [{"name": d, "path": d} for d in drives],
                            "files": [],
                        })
                    else:
                        self.send_json({
                            "path": "/", "parent": None,
                            "dirs": self._browse_dirs("/"),
                            "files": self._browse_files("/", all_exts),
                        })
                    return
                padded = token + "=" * (-len(token) % 4)
                folder = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                if not os.path.isdir(folder):
                    self.send_json({"error": f"Not a directory: {folder}"}, 400); return
                parent = str(os.path.dirname(os.path.abspath(folder)))
                if parent == folder:
                    parent = None  # at root
                self.send_json({
                    "path": folder,
                    "parent": parent,
                    "dirs": self._browse_dirs(folder),
                    "files": self._browse_files(folder, all_exts),
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # /api/videos — list video files from MEDIA_DIR
        if path == "/api/videos":
            all_exts = BROWSER_NATIVE | TRANSCODE_FORMATS
            videos = []
            try:
                for f in sorted(os.listdir(MEDIA_DIR)):
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in all_exts:
                        continue
                    fp   = os.path.join(MEDIA_DIR, f)
                    size = os.path.getsize(fp)
                    b64  = base64.urlsafe_b64encode(f.encode()).decode().rstrip("=")
                    videos.append({
                        "name":       f,
                        "stem":       os.path.splitext(f)[0],
                        "ext":        ext,
                        "size_mb":    round(size / (1024 * 1024), 1),
                        "native":     ext in BROWSER_NATIVE,
                        "url":        f"/media/{f}",
                        "stream_url": f"/stream/{b64}",
                    })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                return
            self.send_json(videos)
            return

        if path == "/api/all":
            self.send_json({"folders": load_folders(), "pois": load_pois(), "routes": load_routes()})
            return
        if path == "/api/folders":
            self.send_json(load_folders()); return
        if path == "/api/pois":
            self.send_json(load_pois()); return
        if path == "/api/routes":
            self.send_json(load_routes()); return

        if path == "/api/is-local":
            client_ip = self.client_address[0]
            is_local  = client_ip in ("127.0.0.1", "::1", "localhost")
            self.send_json({"local": is_local})
            return

        # ── Video streaming endpoint ──
        # /stream/<base64url-encoded filename>
        if path.startswith("/stream/"):
            self._handle_stream(path[len("/stream/"):])
            return

        # ── Media files with range support ──
        if path.startswith("/media/"):
            fname    = os.path.basename(path)
            filepath = os.path.join(MEDIA_DIR, fname)
            if not os.path.isfile(filepath):
                self.send_response(404); self.end_headers(); return
            ext = os.path.splitext(filepath)[1].lower()
            if ext in TRANSCODE_FORMATS:
                # Redirect AVI/MKV etc to the stream endpoint
                b64 = base64.urlsafe_b64encode(fname.encode()).decode().rstrip("=")
                self.send_response(302)
                self.send_header("Location", f"/stream/{b64}")
                self.end_headers()
            else:
                self._send_ranged(filepath)
            return

        # ── Serve local file by base64url-encoded absolute path ──
        if path.startswith("/localfile/"):
            try:
                encoded  = path[len("/localfile/"):]
                padded   = encoded + "=" * (-len(encoded) % 4)
                raw      = base64.urlsafe_b64decode(padded)
                try:    local_path = raw.decode("utf-8")
                except: local_path = raw.decode("latin-1")
                local_path = os.path.abspath(local_path)
                if not os.path.isfile(local_path):
                    self.send_response(404); self.end_headers()
                    self.wfile.write(b"File not found on server"); return
                ext = os.path.splitext(local_path)[1].lower()
                if ext in TRANSCODE_FORMATS:
                    b64 = base64.urlsafe_b64encode(local_path.encode()).decode().rstrip("=")
                    self.send_response(302)
                    self.send_header("Location", f"/stream/{b64}")
                    self.end_headers()
                else:
                    self._send_ranged(local_path)
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode())
            return

        # ── HTML pages ──
        if path in ("/", "/viewer", "/viewer.html"):
            self.send_file(VIEWER_HTML); return
        if path in ("/admin", "/admin.html", "/world_map_app.html"):
            self.send_file(ADMIN_HTML); return

        # ── Static fallback ──
        safe = os.path.normpath(path.lstrip("/"))
        if ".." in safe:
            self.send_response(403); self.end_headers(); return
        fp = os.path.join(APP_DIR, safe)
        if os.path.isfile(fp):
            self.send_file(fp)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Not found: {safe}".encode())

    def _browse_dirs(self, folder):
        result = []
        try:
            for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
                if entry.is_dir(follow_symlinks=False):
                    result.append({"name": entry.name, "path": entry.path})
        except PermissionError:
            pass
        return result

    def _browse_files(self, folder, exts):
        result = []
        try:
            for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in exts:
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                result.append({"name": entry.name, "path": entry.path,
                                "ext": ext, "size": size})
        except PermissionError:
            pass
        return result

    def _send_ranged(self, filepath):
        """Serve a file with HTTP Range support — needed for native video seeking."""
        range_hdr = self.headers.get("Range")
        status, headers, gen = stream_native_ranged(filepath, range_hdr)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.cors()
        self.end_headers()
        try:
            for chunk in gen:
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _handle_stream(self, encoded):
        """
        Stream a video file:
        - Native formats (mp4/webm/ogg): HTTP range streaming, fully seekable.
        - AVI/MKV/etc: transcode to fMP4 via ffmpeg (preferred) or VLC, stream stdout.
          Supports ?seek=<seconds> query param to start from an offset.

        The encoded segment is base64url of:
          - A bare filename (file lives in MEDIA_DIR)
          - An absolute path
        """
        try:
            # Parse seek query param from the full self.path
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            seek = 0
            try:
                seek = float(params.get("seek", ["0"])[0])
            except (ValueError, IndexError):
                seek = 0

            padded = encoded + "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(padded)
            try:    key = raw.decode("utf-8")
            except: key = raw.decode("latin-1")

            # Resolve to absolute path
            if os.path.isabs(key):
                filepath = os.path.abspath(key)
            else:
                filepath = os.path.join(MEDIA_DIR, os.path.basename(key))

            if not os.path.isfile(filepath):
                self.send_response(404); self.end_headers()
                self.wfile.write(b"File not found"); return

            ext = os.path.splitext(filepath)[1].lower()

            # ── Native formats: range-based streaming (seekable) ──
            if ext in BROWSER_NATIVE:
                self._send_ranged(filepath)
                return

            # ── Non-native: transcode to fMP4 stream ──
            if ext not in TRANSCODE_FORMATS:
                self.send_response(415); self.end_headers()
                self.wfile.write(f"Unsupported format: {ext}".encode()); return

            try:
                proc, mime = build_transcode_process(filepath, seek=seek)
            except RuntimeError as e:
                self.send_response(503); self.end_headers()
                self.wfile.write(str(e).encode()); return

            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Accept-Ranges", "none")
            self.cors()
            self.end_headers()

            try:
                while True:
                    chunk = proc.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                proc.kill()
                proc.wait()

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            print(f"  [stream ERROR] {e}")
            try:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode())
            except Exception:
                pass

    # ── POST ───────────────────────────────────────────────────────────────
    def do_POST(self):
        path = self.get_path()
        ct   = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        # For multipart routes, parse later; for everything else parse JSON now
        if "multipart/form-data" not in ct:
            try:
                body = json.loads(raw_body) if raw_body else {}
            except Exception:
                self.send_json({"error": "Invalid JSON"}, 400); return
        else:
            body = None  # will be parsed per-endpoint below

        if path == "/api/folders":
            name = (body.get("name") or "").strip()
            if not name: self.send_json({"error": "Missing name"}, 400); return
            folder = {"id": new_id(), "name": name, "color": body.get("color", "#c0392b"),
                      "created_at": datetime.now().isoformat()}
            folders = load_folders(); folders.append(folder); save_json(FOLDERS_FILE, folders)
            self.send_json({"success": True, "folder": folder}, 201); return

        # /api/link — copy a server-side file into the media folder
        if path == "/api/link":
            src_path = body.get("path", "")
            name     = body.get("name", os.path.basename(src_path))
            if not src_path or not os.path.isfile(src_path):
                self.send_json({"error": f"File not found: {src_path}"}, 404); return
            safe  = "".join(c for c in name if c.isalnum() or c in "._-")
            fname = f"{new_id()}_{safe}"
            dst   = os.path.join(MEDIA_DIR, fname)
            try:
                shutil.copy2(src_path, dst)
                ext = os.path.splitext(name)[1].lower()
                media = {"name": name, "type": f"video/{ext.lstrip('.')}", "filename": fname,
                         "url": f"/media/{fname}"}
                self.send_json({"success": True, "media": media})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if path == "/api/pois":
            # Accept both multipart/form-data (from admin) and application/json
            if "multipart/form-data" in ct:
                import email.parser
                # Build a fake email message for multipart parsing (raw_body already read above)
                msg_str = f"Content-Type: {ct}\r\n\r\n".encode() + raw_body
                msg = email.parser.BytesParser().parsebytes(msg_str)
                fields = {}
                saved_media = []
                for part in msg.walk():
                    if part.get_content_maintype() == "multipart": continue
                    cd = str(part.get("Content-Disposition", ""))
                    if not cd: continue
                    # Extract name and filename
                    name_m    = __import__("re").search(r'name="([^"]*)"', cd)
                    fname_m   = __import__("re").search(r'filename="([^"]*)"', cd)
                    field_name = name_m.group(1) if name_m else ""
                    fname_orig = fname_m.group(1) if fname_m else ""
                    payload = part.get_payload(decode=True)
                    if payload is None: continue
                    if fname_orig:
                        # It's a file upload
                        safe = "".join(c for c in fname_orig if c.isalnum() or c in "._-")
                        saved_fname = f"{new_id()}_{safe}"
                        with open(os.path.join(MEDIA_DIR, saved_fname), "wb") as mf:
                            mf.write(payload)
                        mime = part.get_content_type() or "application/octet-stream"
                        saved_media.append({"name": fname_orig, "type": mime,
                                            "filename": saved_fname, "url": f"/media/{saved_fname}"})
                    else:
                        fields[field_name] = payload.decode("utf-8", errors="replace")
                # linked files (path-only references, already on server)
                try:
                    linked = json.loads(fields.get("linked", "[]"))
                    for lf in linked:
                        src = lf.get("path", "")
                        if src and os.path.isfile(src):
                            safe = "".join(c for c in lf["name"] if c.isalnum() or c in "._-")
                            lfname = f"{new_id()}_{safe}"
                            # Don't copy — store local_path reference
                            ext = os.path.splitext(lf["name"])[1].lower()
                            saved_media.append({"name": lf["name"], "type": f"video/{ext.lstrip('.')}",
                                                "filename": lfname, "local_path": src,
                                                "url": f"/stream/{base64.urlsafe_b64encode(src.encode()).decode().rstrip('=')}"})
                except Exception as e:
                    print(f"  [WARN] linked: {e}")
                # pre_copied files (already moved to media dir by /api/link)
                try:
                    pre = json.loads(fields.get("pre_copied", "[]"))
                    saved_media.extend(pre)
                except Exception as e:
                    print(f"  [WARN] pre_copied: {e}")
                name = fields.get("name","").strip()
                lat_s = fields.get("lat",""); lng_s = fields.get("lng","")
                date  = fields.get("date","").strip()
                desc  = fields.get("description","").strip()
                fid_s = fields.get("folder_id","")
                for field, val in [("name",name),("lat",lat_s),("lng",lng_s),("date",date)]:
                    if not val:
                        self.send_json({"error": f"Missing field: {field}"}, 400); return
                try: lat = float(lat_s); lng = float(lng_s)
                except ValueError:
                    self.send_json({"error": "Invalid lat/lng"}, 400); return
                fid = int(fid_s) if fid_s.strip().isdigit() else None
            else:
                # JSON path (backward compat)
                if body is None:
                    self.send_json({"error": "Invalid JSON"}, 400); return
                for field in ("name", "lat", "lng", "description", "date"):
                    if not body.get(field) and body.get(field) != 0:
                        self.send_json({"error": f"Missing field: {field}"}, 400); return
                saved_media = []
                for item in body.get("media", []):
                    try:
                        data_url = item.get("data", "")
                        if "," not in data_url: continue
                        _, b64 = data_url.split(",", 1)
                        raw  = base64.b64decode(b64)
                        safe = "".join(c for c in item["name"] if c.isalnum() or c in "._-")
                        fname = f"{new_id()}_{safe}"
                        with open(os.path.join(MEDIA_DIR, fname), "wb") as mf:
                            mf.write(raw)
                        saved_media.append({"name": item["name"], "type": item.get("type", ""),
                                            "filename": fname, "url": f"/media/{fname}"})
                    except Exception as e:
                        print(f"  [WARN] Media: {e}")
                name = str(body["name"]); lat = float(body["lat"]); lng = float(body["lng"])
                date = str(body["date"]); desc = str(body.get("description",""))
                fid  = body.get("folder_id")

            poi = {"id": new_id(), "folder_id": fid,
                   "name": name, "lat": lat, "lng": lng,
                   "description": desc, "date": date,
                   "media": saved_media, "created_at": datetime.now().isoformat()}
            pois = load_pois(); pois.append(poi); save_json(POIS_FILE, pois)
            print(f"  [OK] POI: '{poi['name']}' ({len(saved_media)} media)")
            self.send_json({"success": True, "poi": poi}, 201); return

        if path == "/api/routes":
            name = (body.get("name") or "").strip()
            if not name: self.send_json({"error": "Missing name"}, 400); return
            route = {"id": new_id(), "folder_id": body.get("folder_id"), "name": name,
                     "color": body.get("color", "#2980b9"), "points": body.get("points", []),
                     "created_at": datetime.now().isoformat()}
            routes = load_routes(); routes.append(route); save_json(ROUTES_FILE, routes)
            self.send_json({"success": True, "route": route}, 201); return

        # ── /api/play — launch VLC on server to open a file locally ──
        if path == "/api/play":
            file_path = body.get("local_path", "") or ""
            filename  = body.get("filename",   "") or ""
            if not file_path and filename:
                file_path = os.path.join(MEDIA_DIR, filename)
            if not file_path or not os.path.isfile(file_path):
                self.send_json({"error": f"File not found: {file_path}"}, 404); return
            vlc = find_vlc()
            if not vlc:
                self.send_json({"error": "VLC not found on this server"}, 503); return
            try:
                subprocess.Popen(
                    [vlc, file_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=(sys.platform != "win32")
                )
                print(f"  [VLC] Opened: {file_path}")
                self.send_json({"success": True, "vlc": vlc})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # ── /api/relink — bulk path-prefix replacement across all POIs ──
        if path == "/api/relink":
            old_prefix = (body.get("old_prefix") or "").rstrip("/\\")
            new_prefix = (body.get("new_prefix") or "").rstrip("/\\")
            if not old_prefix or not new_prefix:
                self.send_json({"error": "Both old_prefix and new_prefix required"}, 400); return
            pois = load_pois()
            updated = missing = skipped = 0
            for p in pois:
                changed = False
                for m in p.get("media", []):
                    lp = m.get("local_path", "")
                    if lp and lp.startswith(old_prefix):
                        new_path = new_prefix + lp[len(old_prefix):]
                        m["local_path"] = new_path
                        # Update stream URL to match new path
                        b64 = base64.urlsafe_b64encode(new_path.encode()).decode().rstrip("=")
                        m["url"] = f"/stream/{b64}"
                        if os.path.isfile(new_path):
                            updated += 1
                        else:
                            missing += 1
                        changed = True
                    elif lp:
                        skipped += 1
                if changed:
                    p["updated_at"] = datetime.now().isoformat()
            save_json(POIS_FILE, pois)
            print(f"  [relink] updated={updated} missing={missing} skipped={skipped}")
            self.send_json({"success": True, "updated": updated,
                            "missing": missing, "skipped": skipped}); return

        # ── /api/pois/<id>/relink — update a single linked file's path ──
        m = re.match(r"^/api/pois/(\d+)/relink$", path)
        if m:
            try:
                poi_id   = int(m.group(1))
                old_key  = body.get("old_key", "")
                new_path = body.get("new_path", "")
                new_name = body.get("new_name", "")
                if not new_path:
                    self.send_json({"error": "new_path required"}, 400); return
                pois = load_pois()
                found = False
                for p in pois:
                    if p["id"] != poi_id:
                        continue
                    for med in p.get("media", []):
                        if (med.get("local_path", "") == old_key or
                                med.get("filename", "") == old_key or
                                med.get("name", "") == old_key):
                            med["local_path"] = new_path
                            if new_name:
                                med["name"] = new_name
                            ext = os.path.splitext(new_path)[1].lower()
                            med["type"] = f"video/{ext.lstrip('.')}"
                            b64 = base64.urlsafe_b64encode(new_path.encode()).decode().rstrip("=")
                            med["url"] = f"/stream/{b64}"
                            found = True
                            break
                    if found:
                        p["updated_at"] = datetime.now().isoformat()
                        break
                if not found:
                    self.send_json({"error": "Media item not found on this POI"}, 404); return
                save_json(POIS_FILE, pois)
                poi = next((p for p in pois if p["id"] == poi_id), None)
                print(f"  [relink] poi={poi_id} -> {new_path}")
                self.send_json({"success": True, "poi": poi}); return
            except Exception as e:
                self.send_json({"error": str(e)}, 500); return

        # ── /api/transcode — convert video to MP4 permanently via ffmpeg ──
        if path == "/api/transcode":
            poi_id   = body.get("poi_id")
            src_key  = body.get("src_key", "")
            src_name = body.get("src_name", "")
            src_path = None
            if src_key and os.path.isfile(os.path.join(MEDIA_DIR, os.path.basename(src_key))):
                src_path = os.path.join(MEDIA_DIR, os.path.basename(src_key))
            elif src_key and os.path.isfile(src_key):
                src_path = src_key
            if not src_path:
                self.send_json({"error": f"Source not found: {src_key}"}, 404); return
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                self.send_json({"error": "ffmpeg not installed. Download from https://ffmpeg.org"}, 503); return
            base   = os.path.splitext(src_name or os.path.basename(src_path))[0]
            safe_b = "".join(c for c in base if c.isalnum() or c in "._-")
            out_fn = f"{new_id()}_{safe_b}.mp4"
            out_p  = os.path.join(MEDIA_DIR, out_fn)
            print(f"  [transcode] {src_path} -> {out_fn}")
            try:
                cmd = [
                    ffmpeg, "-y", "-i", src_path,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    out_p,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=3600)
                if result.returncode != 0:
                    err = result.stderr.decode("utf-8", errors="replace")[-300:]
                    self.send_json({"error": f"ffmpeg failed: {err}"}, 500); return
            except subprocess.TimeoutExpired:
                self.send_json({"error": "Transcode timed out"}, 500); return
            new_entry = {"name": base + ".mp4", "type": "video/mp4",
                         "filename": out_fn, "url": f"/media/{out_fn}",
                         "transcoded_from": src_name}
            if poi_id:
                pois = load_pois()
                for p in pois:
                    if p["id"] == int(poi_id):
                        p.setdefault("media", []).append(new_entry)
                        p["updated_at"] = datetime.now().isoformat()
                        save_json(POIS_FILE, pois)
                        self.send_json({"success": True, "media": new_entry, "poi": p}); return
            self.send_json({"success": True, "media": new_entry}); return

        self.send_json({"error": "Not found"}, 404)

    # ── PUT ────────────────────────────────────────────────────────────────
    def do_PUT(self):
        path = self.get_path()
        body = self.read_body()
        if body is None: self.send_json({"error": "Invalid JSON"}, 400); return

        if path.startswith("/api/pois/"):
            try: pid = int(path.split("/")[-1])
            except ValueError: self.send_json({"error": "Invalid id"}, 400); return
            pois = load_pois()
            for p in pois:
                if p["id"] == pid:
                    for k in ("name", "description", "date", "folder_id"):
                        if k in body: p[k] = body[k]
                    if "lat" in body: p["lat"] = float(body["lat"])
                    if "lng" in body: p["lng"] = float(body["lng"])
                    p["updated_at"] = datetime.now().isoformat()
                    save_json(POIS_FILE, pois)
                    print(f"  [OK] Updated POI id={pid}")
                    self.send_json({"success": True, "poi": p}); return
            self.send_json({"error": "Not found"}, 404); return

        if path.startswith("/api/routes/"):
            try: rid = int(path.split("/")[-1])
            except ValueError: self.send_json({"error": "Invalid id"}, 400); return
            routes = load_routes()
            for r in routes:
                if r["id"] == rid:
                    for k in ("points", "name", "color"):
                        if k in body: r[k] = body[k]
                    save_json(ROUTES_FILE, routes)
                    self.send_json({"success": True, "route": r}); return
            self.send_json({"error": "Not found"}, 404); return

        self.send_json({"error": "Not found"}, 404)

    # ── DELETE ─────────────────────────────────────────────────────────────
    def do_DELETE(self):
        path = self.get_path()

        if path.startswith("/api/folders/"):
            try: fid = int(path.split("/")[-1])
            except ValueError: self.send_json({"error": "Invalid id"}, 400); return
            folders = load_folders()
            new_f = [f for f in folders if f["id"] != fid]
            if len(new_f) == len(folders): self.send_json({"error": "Not found"}, 404); return
            save_json(FOLDERS_FILE, new_f)
            pois = load_pois()
            for p in pois:
                if p.get("folder_id") == fid: p["folder_id"] = None
            save_json(POIS_FILE, pois)
            routes = load_routes()
            for r in routes:
                if r.get("folder_id") == fid: r["folder_id"] = None
            save_json(ROUTES_FILE, routes)
            self.send_json({"success": True}); return

        if path.startswith("/api/pois/"):
            try: pid = int(path.split("/")[-1])
            except ValueError: self.send_json({"error": "Invalid id"}, 400); return
            pois = load_pois()
            new_p = [p for p in pois if p["id"] != pid]
            if len(new_p) == len(pois): self.send_json({"error": "Not found"}, 404); return
            save_json(POIS_FILE, new_p)
            self.send_json({"success": True}); return

        if path.startswith("/api/routes/"):
            try: rid = int(path.split("/")[-1])
            except ValueError: self.send_json({"error": "Invalid id"}, 400); return
            routes = load_routes()
            new_r = [r for r in routes if r["id"] != rid]
            if len(new_r) == len(routes): self.send_json({"error": "Not found"}, 404); return
            save_json(ROUTES_FILE, new_r)
            self.send_json({"success": True}); return

        self.send_json({"error": "Not found"}, 404)


# ---------------------------------------------------------------------------
def run_server():
    socketserver.TCPServer.allow_reuse_address = True
    try:
        class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
        httpd = ThreadedServer(("", PORT), Handler)
    except OSError as e:
        print(f"\n[ERROR] Port {PORT} in use: {e}")
        sys.exit(1)

    ffmpeg_path = find_ffmpeg()
    vlc_path    = find_vlc()

    with httpd:
        print("=" * 55)
        print("  Travel Archive Server")
        print(f"  Viewer : http://localhost:{PORT}/")
        print(f"  Admin  : http://localhost:{PORT}/admin")
        print(f"  Data   : {DATA_DIR}")
        print(f"  ffmpeg : {ffmpeg_path or 'NOT FOUND — install ffmpeg for AVI/MKV streaming'}")
        print(f"  VLC    : {vlc_path or '(fallback, not found)'}")
        print("=" * 55)
        print("  Press Ctrl+C to stop\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[INFO] Stopped.")

if __name__ == "__main__":
    def _open():
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{PORT}/admin")
    threading.Thread(target=_open, daemon=True).start()
    run_server()