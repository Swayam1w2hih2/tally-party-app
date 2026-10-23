"""
Tally connector
---------------
Reads party ledgers, bills, receipts and payments from TallyPrime / Tally.ERP 9
through Tally's built-in XML server (default http://localhost:9000) and turns
them into one simple data file the app can show.

It only READS from Tally. It never creates, changes or deletes anything.
Uses only the Python standard library (no extra installs needed).
"""
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


class TallyError(Exception):
    """A problem talking to Tally, with a message written for people."""


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
# Tally sometimes sends characters that are not allowed in XML (for example &#4;).
_BAD_REFS = re.compile(
    r"&#(?:x0*[0-8bcef]|x0*1[0-9a-f]|0*[0-8]|0*1[12]|0*1[4-9]|0*2[0-9]|0*3[01]);", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def clean_xml(text):
    text = _BAD_REFS.sub("", text)
    text = _CTRL.sub("", text)
    return _DECL.sub("", text).strip()


def tx(el, tag):
    """Text of the first child with this tag, or ''."""
    if el is None:
        return ""
    found = el.find(tag)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def num(s):
    """'-48,000.00' -> -48000.0. Anything unreadable -> 0."""
    if not s:
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group(0)) if m else 0.0


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s):
    """'20260401' or '1-Apr-2026' -> date, else None."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{2,4})", s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return dt.date(y, _MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def parse_days(s):
    """'30 Days' -> 30. A due date or anything else -> None."""
    s = (s or "").strip()
    m = re.search(r"(\d+)\s*day", s, re.I)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d+", s):
        return int(s)
    return None


def tally_date(d):
    return d.strftime("%Y%m%d")


def name_of(el):
    n = el.get("NAME")
    if n:
        return n.strip()
    n = tx(el, "NAME")
    if n:
        return n
    lst = el.find("NAME.LIST")
    return tx(lst, "NAME") if lst is not None else ""


# ----------------------------------------------------------------------------
# Talking to Tally
# ----------------------------------------------------------------------------
class Tally:
    def __init__(self, url="http://localhost:9000", company="", timeout=120, debug_dir=None):
        self.url = url.rstrip("/") + "/"
        self.company = (company or "").strip()
        self.timeout = timeout
        self.debug_dir = debug_dir
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)

    def _static(self, extra=""):
        comp = ""
        if self.company:
            comp = "<SVCURRENTCOMPANY>%s</SVCURRENTCOMPANY>" % _esc(self.company)
        return "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>" + comp + extra

    def post(self, xml, label="request"):
        data = xml.encode("utf-8")
        req = urllib.request.Request(self.url, data=data,
                                     headers={"Content-Type": "text/xml;charset=utf-8"})
        try:
            # Tally is on this computer or the office network, so never send it through a proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.URLError as e:
            raise TallyError(
                "Could not reach Tally at %s. Check that Tally is open, a company is loaded, "
                "and Tally is set to act as Server or Both on this port "
                "(F1 > Settings > Connectivity). Details: %s" % (self.url, e))
        except OSError as e:
            raise TallyError("Tally did not answer in time (%s). Details: %s" % (self.url, e))

        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8", errors="replace")

        if self.debug_dir:
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)
            with open(os.path.join(self.debug_dir, safe + ".xml"), "w", encoding="utf-8") as f:
                f.write(text)

        text = clean_xml(text)
        if not text:
            raise TallyError("Tally sent back an empty answer for %s." % label)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise TallyError("Tally's answer for %s could not be read (%s). "
                             "Run the connection test to save the answer for checking." % (label, e))
        err = root.find(".//LINEERROR")
        if err is not None and (err.text or "").strip():
            raise TallyError("Tally reported an error for %s: %s" % (label, err.text.strip()))
        return root

    def collection(self, cid, tdl_body, extra_static="", label=None):
        xml = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
            "<TYPE>Collection</TYPE><ID>%s</ID></HEADER><BODY><DESC><STATICVARIABLES>%s"
            "</STATICVARIABLES><TDL><TDLMESSAGE>%s</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
        ) % (cid, self._static(extra_static), tdl_body)
        return self.post(xml, label or cid)

    # -- the four things we read ----------------------------------------------
    def companies(self):
        root = self.collection(
            "AppCompanies",
            '<COLLECTION NAME="AppCompanies" ISMODIFY="No"><TYPE>Company</TYPE>'
            "<FETCH>Name, StartingFrom, BooksFrom</FETCH></COLLECTION>",
            label="companies")
        out = []
        for c in root.iter("COMPANY"):
            out.append({
                "name": name_of(c),
                "starting_from": parse_date(tx(c, "STARTINGFROM")),
                "books_from": parse_date(tx(c, "BOOKSFROM")),
            })
        return [c for c in out if c["name"]]

    def groups(self):
        root = self.collection(
            "AppGroups",
            '<COLLECTION NAME="AppGroups" ISMODIFY="No"><TYPE>Group</TYPE>'
            "<FETCH>Name, Parent</FETCH></COLLECTION>",
            label="groups")
        return {name_of(g): tx(g, "PARENT") for g in root.iter("GROUP") if name_of(g)}

    def ledgers(self, frm, to, progress=None):
        """Party ledgers, read WITHOUT any dates (Educational mode only accepts some dates,
        and a refused date makes Tally show a message box and stop answering).
        Read in two small parts so that, if anything goes wrong, we know which part."""
        say = progress or (lambda m: None)
        say("  balances ...")
        root = self.collection(
            "AppLedgers",
            '<COLLECTION NAME="AppLedgers" ISMODIFY="No"><TYPE>Ledger</TYPE>'
            "<FETCH>Name, Parent, OpeningBalance, ClosingBalance, IsBillWiseOn, BillCreditPeriod</FETCH>"
            "</COLLECTION>",
            label="ledgers_balances")
        ledgers = list(root.iter("LEDGER"))
        say("  contact details ...")
        try:
            extra = self.collection(
                "AppLedgerContacts",
                '<COLLECTION NAME="AppLedgerContacts" ISMODIFY="No"><TYPE>Ledger</TYPE>'
                "<FETCH>Name, LedgerMobile, LedgerPhone, Email, PartyGSTIN, LedStateName</FETCH>"
                "</COLLECTION>",
                label="ledgers_contacts")
            by = {name_of(e).lower(): e for e in extra.iter("LEDGER")}
            for led in ledgers:
                e = by.get(name_of(led).lower())
                if e is None:
                    continue
                for child in list(e):
                    if child.tag in ("LEDGERMOBILE", "LEDGERPHONE", "EMAIL", "PARTYGSTIN", "LEDSTATENAME") \
                            and led.find(child.tag) is None:
                        led.append(child)
        except TallyError as e:
            say("  (contact details could not be read, continuing without them: %s)" % e)
        return ledgers

    def vouchers(self, frm, to, progress=None):
        """Day Book, one month at a time.
        Every date sent to Tally is the 1st of a month, which Educational mode accepts.
        Each request covers 1st of the month to 1st of the next month; vouchers dated
        outside the month are dropped here, so nothing is counted twice."""
        out = []
        start = frm.replace(day=1)
        while start <= to:
            nxt = (start + dt.timedelta(days=32)).replace(day=1)
            xml = (
                "<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER><BODY><EXPORTDATA>"
                "<REQUESTDESC><REPORTNAME>Day Book</REPORTNAME><STATICVARIABLES>%s"
                '<SVFROMDATE TYPE="Date">%s</SVFROMDATE><SVTODATE TYPE="Date">%s</SVTODATE>'
                "</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"
            ) % (self._static(), tally_date(start), tally_date(nxt))
            root = self.post(xml, "daybook_%s" % start.strftime("%Y_%m"))
            found = []
            for v in root.iter("VOUCHER"):
                d = parse_date(tx(v, "DATE"))
                if d and start <= d < nxt and d >= frm and d <= to:
                    found.append(v)
            out.extend(found)
            if progress:
                progress("  %s: %d vouchers" % (start.strftime("%b %Y"), len(found)))
            start = nxt
        return out


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


# ----------------------------------------------------------------------------
# Turning Tally's answers into the app's data
# ----------------------------------------------------------------------------
def side_of(parent, groups):
    """'dr' if the group sits under Sundry Debtors, 'cr' under Sundry Creditors."""
    g, seen = parent, set()
    while g and g not in seen:
        low = g.lower()
        if low == "sundry debtors":
            return "dr"
        if low == "sundry creditors":
            return "cr"
        seen.add(g)
        g = groups.get(g, "")
    return None


def due(side, tally_amount):
    """
    Tally writes debits as negative numbers. We turn every amount into
    'how much is due': positive = customer owes us (dr) or we owe supplier (cr).
    """
    return -tally_amount if side == "dr" else tally_amount


def build_data(company, books_from, to, groups, ledger_elems, voucher_elems, source="tally"):
    warnings = []
    parties, by_name = [], {}

    # --- parties --------------------------------------------------------------
    for el in ledger_elems:
        name = name_of(el)
        parent = tx(el, "PARENT")
        side = side_of(parent, groups)
        if not name or not side:
            continue
        address = [a.text.strip() for a in el.iter("ADDRESS") if a.text and a.text.strip()]
        state = tx(el, "LEDSTATENAME") or tx(el, "STATENAME")
        closing_tag = el.find("CLOSINGBALANCE")
        p = {
            "id": "p%d" % (len(parties) + 1),
            "name": name,
            "group": parent,
            "side": side,
            "city": address[-1] if address else state,
            "state": state,
            "mobile": tx(el, "LEDGERMOBILE") or tx(el, "LEDGERPHONE"),
            "email": tx(el, "EMAIL"),
            "gstin": tx(el, "PARTYGSTIN"),
            "credit": parse_days(tx(el, "BILLCREDITPERIOD")) or 0,
            "billwise": tx(el, "ISBILLWISEON").lower() != "no",
            "opening": round(due(side, num(tx(el, "OPENINGBALANCE"))), 2),
            "tally_closing": (round(due(side, num(closing_tag.text or "")), 2)
                              if closing_tag is not None else None),
            "_opening_bills": [],
        }
        for b in el.findall("BILLALLOCATIONS.LIST"):
            ref = tx(b, "NAME")
            if not ref:
                continue
            p["_opening_bills"].append({
                "ref": ref,
                "date": parse_date(tx(b, "BILLDATE")) or books_from,
                "amount": due(side, num(tx(b, "OPENINGBALANCE"))),
                "credit": parse_days(tx(b, "BILLCREDITPERIOD")),
            })
        parties.append(p)
        by_name[name.lower()] = p

    no_billwise = [p["name"] for p in parties if not p["billwise"]]
    if no_billwise:
        warnings.append("Bill-by-bill is off for %d parties, so their pending bills cannot be "
                        "shown (only balances): %s" % (len(no_billwise), ", ".join(no_billwise[:10])))

    # --- read vouchers --------------------------------------------------------
    recs = []
    for v in voucher_elems:
        if tx(v, "ISCANCELLED").lower() == "yes" or tx(v, "ISOPTIONAL").lower() == "yes" \
                or tx(v, "ISDELETED").lower() == "yes":
            continue
        date = parse_date(tx(v, "DATE"))
        if not date or date > to:
            continue
        # Accounting-mode vouchers list ledgers in ALLLEDGERENTRIES.LIST; item-invoice
        # vouchers (with stock items) usually put the party in LEDGERENTRIES.LIST.
        # Some vouchers have both, so read both and skip a ledger already seen.
        lines = [e for e in v.findall("ALLLEDGERENTRIES.LIST") if tx(e, "LEDGERNAME")]
        seen = {tx(e, "LEDGERNAME").lower() for e in lines}
        lines += [e for e in v.findall("LEDGERENTRIES.LIST")
                  if tx(e, "LEDGERNAME") and tx(e, "LEDGERNAME").lower() not in seen]
        hits = {}
        for e in lines:
            p = by_name.get(tx(e, "LEDGERNAME").lower())
            if not p:
                continue
            h = hits.setdefault(p["id"], {"party": p, "delta": 0.0, "allocs": []})
            h["delta"] += due(p["side"], num(tx(e, "AMOUNT")))
            for b in e.findall("BILLALLOCATIONS.LIST"):
                ref = tx(b, "NAME")
                btype = tx(b, "BILLTYPE") or "On Account"
                h["allocs"].append({
                    "ref": ref,
                    "type": btype,
                    "amount": due(p["side"], num(tx(b, "AMOUNT"))),
                    "credit": parse_days(tx(b, "BILLCREDITPERIOD")),
                })
        if not hits:
            continue
        recs.append({
            "date": date,
            "type": tx(v, "VOUCHERTYPENAME") or v.get("VCHTYPE", "") or "Voucher",
            "no": tx(v, "VOUCHERNUMBER"),
            "narration": tx(v, "NARRATION"),
            "hits": list(hits.values()),
        })
    recs.sort(key=lambda r: r["date"])

    # --- bills: opening bills + every New Ref ---------------------------------
    bills, bill_key = [], {}

    def add_bill(p, ref, date, amount, credit, src, vtype="", vno=""):
        key = (p["id"], ref.lower())
        if key in bill_key:                       # same bill name used again: add to it
            bill_key[key]["amount"] += amount
            return
        b = {"id": "b%d" % (len(bills) + 1), "party": p["id"], "ref": ref, "date": date,
             "amount": amount, "credit": credit if credit is not None else p["credit"],
             "src": src, "vtype": vtype, "vno": vno}
        bills.append(b)
        bill_key[key] = b

    for p in parties:
        total = 0.0
        for ob in p["_opening_bills"]:
            add_bill(p, ob["ref"], ob["date"], ob["amount"], ob["credit"], "opening", "Opening")
            total += ob["amount"]
        left = p["opening"] - total
        if abs(left) > 0.5 and p["billwise"]:
            add_bill(p, "Opening balance", books_from - dt.timedelta(days=1), left, None,
                     "opening", "Opening")

    entries = []
    for r in recs:
        for h in r["hits"]:
            p = h["party"]
            eid = "e%d" % (len(entries) + 1)
            h["eid"] = eid
            entries.append({"id": eid, "party": p["id"], "date": r["date"], "type": r["type"],
                            "no": r["no"], "narration": r["narration"],
                            "delta": round(h["delta"], 2),
                            "allocs": [{"ref": a["ref"], "type": a["type"],
                                        "amount": round(a["amount"], 2)} for a in h["allocs"]]})
            for a in h["allocs"]:
                if a["type"].lower() == "new ref" and a["ref"]:
                    add_bill(p, a["ref"], r["date"], a["amount"], a["credit"], "voucher",
                             r["type"], r["no"])

    # --- settlements: every Agst Ref ------------------------------------------
    settlements, unmatched = [], 0
    for r in recs:
        for h in r["hits"]:
            for a in h["allocs"]:
                if a["type"].lower() != "agst ref" or not a["ref"]:
                    continue
                b = bill_key.get((h["party"]["id"], a["ref"].lower()))
                if not b:
                    unmatched += 1
                    continue
                settlements.append({"bill": b["id"], "date": r["date"],
                                    "amount": round(-a["amount"], 2), "entry": h["eid"]})
    if unmatched:
        warnings.append("%d payments were adjusted against bills that could not be found "
                        "(often bills from before %s). They are counted in balances but not "
                        "matched to a bill." % (unmatched, books_from.strftime("%d %b %Y")))

    # --- tidy up for saving ---------------------------------------------------
    iso = lambda d: d.isoformat() if isinstance(d, dt.date) else d
    for p in parties:
        p.pop("_opening_bills", None)
    for b in bills:
        b["date"] = iso(b["date"])
        b["amount"] = round(b["amount"], 2)
    for e in entries:
        e["date"] = iso(e["date"])
    for s in settlements:
        s["date"] = iso(s["date"])

    return {
        "app_data_version": 1,
        "source": source,
        "company": company,
        "books_from": iso(books_from),
        "as_of": iso(to),
        "synced_at": dt.datetime.now().isoformat(timespec="seconds"),
        "counts": {"parties": len(parties), "bills": len(bills), "entries": len(entries),
                   "vouchers_read": len(voucher_elems)},
        "warnings": warnings,
        "parties": parties,
        "bills": bills,
        "settlements": settlements,
        "entries": entries,
    }


# ----------------------------------------------------------------------------
# One full sync
# ----------------------------------------------------------------------------
def sync(cfg, progress=print, debug_dir=None):
    t = Tally(cfg.get("tally_url", "http://localhost:9000"), cfg.get("tally_company", ""),
              timeout=int(cfg.get("tally_timeout_seconds", 120)), debug_dir=debug_dir)
    progress("Connecting to Tally at %s ..." % t.url)
    comps = t.companies()
    if not comps:
        raise TallyError("Tally is running but no company is open. Open your company in Tally first.")
    if t.company:
        match = [c for c in comps if c["name"].lower() == t.company.lower()]
        if not match:
            raise TallyError("The company '%s' (from Settings) is not open in Tally. Open companies: %s"
                             % (t.company, ", ".join(c["name"] for c in comps)))
        comp = match[0]
    else:
        comp = comps[0]
        if len(comps) > 1:
            progress("More than one company is open. Using '%s'. To choose another, set "
                     "tally_company in Settings." % comp["name"])
        t.company = comp["name"]
    progress("Company: %s" % comp["name"])

    today = dt.date.today()
    books_from = parse_date(cfg.get("read_from_date", "")) or comp["books_from"] \
        or comp["starting_from"] or dt.date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    progress("Reading from %s to %s" % (books_from.strftime("%d %b %Y"), today.strftime("%d %b %Y")))

    progress("Reading groups ...")
    groups = t.groups()
    progress("Reading ledgers ...")
    ledgers = t.ledgers(books_from, today, progress)
    progress("Reading vouchers (Day Book), month by month ...")
    vouchers = t.vouchers(books_from, today, progress)

    data = build_data(comp["name"], books_from, today, groups, ledgers, vouchers)

    mism = [p for p in data["parties"] if p["tally_closing"] is not None
            and abs(_closing(data, p) - p["tally_closing"]) > 1]
    if mism:
        data["warnings"].append("%d parties have a balance that does not match Tally. "
                                "See the Data check tab." % len(mism))
    progress("Done: %d parties, %d bills, %d entries." % (
        data["counts"]["parties"], data["counts"]["bills"], data["counts"]["entries"]))
    return data


def _closing(data, p):
    return p["opening"] + sum(e["delta"] for e in data["entries"] if e["party"] == p["id"])


def build_from_files(company, books_from, to, masters_path, vouchers_path, source="sample"):
    """Build app data from Tally XML files (used to make the sample data)."""
    def load(path):
        with open(path, encoding="utf-8") as f:
            return ET.fromstring(clean_xml(f.read()))
    m = load(masters_path)
    v = load(vouchers_path)
    return build_data(company, books_from, to, {}, list(m.iter("LEDGER")), list(v.iter("VOUCHER")),
                      source=source)
