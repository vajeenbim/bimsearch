"""
BIM Search data job
-------------------
Runs on GitHub (on a schedule) or on a PC. It downloads the Styra reports and Kobo forms once,
splits them into small compressed pieces (one per month for dated reports) and uploads the pieces
that changed to the "BIM Search data" folder in Google Drive through the Google Apps Script server.
Phones then only download the pieces that changed.

Needs two settings (GitHub: Settings > Secrets and variables > Actions):
  BIM_SERVER   the Apps Script web app link (ends with /exec)
  BIM_JOB_KEY  the "Data job key" from the Settings tab of the Google Sheet
Optional: STYRA_API (default https://apikrglive.styra.in), ONLY (comma separated report ids, for tests)
"""
import base64, csv, datetime, gzip, hashlib, hmac, io, json, os, re, shutil, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, zipfile

csv.field_size_limit(1 << 30)
SERVER = os.environ.get("BIM_SERVER", "").strip()
KEY = os.environ.get("BIM_JOB_KEY", "").strip()
STYRA_API = os.environ.get("STYRA_API", "https://apikrglive.styra.in").rstrip("/")
ONLY = [x for x in os.environ.get("ONLY", "").split(",") if x]
TZ = datetime.timezone(datetime.timedelta(hours=3))        # Iraq time
TODAY = datetime.datetime.now(TZ).date().isoformat()
CHUNK_BYTES = 30_000_000                                   # about 30 MB of CSV per piece before compression
WORK = tempfile.mkdtemp(prefix="bimjob_")

REPORTS = [  # Work Order Report is not in the site app
    dict(key="consumer-meter-mapping", name="Consumer Meter Mapping Report", em=10, space=8, tpl="9dfaaffe-b357-4a6f-92ef-5d1fed50c6cb"),
    dict(key="meter-installation", name="Meter Installation", em=5, space=3, tpl="277df233-3f23-4953-930d-f91c93512592", date=["Installation done date", r"installation.*date"]),
    dict(key="meter-survey", name="Meter Survey", em=4, space=2, tpl="d02f00ed-6a51-4f70-99bf-957b9501fb81", date=["Survey_date", r"survey_date"]),
    dict(key="tentative-transformer", name="Tentative Transformer Report", em=16, space=18, tpl="1a957d6e-5a89-43ce-a65e-fa52dd8425c2", noParams=True),
    dict(key="transformer-mapping", name="Transformer Mapping Report", em=14, space=11, tpl="a7dc0dce-5fe9-4d69-a4ea-508e924ae443"),
    dict(key="transformer-installation", name="Transformer Installation", em=6, space=5, tpl="fdda8d42-8f15-430a-9104-f41d581e8f53", date=["Date of Installation", r"date of installation"]),
    dict(key="transformer-survey", name="Transformer Survey", em=2, space=1, tpl="d50bf889-fd15-4864-a676-2447b6e49810", date=["Survey Done Date", r"survey done date"]),
]


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- http
def http(method, url, headers=None, body=None, timeout=900, raw=False, to_file=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if to_file:
                    with open(to_file, "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
                    return r.status, dict(r.headers), None
                b = r.read()
                return r.status, dict(r.headers), (b if raw else b.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == 2:
                raise
            say("   network problem, trying again:", e)
            time.sleep(10)


def hdr(h, name):
    for k, v in h.items():
        if k.lower() == name.lower():
            return v
    return None


def server(action, **kw):
    body = json.dumps(dict(action=action, key=KEY, **kw))
    st, h, t = http("POST", SERVER, {"content-type": "text/plain;charset=utf-8"}, body, timeout=600)
    try:
        j = json.loads(t)
    except Exception:
        raise SystemExit(f"The Google server did not answer correctly (HTTP {st}). Check BIM_SERVER. Answer: {t[:200]}")
    if not j.get("ok"):
        raise SystemExit(f"Google server: {j.get('error')}")
    return j


# ---------------------------------------------------------------- Styra
class Styra:
    def __init__(self, user, pwd):
        self.user, self.pwd, self.sess = user, pwd, None

    base = {"accept": "application/json, text/plain, */*", "appversion": "4.72.1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"}

    def login(self):
        st, h, t = http("POST", f"{STYRA_API}/styra/login", {**self.base, "content-type": "application/json"},
                        json.dumps({"username": self.user, "password": self.pwd, "clientIdentifier": "INVENTORY_WEB"}))
        j = json.loads(t or "{}"); d = j.get("data") or {}
        tok, user = hdr(h, "x-auth-token"), d.get("user")
        if d.get("context") == "MULTIPLE_SESSION":            # "other active sessions?" -> No
            st, h2, t2 = http("POST", f"{STYRA_API}/styra/user/allow-concurrent-sessions",
                              {**self.base, "content-type": "application/json", "x-multiple-session-token": hdr(h, "x-multiple-session-token") or "", "x-request-hmac": ""}, "")
            tok, user = hdr(h2, "x-auth-token"), (json.loads(t2 or "{}").get("data"))
        if not tok or not user or not user.get("uniqueId"):
            raise RuntimeError("Styra login failed: " + str(j.get("description") or st))
        self.sess = {"token": tok, "user": user}

    def sign(self, msg):
        u = self.sess["user"]
        key = f"{u['uniqueId']}~{u.get('groupRid') if u.get('groupRid') is not None else 0}".encode()
        return base64.b64encode(hmac.new(key, msg.encode(), hashlib.sha256).digest()).decode()

    def export(self, rep, out_csv):
        if not self.sess:
            self.login()
        q = f"emRid={rep['em']}&templateUniqueId={rep['tpl']}"
        def ask():
            return http("GET", f"{STYRA_API}/styra/report/generate-report-token?{q}",
                        {**self.base, "authorization": f"Bearer {self.sess['token']}", "x-request-hmac": self.sign(q)})
        st, h, t = ask()
        if st in (401, 403):
            self.login(); st, h, t = ask()
        rt = (json.loads(t or "{}") or {}).get("data")
        if not isinstance(rt, str):
            raise RuntimeError(f"Styra refused the report (HTTP {st}): {t[:150]}")
        st, h, t = http("POST", f"{STYRA_API}/krg-pivot-api/report/validate-external-session",
                        {"accept": "application/json, text/plain, */*", "content-type": "text/plain", "token": rt}, "null")
        ptok = hdr(h, "x-auth-token")
        if not ptok:
            raise RuntimeError("Styra did not open the report")
        auth = {"accept": "application/json, text/plain, */*", "authorization": f"Bearer {ptok}"}
        st, h, t = http("GET", f"{STYRA_API}/krg-pivot-api/master/list-filter-values?spaceId={rep['space']}&column=Zone&searchKey=", auth)
        zones = (json.loads(t or "{}") or {}).get("data") or []
        if not zones:
            raise RuntimeError("Could not read the zone list")
        filters = [{"name": "Zone", "dataType": "character varying", "valueList": zones, "expression": ""}]
        if rep.get("date"):
            filters.append({"name": "Date Range", "dataType": "date", "valueList": [TODAY], "expression": "<="})
        u = self.sess["user"]
        params = "" if rep.get("noParams") else f"userRid={u['userId']}&roleRid={u['roleRid']}&buRid={u['buRid']}&featureRid={rep['em']}&"
        body = json.dumps({"mainFilterList": filters, "columnFilterList": []})
        raw = os.path.join(WORK, rep["key"] + ".download")
        for z in (1, 0):
            st, h, _ = http("POST", f"{STYRA_API}/krg-pivot-api/report/export/{rep['tpl']}/csv?{params}isZip={z}",
                            {**auth, "content-type": "application/json"}, body, timeout=1800, to_file=raw)
            if st != 200:
                continue
            with open(raw, "rb") as f:
                head = f.read(4)
            if head[:2] == b"PK":
                try:
                    with zipfile.ZipFile(raw) as zf, zf.open(zf.namelist()[0]) as src, open(out_csv, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    return
                except zipfile.BadZipFile:
                    say("   zipped file damaged, asking for the plain file")
                    continue
            if head[:1] == b"{":           # a message instead of a file
                continue
            shutil.move(raw, out_csv)
            return
        raise RuntimeError(f"Styra export failed (HTTP {st})")


# ---------------------------------------------------------------- Kobo
SKIP = {"_attachments", "_notes", "_tags", "formhub/uuid", "meta/rootUuid", "_xform_id_string", "__version__", "_bamboo_dataset_id"}


def kobo_server(v):
    t = (v or "").strip()
    if not t:
        return "https://kf.kobotoolbox.org"
    if not re.match(r"^https?://", t, re.I):
        t = "https://" + t
    p = urllib.parse.urlparse(t)
    return f"{p.scheme}://{p.netloc}" if "." in (p.hostname or "") or (p.hostname or "") in ("localhost",) or re.match(r"^\d", p.hostname or "") else "https://kf.kobotoolbox.org"


def kobo_get(base, token, url):
    st, h, t = http("GET", url, {"Authorization": f"Token {token}", "Accept": "application/json"})
    if st in (401, 403):
        raise RuntimeError("Kobo token not accepted, or no access to this form")
    if st == 404:
        raise RuntimeError("Kobo form not found")
    try:
        return json.loads(t)
    except Exception:
        raise RuntimeError(f"Kobo answered with a web page instead of data - check the Kobo server ({base})")


def flatten(recs, meta):
    cols, rows = {}, []
    for r in recs:
        o = {}
        for k, v in r.items():
            if k in SKIP:
                continue
            if k == "_geolocation":
                if isinstance(v, list) and len(v) == 2 and v[0] is not None:
                    o["_latitude"], o["_longitude"] = v[0], v[1]
                continue
            if k == "_validation_status":
                o[k] = v.get("label", "") if isinstance(v, dict) else ""
                continue
            o[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        for k in o:
            cols.setdefault(k, True)
        rows.append(o)
    cl = list(cols)
    sysc = lambda c: c.startswith("_") or c.startswith("meta/") or c in ("start", "end", "today", "deviceid")
    cl = [c for c in cl if not sysc(c)] + [c for c in cl if sysc(c)]
    if not meta or not (meta.get("content") or {}).get("survey"):
        return cl, [[r.get(c) for c in cl] for r in rows]
    first = lambda l: (next((x for x in l if x), "") if isinstance(l, list) else (l or ""))
    q_by = {}
    for q in meta["content"]["survey"]:
        n = q.get("name") or q.get("$autoname")
        if n:
            q_by[n] = q
    choices = {}
    for c in meta["content"].get("choices") or []:
        choices.setdefault(c.get("list_name"), {})[c.get("name") or c.get("$autovalue")] = first(c.get("label")) or c.get("name")
    header, used = {}, set()
    for c in cl:
        q = q_by.get(c.split("/")[-1])
        h = first(q.get("label")).strip() if q and first(q.get("label")) else c
        if h in used:
            h = f"{h} ({c.split('/')[-1]})"
        used.add(h); header[c] = h
    def mapv(c, v):
        q = q_by.get(c.split("/")[-1])
        if not q or v is None or v == "" or not q.get("select_from_list_name"):
            return v
        ch = choices.get(q["select_from_list_name"], {})
        if q.get("type") == "select_multiple":
            return ", ".join(ch.get(x, x) for x in str(v).split(" "))
        return ch.get(v, v)
    return [header[c] for c in cl], [[mapv(c, r.get(c)) for c in cl] for r in rows]


def kobo_form(f):
    base, token = kobo_server(f.get("server")), f.get("token", "")
    if not token:
        raise RuntimeError("no API token in the sheet")
    uid = (f.get("uid") or "").strip()
    m = re.search(r"/(?:forms|assets)/([A-Za-z0-9]{10,})", uid)
    if m:
        uid = m.group(1)
    if not re.match(r"^[A-Za-z0-9]{10,}$", uid):
        url, found = f"{base}/api/v2/assets/?asset_type=survey&limit=200&format=json", None
        want = (f.get("name") or "").strip().lower()
        allf = []
        while url:
            j = kobo_get(base, token, url); allf += j.get("results", []); url = j.get("next")
        found = next((a for a in allf if a.get("name", "").strip().lower() == want), None) or next((a for a in allf if want and want in a.get("name", "").lower()), None)
        if not found:
            raise RuntimeError("no form with this name in that Kobo account")
        uid = found["uid"]
    try:
        meta = kobo_get(base, token, f"{base}/api/v2/assets/{uid}/?format=json")
    except Exception:
        meta = None
    recs, url = [], f"{base}/api/v2/assets/{uid}/data/?format=json&limit=5000"
    while url:
        j = kobo_get(base, token, url); recs += j.get("results", []); url = j.get("next")
        say(f"   {len(recs):,} / {j.get('count', '?')}")
    cols, rows = flatten(recs, meta)
    out = os.path.join(WORK, "kobo-" + uid + ".csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\r\n"); w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return uid, out


# ---------------------------------------------------------------- splitting into pieces
def iso_date(v):
    t = (v or "").strip()
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


def find_col(cols, tests):
    for t in tests:
        for c in cols:
            if c.strip().lower() == t.lower() or re.search(t, c, re.I):
                return c
    return None


class Piece:
    def __init__(self, fid, part, header):
        self.name = f"{fid}__{part}.csv.gz"
        self.path = os.path.join(WORK, self.name)
        self.raw = open(self.path, "wb")
        self.fh = gzip.GzipFile(filename="", mode="wb", fileobj=self.raw, mtime=0, compresslevel=6)
        self.text = io.TextIOWrapper(self.fh, encoding="utf-8", newline="")
        self.w = csv.writer(self.text, lineterminator="\r\n")
        self.w.writerow(header)
        self.rows, self.size, self.part = 0, 0, part

    def add(self, row):
        self.w.writerow(row); self.rows += 1; self.size += sum(len(x) for x in row) + len(row)

    def close(self):
        self.text.flush(); self.text.detach(); self.fh.close(); self.raw.close()
        data = open(self.path, "rb").read()
        return {"name": self.name, "part": self.part, "rows": self.rows, "size": self.size, "gz": len(data), "sha": hashlib.sha1(data).hexdigest()}


def split(fid, path, date_tests):
    pieces, done = {}, []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rd = csv.reader(fh)
        header = next(rd, None)
        if not header:
            return [], []
        di = header.index(find_col(header, date_tests)) if date_tests and find_col(header, date_tests) else -1
        n = 0
        for row in rd:
            if not row or (len(row) == 1 and not row[0]):
                continue
            if di >= 0:
                d = iso_date(row[di] if di < len(row) else "")
                base = d[:7] if d else "nodate"
            else:
                base = "rows"
            p = pieces.get(base)
            if p is None or p.size > CHUNK_BYTES:
                if p is not None:
                    done.append(p.close())
                k = sum(1 for x in done if x["part"].split("_")[0] == base) + 1
                p = pieces[base] = Piece(fid, f"{base}_{k}", header)
            p.add(row); n += 1
    done += [p.close() for p in pieces.values()]
    done.sort(key=lambda x: x["part"], reverse=True)       # newest month first
    return header, done


# ---------------------------------------------------------------- main
def main():
    if not SERVER or not KEY:
        raise SystemExit("Set BIM_SERVER and BIM_JOB_KEY")
    cfg = server("jobconfig")
    old = (server("jobmanifest").get("manifest") or {}).get("reports", [])
    old_sha = {c["name"]: c["sha"] for r in old for c in r.get("chunks", [])}
    old_by_id = {r["id"]: r for r in old}
    reports, problems = [], []

    jobs = []
    sty = Styra(cfg["styra"]["username"], cfg["styra"]["password"])
    for rep in REPORTS:
        jobs.append(("styra:" + rep["key"], rep["name"], "styra", rep))
    for f in cfg.get("kobo", []):
        jobs.append((None, f.get("name") or f.get("uid"), "kobo", f))

    for rid, name, kind, src in jobs:
        if ONLY and (rid or "") not in ONLY and name not in ONLY:
            continue
        say(f"== {name}")
        try:
            if kind == "styra":
                csvp = os.path.join(WORK, src["key"] + ".csv")
                sty.export(src, csvp)
                fid = "styra-" + src["key"]
                header, chunks = split(fid, csvp, src.get("date"))
            else:
                uid, csvp = kobo_form(src)
                rid = uid
                fid = "kobo-" + uid
                header, chunks = split(fid, csvp, None)
            os.remove(csvp)
            sent = 0
            for c in chunks:
                if old_sha.get(c["name"]) != c["sha"]:
                    with open(os.path.join(WORK, c["name"]), "rb") as fh:
                        server("put", name=c["name"], b64=base64.b64encode(fh.read()).decode())
                    sent += 1
                os.remove(os.path.join(WORK, c["name"]))
            reports.append({"id": rid, "name": name, "kind": kind, "chunks": chunks,
                            "rows": sum(c["rows"] for c in chunks), "updated": datetime.datetime.now(TZ).isoformat(timespec="minutes")})
            say(f"   {sum(c['rows'] for c in chunks):,} rows in {len(chunks)} pieces, {sent} changed and uploaded")
        except Exception as e:
            problems.append(f"{name}: {e}")
            say("   FAILED:", e)
            if rid and rid in old_by_id:            # keep yesterday's data for this report
                reports.append(old_by_id[rid])

    if ONLY:                                          # test runs keep the other reports as they were
        have = {r["id"] for r in reports}
        reports += [r for r in old if r["id"] not in have]
    manifest = {"updated": datetime.datetime.now(TZ).isoformat(timespec="minutes"), "reports": reports, "problems": problems}
    server("put", name="manifest.json", text=json.dumps(manifest), note=f"{len(reports)} reports" + (f", {len(problems)} problems" if problems else ""))
    server("prune", keep=[c["name"] for r in reports for c in r["chunks"]])
    say("Done.", "Problems: " + "; ".join(problems) if problems else "No problems.")
    shutil.rmtree(WORK, ignore_errors=True)
    if problems and not reports:
        sys.exit(1)


if __name__ == "__main__":
    main()
