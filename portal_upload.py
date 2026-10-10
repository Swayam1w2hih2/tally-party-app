"""
Portal upload (Supabase, private links)
---------------------------------------
After each sync, sends data to the online customer portal in Supabase.

Tables (created by link-portal-setup.sql):
  lp_accounts    one row per customer with ONLY that customer's bills, payments, statement
  lp_links       the private link codes: one per customer, plus the admin link
  lp_summary     list of all customers with balances, for the admin screen
  lp_admin_data  full reports (customers and suppliers), for the admin's Reports screen

Only rows that changed are sent. Uses only the Python standard library.
The Supabase secret key bypasses all security rules, so it stays on this
computer and is never put in the website.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "data", "portal-state.json")


class PortalError(Exception):
    pass


def norm_phone(raw):
    """'98000 00001', '+91-9800000001', '09800000001' -> '+919800000001'. Else None."""
    for part in re.split(r"[,;/]| or ", raw or ""):
        d = re.sub(r"\D", "", part)
        if len(d) == 12 and d.startswith("91"):
            d = d[2:]
        if len(d) == 11 and d.startswith("0"):
            d = d[1:]
        if len(d) == 10 and d[0] in "6789":
            return "+91" + d
    return None


def _jwt_role(key):
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------
# Talking to Supabase (REST)
# ----------------------------------------------------------------------------
class Supa:
    def __init__(self, url, key, timeout=60):
        url = (url or "").strip().rstrip("/")
        key = (key or "").strip()
        if not url.startswith("https://") or "supabase" not in url:
            raise PortalError("supabase_url in Settings is missing or wrong. It looks like "
                              "https://abcdefgh.supabase.co (Supabase: Project Settings > Data API).")
        if not key or key.startswith("PASTE"):
            raise PortalError("supabase_secret_key in Settings is missing. Copy the secret key "
                              "(or legacy service_role key) from Supabase: Project Settings > API Keys.")
        if key.startswith("sb_publishable") or _jwt_role(key) == "anon":
            raise PortalError("config.json has the publishable (anon) key. The office app needs the "
                              "SECRET key (or legacy service_role key) to upload.")
        self.base = url + "/rest/v1/"
        self.key = key
        self.timeout = timeout

    def call(self, method, path, body=None, prefer=None):
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if self.key.startswith("eyJ"):            # legacy service_role key is a JWT
            headers["Authorization"] = "Bearer " + self.key
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code in (401, 403):
                raise PortalError("Supabase refused the key (%s). Check supabase_secret_key in "
                                  "config.json. Details: %s" % (e.code, detail))
            if "PGRST002" in detail or e.code in (502, 503, 504):
                raise PortalError("Supabase is not answering right now (it may be starting up or paused). "
                                  "Open your project on supabase.com, wait a minute, and try again.")
            if e.code == 404 or "does not exist" in detail or "PGRST205" in detail \
                    or "Could not find the table" in detail or "Could not find the function" in detail:
                raise PortalError("The portal tables were not found in Supabase. Run "
                                  "link-portal-setup.sql in the Supabase SQL Editor first. Details: %s" % detail)
            raise PortalError("Supabase error %s: %s" % (e.code, detail))
        except urllib.error.URLError as e:
            raise PortalError("Could not reach Supabase. Check the internet connection and "
                              "supabase_url. Details: %s" % e)

    def upsert(self, table, rows, conflict):
        for i in range(0, len(rows), 200):
            self.call("POST", "%s?on_conflict=%s" % (table, conflict), rows[i:i + 200],
                      prefer="resolution=merge-duplicates,return=minimal")

    def delete_in(self, table, col, values):
        for i in range(0, len(values), 100):
            part = ",".join('"%s"' % v for v in values[i:i + 100])
            q = urllib.parse.quote("in.(%s)" % part, safe="")
            self.call("DELETE", "%s?%s=%s" % (table, col, q), prefer="return=minimal")


# ----------------------------------------------------------------------------
# Building what to upload
# ----------------------------------------------------------------------------
def _day(s):
    return dt.date.fromisoformat(s)


def _numbers(p, bills, sets, ents, today):
    close = p["opening"] + sum(e["delta"] for e in ents)
    paid = {}
    for s in sets:
        paid[s["bill"]] = paid.get(s["bill"], 0) + s["amount"]
    overdue, pending = 0.0, 0
    for b in bills:
        pend = b["amount"] - paid.get(b["id"], 0)
        if pend > 0.5:
            pending += 1
            if today > _day(b["date"]) + dt.timedelta(days=b.get("credit") or 0):
                overdue += pend
    return round(close, 2), round(overdue, 2), pending


def _slice(data, p, since, show_narration):
    bills = [b for b in data["bills"] if b["party"] == p["id"]]
    ids = {b["id"] for b in bills}
    sets = [s for s in data["settlements"] if s["bill"] in ids]
    ents = [e for e in data["entries"] if e["party"] == p["id"]]
    opening = p["opening"]
    start = data["books_from"]
    if since and since > start:
        start = since
        opening += sum(e["delta"] for e in ents if e["date"] < since)
        paid = {}
        for s in sets:
            paid[s["bill"]] = paid.get(s["bill"], 0) + s["amount"]
        late = {s["bill"] for s in sets if s["date"] >= since}
        bills = [b for b in bills if b["date"] >= since or b["id"] in late
                 or b["amount"] - paid.get(b["id"], 0) > 0.5]
        ids = {b["id"] for b in bills}
        sets = [s for s in sets if s["bill"] in ids]
        ents = [e for e in ents if e["date"] >= since]
    return {
        "id": p["id"], "name": p["name"], "city": p.get("city") or "",
        "credit": p.get("credit") or 0, "opening": round(opening, 2) + 0.0, "from": start,
        "bills": [{k: b[k] for k in ("id", "ref", "date", "amount", "credit", "vtype")} for b in bills],
        "settlements": [{k: s[k] for k in ("bill", "date", "amount", "entry")} for s in sets],
        "entries": [{"id": e["id"], "date": e["date"], "type": e["type"], "no": e["no"],
                     "delta": e["delta"], "allocs": e["allocs"],
                     "narration": e.get("narration", "") if show_narration else ""} for e in ents],
    }


def _admin_data(data, since, company):
    """Everything the admin's online Reports screen needs (all customers and suppliers),
    in the same shape as the office app's data, trimmed to the same history."""
    parties, bills, sets, ents = [], [], [], []
    by_party_bills, by_party_ents = {}, {}
    for b in data["bills"]:
        by_party_bills.setdefault(b["party"], []).append(b)
    for e in data["entries"]:
        by_party_ents.setdefault(e["party"], []).append(e)
    for p in data["parties"]:
        sl = _slice(data, p, since, True)
        q = dict(p)
        q["opening"] = sl["opening"]
        parties.append(q)
        bill_ids = {x["id"] for x in sl["bills"]}
        keep = {x["id"] for x in sl["entries"]}
        bills += [b for b in by_party_bills.get(p["id"], []) if b["id"] in bill_ids]
        sets += [x for x in data["settlements"] if x["bill"] in bill_ids] if bill_ids else []
        ents += [e for e in by_party_ents.get(p["id"], []) if e["id"] in keep]
    start = max(data["books_from"], since) if since else data["books_from"]
    return {
        "app_data_version": data.get("app_data_version", 1), "source": data.get("source", "tally"),
        "company": company or data.get("company", ""), "books_from": start, "as_of": data["as_of"],
        "synced_at": data.get("synced_at"), "counts": data.get("counts", {}),
        "warnings": data.get("warnings", []),
        "parties": parties, "bills": bills, "settlements": sets, "entries": ents,
    }


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def ckey_of(name):
    return " ".join((name or "").split()).lower()


def new_token():
    return secrets.token_hex(32)


def upload(data, cfg, log=print, client=None):
    """Send the data to Supabase. Returns a short summary string."""
    if data.get("source") == "sample" and not cfg.get("portal_allow_sample", False):
        raise PortalError("Sample data is not uploaded to the portal. Sync from Tally first, or "
                          "use 'Upload sample data' in Settings to test with sample data.")
    sb = client or Supa(cfg.get("supabase_url"), cfg.get("supabase_secret_key"))

    today = dt.date.today()
    months = int(cfg.get("portal_history_months", 24) or 0)
    since = None
    if months > 0:
        y, m = today.year, today.month - months
        while m <= 0:
            m += 12
            y -= 1
        since = dt.date(y, m, 1).isoformat()
    show_narr = bool(cfg.get("portal_show_narration", False))
    company = cfg.get("your_company_name") or data.get("company", "")

    accounts, summary = {}, []
    for p in data["parties"]:
        if p["side"] != "dr":
            continue
        key = ckey_of(p["name"])
        if not key or key in accounts:
            continue
        bills = [b for b in data["bills"] if b["party"] == p["id"]]
        ids = {b["id"] for b in bills}
        sets = [x for x in data["settlements"] if x["bill"] in ids]
        ents = [e for e in data["entries"] if e["party"] == p["id"]]
        close, overdue, pending = _numbers(p, bills, sets, ents, today)
        phone = norm_phone(p.get("mobile", "")) or ""
        summary.append({"ckey": key, "pid": p["id"], "name": p["name"], "city": p.get("city") or "",
                        "phone": phone, "outstanding": close, "overdue": overdue, "pending": pending})
        accounts[key] = {"name": p["name"], "phone": phone,
                         "doc": {"company": company, "as_of": data["as_of"],
                                 "accounts": [_slice(data, p, since, show_narr)]}}

    state = _load_state()
    old = state.get("accounts", {})
    new, rows = {}, []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for key, a in accounts.items():
        h = _hash(a)
        new[key] = h
        if old.get(key) != h:
            rows.append({"ckey": key, "name": a["name"], "phone": a["phone"], "data": a["doc"],
                         "updated_at": now})
    gone = [k for k in old if k not in new]
    if rows:
        sb.upsert("lp_accounts", rows, "ckey")
    if gone:
        sb.delete_in("lp_accounts", "ckey", gone)

    # every customer gets a private link; there is always one admin link
    links = sb.call("GET", "lp_links?select=kind,ckey") or []
    have = {r["ckey"] for r in links if r.get("kind") == "customer"}
    add = [{"token": new_token(), "kind": "customer", "ckey": k} for k in accounts if k not in have]
    if not any(r.get("kind") == "admin" for r in links):
        add.append({"token": new_token(), "kind": "admin", "ckey": None})
    if add:
        sb.call("POST", "lp_links", add, prefer="return=minimal")
    stale = sorted(k for k in have if k not in accounts)
    if stale:
        sb.delete_in("lp_links", "ckey", stale)

    summ = {"company": company, "as_of": data["as_of"], "customers": summary}
    sb.upsert("lp_summary", [{"id": 1, "data": summ, "synced_at": now}], "id")

    full = _admin_data(data, since, company)
    hfull = _hash({k: v for k, v in full.items() if k != "synced_at"})
    if state.get("admin_data") != hfull:
        sb.upsert("lp_admin_data", [{"id": 1, "data": full, "synced_at": now}], "id")
    state["accounts"] = new
    state["admin_data"] = hfull
    state["last_upload"] = dt.datetime.now().isoformat(timespec="seconds")
    _save_state(state)
    new_links = len([a for a in add if a["kind"] == "customer"])
    msg = "Portal: %d customers online, %d updated, %d removed." % (len(accounts), len(rows), len(gone))
    if new_links:
        msg += " %d new private links made." % new_links
    return msg


# ----------------------------------------------------------------------------
# Private links (for the office app's Customer links tab)
# ----------------------------------------------------------------------------
def list_links(cfg, client=None):
    """Returns {"admin": token or "", "customers": [{ckey, name, phone, token}]}."""
    sb = client or Supa(cfg.get("supabase_url"), cfg.get("supabase_secret_key"))
    accts = sb.call("GET", "lp_accounts?select=ckey,name,phone&order=name") or []
    links = sb.call("GET", "lp_links?select=token,kind,ckey") or []
    tok = {r["ckey"]: r["token"] for r in links if r.get("kind") == "customer"}
    admin = next((r["token"] for r in links if r.get("kind") == "admin"), "")
    acc = sb.call("GET", "lp_access?select=ckey,enabled") or []
    off = {r["ckey"] for r in acc if r.get("enabled") is False}
    return {"admin": admin,
            "customers": [{"ckey": a["ckey"], "name": a["name"], "phone": a.get("phone") or "",
                           "token": tok.get(a["ckey"], ""), "enabled": a["ckey"] not in off}
                          for a in accts]}


def renew_link(cfg, ckey=None, client=None):
    """Make a new private link (the old one stops working). ckey=None renews the admin link."""
    sb = client or Supa(cfg.get("supabase_url"), cfg.get("supabase_secret_key"))
    tok = new_token()
    if ckey is None:
        sb.call("DELETE", "lp_links?kind=eq.admin", prefer="return=minimal")
        sb.call("POST", "lp_links", [{"token": tok, "kind": "admin", "ckey": None}], prefer="return=minimal")
        return tok
    found = sb.call("GET", "lp_accounts?select=ckey&ckey=eq." + urllib.parse.quote(ckey, safe=""))
    if not found:
        raise PortalError("That customer is not in the portal yet. Upload to the portal first.")
    sb.delete_in("lp_links", "ckey", [ckey])
    sb.call("POST", "lp_links", [{"token": tok, "kind": "customer", "ckey": ckey}], prefer="return=minimal")
    return tok
