"""
Tally Party App - start here
----------------------------
    python app.py           start the app (opens in your browser)
    python app.py --test    check the connection to Tally and save Tally's answers for checking

Uses only the Python standard library. Python 3.8 or newer.
"""
import datetime as dt
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tally_connector as tc
import portal_upload as pu

BASE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))          # True inside the installed Windows app
RES_DIR = getattr(sys, "_MEIPASS", BASE)               # app.html, sample data
if FROZEN:
    USER_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "TallyPartyApp")
else:
    USER_DIR = BASE
os.makedirs(USER_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(USER_DIR, "config.json")
DEFAULT_CONFIG_PATH = os.path.join(RES_DIR, "config.default.json")
DATA_DIR = os.path.join(USER_DIR, "data")
DATA_PATH = os.path.join(DATA_DIR, "data.json")
SAMPLE_PATH = os.path.join(RES_DIR, "sample-data.json")
DEBUG_DIR = os.path.join(DATA_DIR, "tally-answers")
LOG_PATH = os.path.join(DATA_DIR, "sync-log.txt")

DEFAULTS = {
    "tally_url": "http://localhost:9000",
    "tally_company": "",
    "read_from_date": "",
    "your_company_name": "",
    "app_port": 8050,
    "open_on_other_computers": False,
    "auto_sync_minutes": 10,
    "tally_timeout_seconds": 120,
    "bank_details": "",
    "portal_enabled": False,
    "supabase_url": "",
    "supabase_publishable_key": "",
    "supabase_secret_key": "",
    "portal_history_months": 24,
    "portal_show_narration": False,
    "portal_allow_sample": False,
    "portal_website_url": "",
}

STATE = {"syncing": False, "last_error": None, "last_sync": None, "log": [],
         "portal_last": None, "portal_error": None, "portal_message": None}
LOCK = threading.Lock()
SERVER = {"httpd": None}
pu.STATE_PATH = os.path.join(DATA_DIR, "portal-state.json")

# Settings the app's Settings screen may change, with their type.
EDITABLE = {
    "tally_url": str, "tally_company": str, "read_from_date": str, "your_company_name": str,
    "app_port": int, "open_on_other_computers": bool, "auto_sync_minutes": int,
    "tally_timeout_seconds": int, "bank_details": str, "portal_enabled": bool,
    "supabase_url": str, "supabase_publishable_key": str, "supabase_secret_key": str,
    "portal_history_months": int, "portal_show_narration": bool, "portal_website_url": str,
}


def load_config():
    cfg = dict(DEFAULTS)
    if not os.path.exists(CONFIG_PATH) and os.path.exists(DEFAULT_CONFIG_PATH):
        try:
            shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)
        except OSError:
            pass
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # noqa: BLE001
            print("!! config.json could not be read (%s). Using default settings." % e)
    return cfg


def log(msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%d-%m-%Y %H:%M:%S"), msg)
    print(line)
    STATE["log"] = (STATE["log"] + [line])[-60:]
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def _secret_set(v):
    v = (v or "").strip()
    return bool(v) and not v.startswith("PASTE")


def public_config():
    cfg = load_config()
    out = {k: cfg.get(k, DEFAULTS.get(k)) for k in EDITABLE}
    key = cfg.get("supabase_secret_key", "")
    out["supabase_secret_key"] = ""
    out["secret_key_set"] = _secret_set(key)
    out["secret_key_hint"] = ("…" + key.strip()[-4:]) if _secret_set(key) else ""
    out["config_folder"] = USER_DIR
    out["installed_app"] = FROZEN
    return out


def update_config(body):
    cfg = load_config()
    for k, typ in EDITABLE.items():
        if k not in body:
            continue
        v = body[k]
        if k == "supabase_secret_key":
            v = (v or "").strip()
            if not v:
                continue                      # empty box = keep the saved key
        elif typ is bool:
            v = bool(v)
        elif typ is int:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError("'%s' must be a whole number." % k)
        elif typ is list:
            if isinstance(v, str):
                v = [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
            bad = [x for x in v if not pu.norm_phone(x)]
            if bad:
                raise ValueError("These admin mobile numbers are not valid: %s" % ", ".join(bad))
        else:
            v = "" if v is None else str(v).strip() if k != "bank_details" else str(v)
        cfg[k] = v
    pk = (cfg.get("supabase_publishable_key") or "").strip()
    if pk.startswith("sb_secret") or pu._jwt_role(pk) == "service_role":
        raise ValueError("The PUBLISHABLE key box has the secret key in it. Put the publishable key "
                         "(starts with sb_publishable_) there, and the secret key in the secret key box.")
    sk = (cfg.get("supabase_secret_key") or "").strip()
    if sk.startswith("sb_publishable") or pu._jwt_role(sk) == "anon":
        raise ValueError("The SECRET key box has the publishable key in it. Put the secret key "
                         "(starts with sb_secret_) there.")
    url = (cfg.get("supabase_url") or "").strip().rstrip("/")
    if url and not url.startswith("https://"):
        raise ValueError("The Supabase URL should start with https://")
    cfg["supabase_url"] = url
    web = (cfg.get("portal_website_url") or "").strip()
    if web and not web.startswith("https://"):
        raise ValueError("The portal website address should start with https://")
    if cfg.get("app_port", 8050) < 1024 or cfg.get("app_port", 8050) > 65535:
        raise ValueError("The app port must be between 1024 and 65535.")
    save_config(cfg)
    return cfg


def save_data(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DATA_PATH)


def run_sync():
    """Read Tally and save the data. Returns (ok, message)."""
    if not LOCK.acquire(blocking=False):
        return False, "A sync is already running."
    STATE["syncing"] = True
    try:
        cfg = load_config()
        data = tc.sync(cfg, progress=log)
        save_data(data)
        STATE["last_sync"] = data["synced_at"]
        STATE["last_error"] = None
        if cfg.get("portal_enabled"):
            portal_push(data, cfg)
        return True, "Synced %d parties from Tally." % data["counts"]["parties"]
    except tc.TallyError as e:
        STATE["last_error"] = str(e)
        log("Sync failed: %s" % e)
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        STATE["last_error"] = "Unexpected problem: %s" % e
        log("Sync failed with an unexpected problem:\n" + traceback.format_exc())
        return False, STATE["last_error"]
    finally:
        STATE["syncing"] = False
        LOCK.release()


def portal_push(data, cfg):
    try:
        msg = pu.upload(data, cfg, log)
        STATE["portal_last"] = dt.datetime.now().isoformat(timespec="seconds")
        STATE["portal_error"] = None
        STATE["portal_message"] = msg
        log(msg)
    except pu.PortalError as e:
        STATE["portal_error"] = str(e)
        log("Portal upload failed: %s" % e)
    except Exception as e:  # noqa: BLE001
        STATE["portal_error"] = "Unexpected problem: %s" % e
        log("Portal upload failed:\n" + traceback.format_exc())


def auto_sync_loop(_minutes=None):
    """Sync every auto_sync_minutes (read fresh each time, so Settings changes apply)."""
    last = time.time()
    while True:
        time.sleep(30)
        minutes = int(load_config().get("auto_sync_minutes", 10) or 0)
        if minutes <= 0 or time.time() - last < minutes * 60:
            continue
        last = time.time()
        if os.path.exists(DATA_PATH):
            try:
                with open(DATA_PATH, encoding="utf-8") as f:
                    if json.load(f).get("source") == "sample":
                        continue  # don't replace sample data unless asked
            except Exception:  # noqa: BLE001
                pass
        run_sync()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(RES_DIR, "app.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/data":
            if not os.path.exists(DATA_PATH):
                return self._send(404, {"error": "no data yet"})
            with open(DATA_PATH, "rb") as f:
                return self._send(200, f.read())
        if path == "/api/config":
            return self._send(200, public_config())
        if path == "/api/status":
            cfg = load_config()
            return self._send(200, {
                "syncing": STATE["syncing"],
                "last_error": STATE["last_error"],
                "last_sync": STATE["last_sync"],
                "log": STATE["log"][-12:],
                "company_name": cfg.get("your_company_name", ""),
                "bank_details": cfg.get("bank_details", ""),
                "auto_sync_minutes": cfg.get("auto_sync_minutes", 10),
                "sample_available": os.path.exists(SAMPLE_PATH),
                "installed_app": FROZEN,
                "portal_enabled": bool(cfg.get("portal_enabled")),
                "portal_last": STATE["portal_last"],
                "portal_error": STATE["portal_error"],
                "portal_message": STATE["portal_message"],
            })
        return self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _portal(self, fn):
        cfg = load_config()
        if not cfg.get("portal_enabled"):
            return self._send(400, {"ok": False, "message": "The online portal is not turned on. Turn on \"Upload to the online portal\" in Settings."})
        try:
            return self._send(200, dict(ok=True, **fn(cfg)))
        except pu.PortalError as e:
            return self._send(400, {"ok": False, "message": str(e)})
        except Exception as e:  # noqa: BLE001
            log("Portal login problem:\n" + traceback.format_exc())
            return self._send(500, {"ok": False, "message": "Unexpected problem: %s" % e})

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            if not self._local():
                return self._send(403, {"ok": False, "message": "Settings can only be changed on the computer running the app."})
            try:
                update_config(self._body())
            except ValueError as e:
                return self._send(400, {"ok": False, "message": str(e)})
            log("Settings saved.")
            return self._send(200, {"ok": True, "config": public_config()})
        if path == "/api/portal/upload":
            def up(cfg):
                if not os.path.exists(DATA_PATH):
                    raise pu.PortalError("There is no data yet. Sync from Tally first.")
                with open(DATA_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                cfg = dict(cfg, portal_allow_sample=True)      # pressed by hand: sample data is fine
                msg = pu.upload(data, cfg, log)
                STATE["portal_last"] = dt.datetime.now().isoformat(timespec="seconds")
                STATE["portal_error"] = None
                STATE["portal_message"] = msg
                log(msg)
                return {"message": msg}
            return self._portal(up)
        if path == "/api/quit":
            if not self._local():
                return self._send(403, {"ok": False})
            self._send(200, {"ok": True})
            log("App closed from Settings.")
            threading.Thread(target=lambda: (time.sleep(0.5), SERVER["httpd"].shutdown()), daemon=True).start()
            return None
        if path == "/api/portal/links":
            return self._portal(lambda cfg: dict(pu.list_links(cfg), website=cfg.get("portal_website_url", ""),
                                                 api=(cfg.get("supabase_url") or "").strip().rstrip("/"),
                                                 pkey=(cfg.get("supabase_publishable_key") or "").strip()))
        if path == "/api/portal/newlink":
            body = self._body()
            def make(cfg):
                if body.get("admin"):
                    tok = pu.renew_link(cfg, None)
                    log("Portal: new owner link made (the old one stopped working).")
                else:
                    tok = pu.renew_link(cfg, body.get("ckey") or "")
                    log("Portal: new private link made for %s" % body.get("ckey"))
                return {"token": tok}
            return self._portal(make)
        if path == "/api/sync":
            ok, msg = run_sync()
            return self._send(200 if ok else 500, {"ok": ok, "message": msg})
        if path == "/api/use-sample":
            if not os.path.exists(SAMPLE_PATH):
                return self._send(404, {"ok": False, "message": "sample-data.json is missing."})
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.copyfile(SAMPLE_PATH, DATA_PATH)
            log("Loaded the sample data.")
            return self._send(200, {"ok": True, "message": "Sample data loaded."})
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):  # keep the window quiet
        pass


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "this-computer's-IP"


def test_connection():
    cfg = load_config()
    print("=" * 64)
    print(" Tally connection test")
    print("=" * 64)
    if os.path.exists(DEBUG_DIR):
        shutil.rmtree(DEBUG_DIR, ignore_errors=True)
    try:
        data = tc.sync(cfg, progress=print, debug_dir=DEBUG_DIR)
    except tc.TallyError as e:
        print("\nPROBLEM: %s" % e)
        print("\nTally's answers (if any) were saved in: %s" % DEBUG_DIR)
        return 1
    save_data(data)
    dr = [p for p in data["parties"] if p["side"] == "dr"]
    cr = [p for p in data["parties"] if p["side"] == "cr"]
    def ind(n):
        s, neg = "%d" % round(abs(n)), n < 0
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        return ("-" if neg else "") + ",".join(parts + [tail])
    close = lambda p: p["opening"] + sum(e["delta"] for e in data["entries"] if e["party"] == p["id"])
    print("\nCustomers found : %d   total they owe you : Rs %s" % (len(dr), ind(sum(map(close, dr)))))
    print("Suppliers found : %d   total you owe them : Rs %s" % (len(cr), ind(sum(map(close, cr)))))
    print("Bills found     : %d" % data["counts"]["bills"])
    print("Entries found   : %d" % data["counts"]["entries"])
    for w in data["warnings"]:
        print("NOTE: " + w)
    print("\nSUCCESS. The data was saved, so the app will show it when you start it.")
    print("Tally's raw answers were saved in: %s" % DEBUG_DIR)
    return 0


def upload_now():
    """Sync from Tally (or use saved data) and upload to the portal once, with messages."""
    cfg = load_config()
    print("=" * 64)
    print(" Upload to the online portal")
    print("=" * 64)
    data = None
    try:
        data = tc.sync(cfg, progress=print)
        save_data(data)
    except tc.TallyError as e:
        print("Could not read Tally (%s)." % e)
        if os.path.exists(DATA_PATH):
            print("Using the data from the last successful sync instead.")
            with open(DATA_PATH, encoding="utf-8") as f:
                data = json.load(f)
    if data is None:
        print("PROBLEM: there is no data to upload.")
        return 1
    try:
        print(pu.upload(data, cfg, print))
    except pu.PortalError as e:
        print("\nPROBLEM: %s" % e)
        return 1
    except Exception as e:  # noqa: BLE001
        print("\nPROBLEM: %s" % e)
        print(traceback.format_exc())
        return 1
    print("\nSUCCESS. Open your portal website and log in to check.")
    return 0


def start_server():
    """Start the web server. Returns (server, url) or (None, url) if it is already running."""
    cfg = load_config()
    port = int(cfg.get("app_port", 8050))
    host = "0.0.0.0" if cfg.get("open_on_other_computers") else "127.0.0.1"
    url = "http://localhost:%d" % port
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        return None, url
    SERVER["httpd"] = server
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    if not os.path.exists(DATA_PATH):
        log("No data yet. Trying a first sync with Tally ...")
        threading.Thread(target=run_sync, daemon=True).start()
    if host == "0.0.0.0":
        log("Other computers in the office can open http://%s:%d" % (local_ip(), port))
    return server, url


def main():
    if "--test" in sys.argv:
        sys.exit(test_connection())
    if "--upload" in sys.argv:
        sys.exit(upload_now())

    server, url = start_server()
    if server is None:
        print("The app is already running (or port in use). Opening %s" % url)
        webbrowser.open(url)
        sys.exit(0)
    print("=" * 64)
    print(" Tally Party App is running")
    print(" Open: %s" % url)
    print(" Keep this window open. Close it to stop the app.")
    print("=" * 64)
    if load_config().get("portal_enabled"):
        print(" Online portal upload is ON (after every sync).")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
