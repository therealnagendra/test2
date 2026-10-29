import os, sqlite3, math, time, secrets, hmac, hashlib, json, urllib.request, urllib.error
from urllib.parse import quote
from flask import Flask, request, jsonify, session, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(BASE, "parking.db"))
UPI_ID = os.environ.get("UPI_ID", "7416779528@Upi")
PAYEE = os.environ.get("UPI_NAME", "Smart Parking")
HELP_NO = os.environ.get("HELP_NUMBER", "7416779528")
GMAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
# --- YouPI PG (self-hosted UPI gateway) -------------------------------------
YOUPI_API = os.environ.get("YOUPI_API_URL", "").rstrip("/")       # e.g. https://pay.yourdomain.com  (server-to-server)
YOUPI_MERCHANT = os.environ.get("YOUPI_MERCHANT_ID", "")          # merchant UUID from YouPI merchant panel
YOUPI_SECRET = os.environ.get("YOUPI_WEBHOOK_SECRET", "")         # merchant webhook secret
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")         # this app's public URL (for return link)
YOUPI_ON = bool(YOUPI_API and YOUPI_MERCHANT and YOUPI_SECRET)
MULT = {"bike": 0.5, "car": 1.0, "suv": 1.5}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

def init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, mobile TEXT UNIQUE, pw TEXT, role TEXT);
    CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, owner_id INT, name TEXT, area TEXT, lat REAL, lng REAL,
        rate REAL, total INT, created REAL);
    CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, code TEXT UNIQUE, user_id INT, lot_id INT, vehicle TEXT,
        vehicle_no TEXT, hours REAL, hold_min INT, amount REAL, status TEXT, utr TEXT, created REAL);
    """)
    for ddl in ("ALTER TABLE lots ADD COLUMN ev INT DEFAULT 0", "ALTER TABLE bookings ADD COLUMN pay_id TEXT"):
        try: c.execute(ddl)
        except sqlite3.OperationalError: pass
    c.commit(); c.close()
init()

def me():
    uid = session.get("uid")
    if not uid: return None
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def need(role=None):
    u = me()
    if not u: return None, (jsonify(error="Login required"), 401)
    if role and u["role"] != role: return None, (jsonify(error="Not allowed"), 403)
    return u, None

def hav(a, b, c, d):
    R = 6371000; p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(x))

IST = 19800  # India time offset (seconds)

def deadline(b): return b["created"] + b["hold_min"]*60

def live_status(b, now=None):
    """Status validated against time: unpaid -> expired, paid but never entered -> noshow."""
    now = now or time.time(); st = b["status"]
    if st == "held" and now > deadline(b): return "expired"
    if st in ("paid_unverified", "paid") and now > deadline(b): return "noshow"
    return st

def active_count(lot_id):
    n = 0
    for b in db().execute("SELECT * FROM bookings WHERE lot_id=? AND status IN ('held','paid_unverified','paid','in')", (lot_id,)):
        if live_status(b) in ("held", "paid_unverified", "paid", "in"): n += 1
    return n

PRIOR = [.2]*7 + [.4, .6, .6, .6, .7, .8, .8, .7, .6, .6, .7, .85, .85, .85, .8, .7, .4, .3, .2, .2]
PRIOR = (PRIOR + [.2]*24)[:24]

def predict(lot, free_now, eta_min):
    """Simple learning model: blends a typical daily pattern with this lot's own booking history."""
    rows = db().execute("SELECT created,hours FROM bookings WHERE lot_id=?", (lot["id"],)).fetchall()
    tgt = time.time() + eta_min*60
    hr = int(((tgt + IST) % 86400) // 3600)
    n = len(rows)
    days = max(1, len({int((r["created"] + IST)//86400) for r in rows}))
    in_h = [r for r in rows if int(((r["created"] + IST) % 86400)//3600) == hr]
    avg_dur = (sum(r["hours"] for r in rows)/n) if n else 1
    hist = min(1.0, (len(in_h)/days) * avg_dur / max(1, lot["total"]))
    w = min(0.7, n/30.0)
    busy = (1-w)*PRIOR[hr] + w*hist
    model_free = lot["total"]*(1-busy)
    a = min(1.0, eta_min/90.0)
    pred = int(round((1-a)*free_now + a*model_free))
    return max(0, min(lot["total"], pred)), int(round(busy*100))

def lot_json(l, pos=None, eta=30):
    free = max(0, l["total"] - active_count(l["id"]))
    o = db().execute("SELECT name FROM users WHERE id=?", (l["owner_id"],)).fetchone()
    pf, busy = predict(l, free, eta)
    j = dict(id=l["id"], name=l["name"], area=l["area"], lat=l["lat"], lng=l["lng"], rate=l["rate"],
             total=l["total"], free=free, owner=o["name"] if o else "", owner_id=l["owner_id"],
             ev=bool(l["ev"]), pred_free=pf, pred_busy=busy)
    if pos: j["dist"] = round(hav(pos[0], pos[1], l["lat"], l["lng"]))
    return j

@app.get("/")
def index(): return send_from_directory("static", "index.html")

@app.get("/api/config")
def config(): return jsonify(gmaps_key=GMAPS_KEY, upi_id=UPI_ID, help_no=HELP_NO, youpi=YOUPI_ON)

@app.post("/api/register")
def register():
    d = request.json or {}
    name, mob, pw, role = (d.get("name") or "").strip(), (d.get("mobile") or "").strip(), d.get("password") or "", d.get("role")
    if role not in ("customer", "owner") or len(name) < 2 or not mob.isdigit() or len(mob) != 10 or len(pw) < 6:
        return jsonify(error="Name, 10-digit mobile and 6+ char password required"), 400
    try:
        cur = db().execute("INSERT INTO users(name,mobile,pw,role) VALUES(?,?,?,?)", (name, mob, generate_password_hash(pw), role))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="Mobile already registered"), 409
    session["uid"] = cur.lastrowid
    return jsonify(ok=True)

FAILS = {}

@app.post("/api/login")
def login():
    d = request.json or {}
    key = (d.get("mobile") or "").strip(); now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 600]
    if len(FAILS[key]) >= 5: return jsonify(error="Too many wrong attempts. Try again in 10 minutes."), 429
    u = db().execute("SELECT * FROM users WHERE mobile=?", ((d.get("mobile") or "").strip(),)).fetchone()
    if not u or not check_password_hash(u["pw"], d.get("password") or "") or u["role"] != d.get("role"):
        FAILS[key].append(now)
        return jsonify(error="Wrong mobile / password / role"), 401
    session["uid"] = u["id"]; return jsonify(ok=True)

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def whoami():
    u = me()
    return jsonify(user=None if not u else dict(id=u["id"], name=u["name"], mobile=u["mobile"], role=u["role"]))

@app.get("/api/lots")
def lots():
    pos = None
    try: pos = (float(request.args["lat"]), float(request.args["lng"]))
    except Exception: pass
    try: eta = max(0, min(180, int(request.args.get("eta", 30))))
    except Exception: eta = 30
    out = [lot_json(l, pos, eta) for l in db().execute("SELECT * FROM lots")]
    if pos: out.sort(key=lambda x: x["dist"])
    return jsonify(lots=out)

@app.post("/api/lots")
def add_lot():
    u, err = need("owner")
    if err: return err
    d = request.json or {}
    try:
        name, area = d["name"].strip(), (d.get("area") or "").strip()
        lat, lng, rate, total = float(d["lat"]), float(d["lng"]), float(d["rate"]), int(d["total"])
        assert 6 <= lat <= 37.6 and 67 <= lng <= 98, 'outside India'
        assert name and 0 < rate <= 5000 and 1 <= total <= 2000
    except Exception:
        return jsonify(error="Check name, rate and slots. The parking must be inside India."), 400
    db().execute("INSERT INTO lots(owner_id,name,area,lat,lng,rate,total,created,ev) VALUES(?,?,?,?,?,?,?,?,?)",
                 (u["id"], name, area, lat, lng, rate, total, time.time(), 1 if d.get("ev") else 0)); db().commit()
    return jsonify(ok=True)

@app.delete("/api/lots/<int:i>")
def del_lot(i):
    u, err = need("owner")
    if err: return err
    db().execute("DELETE FROM lots WHERE id=? AND owner_id=?", (i, u["id"])); db().commit()
    return jsonify(ok=True)

def fee(rate, vehicle, hours): return round(rate * MULT[vehicle] * hours, 2)

def bk_json(b):
    l = db().execute("SELECT name,area,lat,lng FROM lots WHERE id=?", (b["lot_id"],)).fetchone()
    u = db().execute("SELECT name,mobile FROM users WHERE id=?", (b["user_id"],)).fetchone()
    status = live_status(b)
    return dict(id=b["id"], code=b["code"], lot=l["name"] if l else "(removed)", area=l["area"] if l else "",
                lat=l["lat"] if l else None, lng=l["lng"] if l else None, vehicle=b["vehicle"], vehicle_no=b["vehicle_no"],
                hours=b["hours"], amount=b["amount"], status=status, utr=b["utr"], customer=u["name"], mobile=u["mobile"],
                hold_until=b["created"] + b["hold_min"]*60, created=b["created"])

@app.post("/api/book")
def book():
    u, err = need("customer")
    if err: return err
    d = request.json or {}
    try:
        lot = db().execute("SELECT * FROM lots WHERE id=?", (int(d["lot_id"]),)).fetchone()
        veh, hours, hold = d["vehicle"], float(d["hours"]), int(d["hold_min"])
        vno = (d.get("vehicle_no") or "").strip().upper()
        assert lot and veh in MULT and 0.5 <= hours <= 24 and hold in (15, 30, 60)
    except Exception:
        return jsonify(error="Invalid booking details"), 400
    if lot["total"] - active_count(lot["id"]) <= 0: return jsonify(error="Lot is full"), 409
    code = "SP" + secrets.token_hex(3).upper()
    cur = db().execute("INSERT INTO bookings(code,user_id,lot_id,vehicle,vehicle_no,hours,hold_min,amount,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (code, u["id"], lot["id"], veh, vno, hours, hold, fee(lot["rate"], veh, hours), "held", time.time()))
    db().commit()
    return jsonify(id=cur.lastrowid)

@app.get("/api/bookings")
def my_bookings():
    u, err = need()
    if err: return err
    if u["role"] == "customer":
        rows = db().execute("SELECT * FROM bookings WHERE user_id=? ORDER BY id DESC", (u["id"],))
    else:
        rows = db().execute("SELECT b.* FROM bookings b JOIN lots l ON l.id=b.lot_id WHERE l.owner_id=? ORDER BY b.id DESC", (u["id"],))
    return jsonify(bookings=[bk_json(b) for b in rows])

# ---------------- YouPI PG payment gateway ----------------
def youpi_call(method, path, body=None):
    """Server-to-server call to the YouPI Payment API."""
    req = urllib.request.Request(YOUPI_API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Merchant-Id": YOUPI_MERCHANT, "X-Merchant-Key": YOUPI_SECRET})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

def mark_paid(b, utr=""):
    """Held -> paid (idempotent). Called only after YouPI confirmed SUCCESS (webhook or verified status)."""
    if b["status"] in ("paid", "in", "done"): return True
    if b["status"] != "held": return False
    db().execute("UPDATE bookings SET status='paid', utr=? WHERE id=?", (utr or b["utr"], b["id"])); db().commit()
    return True

@app.post("/api/youpi/start/<int:i>")
def youpi_start(i):
    if not YOUPI_ON: return jsonify(error="YouPI gateway is not configured"), 503
    u, err = need("customer")
    if err: return err
    b = db().execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (i, u["id"])).fetchone()
    if not b: return jsonify(error="Not found"), 404
    if bk_json(b)["status"] != "held": return jsonify(error="Booking not payable (expired or already paid)"), 409
    try:
        p = youpi_call("POST", "/api/v1/payments", {"orderId": b["code"], "amount": "%.2f" % b["amount"], "currency": "INR",
                       "returnUrl": (PUBLIC_URL or request.host_url.rstrip("/")) + "/?paid=%d" % i})
    except urllib.error.HTTPError as e:
        return jsonify(error="Gateway refused: " + e.read().decode()[:160]), 502
    except Exception:
        return jsonify(error="Payment gateway is not reachable. Try again."), 502
    db().execute("UPDATE bookings SET pay_id=? WHERE id=?", (p["paymentId"], i)); db().commit()
    return jsonify(checkout_url=p["checkoutUrl"], payment_id=p["paymentId"])

def amount_ok(b, paise):
    try: return int(round(float(paise))) == int(round(b["amount"] * 100))
    except Exception: return False

@app.post("/api/youpi-webhook")
def youpi_webhook():
    """Authoritative confirmation: HMAC-SHA256 over the RAW body, constant-time compare."""
    raw = request.get_data()
    sig = request.headers.get("X-Gateway-Signature", "")
    exp = "sha256=" + hmac.new(YOUPI_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not YOUPI_SECRET or not hmac.compare_digest(sig, exp): return jsonify(error="Invalid webhook signature"), 401
    try: ev = json.loads(raw.decode())
    except Exception: return jsonify(error="Bad JSON"), 400
    b = db().execute("SELECT * FROM bookings WHERE code=?", (str(ev.get("orderId", "")),)).fetchone()
    if b and ev.get("event") == "SUCCESS":
        if not amount_ok(b, ev.get("amountPaise")): return jsonify(error="Amount mismatch"), 409
        mark_paid(b, str(ev.get("utr") or ""))
    return jsonify(received=True)

@app.get("/api/youpi/status/<int:i>")
def youpi_status(i):
    """Polled by the browser. If the webhook has not arrived yet, ask YouPI directly (verified server-side)."""
    u, err = need("customer")
    if err: return err
    b = db().execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (i, u["id"])).fetchone()
    if not b: return jsonify(error="Not found"), 404
    if b["status"] == "held" and b["pay_id"] and YOUPI_ON:
        try:
            p = youpi_call("GET", "/api/v1/payments/" + b["pay_id"])
            if p.get("status") == "SUCCESS" and p.get("orderId") == b["code"] and amount_ok(b, p.get("amountPaise")):
                mark_paid(b, str(p.get("utr") or ""))
        except Exception: pass
        b = db().execute("SELECT * FROM bookings WHERE id=?", (i,)).fetchone()
    return jsonify(status=bk_json(b)["status"])

@app.get("/api/upi/<int:i>")
def upi(i):
    u, err = need("customer")
    if err: return err
    b = db().execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (i, u["id"])).fetchone()
    if not b: return jsonify(error="Not found"), 404
    link = "upi://pay?pa=%s&pn=%s&am=%.2f&cu=INR&tn=%s" % (quote(UPI_ID, safe="@"), quote(PAYEE), b["amount"], quote("Parking " + b["code"]))
    return jsonify(link=link, amount=b["amount"], code=b["code"], upi_id=UPI_ID)

@app.post("/api/pay/<int:i>")
def pay(i):
    u, err = need("customer")
    if err: return err
    if YOUPI_ON: return jsonify(error="Pay through the YouPI checkout"), 403
    utr = ((request.json or {}).get("utr") or "").strip()
    if not (utr.isdigit() and len(utr) == 12): return jsonify(error="Enter the 12-digit UPI reference (UTR) number"), 400
    b = db().execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (i, u["id"])).fetchone()
    if not b or bk_json(b)["status"] != "held": return jsonify(error="Booking not payable (expired or already paid)"), 409
    db().execute("UPDATE bookings SET status='paid_unverified', utr=? WHERE id=?", (utr, i)); db().commit()
    return jsonify(ok=True)

def owner_booking(code):
    u, err = need("owner")
    if err: return None, None, err
    b = db().execute("SELECT b.* FROM bookings b JOIN lots l ON l.id=b.lot_id WHERE b.code=? AND l.owner_id=?", (code.upper(), u["id"])).fetchone()
    if not b: return None, None, (jsonify(error="No booking with this code at your lots"), 404)
    return u, b, None

@app.post("/api/owner/verify/<code>")
def verify(code):
    u, b, err = owner_booking(code)
    if err: return err
    if b["status"] != "paid_unverified": return jsonify(error="Not waiting for payment check"), 409
    db().execute("UPDATE bookings SET status='paid' WHERE id=?", (b["id"],)); db().commit(); return jsonify(ok=True)

@app.post("/api/owner/scan/<code>")
def scan(code):
    u, b, err = owner_booking(code)
    if err: return err
    s = live_status(b)
    if s in ("paid", "paid_unverified"):
        db().execute("UPDATE bookings SET status='in' WHERE id=?", (b["id"],)); db().commit()
        return jsonify(msg="ENTRY OK" + ("" if s == "paid" else " (payment not yet verified!)"), booking=bk_json(b))
    if s == "in":
        db().execute("UPDATE bookings SET status='done' WHERE id=?", (b["id"],)); db().commit()
        return jsonify(msg="EXIT OK - slot freed", booking=bk_json(b))
    return jsonify(error="Ticket is %s - not valid" % s), 409

@app.get("/api/stats")
def stats():
    u, err = need()
    if err: return err
    now = time.time(); paid = ("paid", "in", "done")
    if u["role"] == "customer":
        rows = db().execute("SELECT * FROM bookings WHERE user_id=?", (u["id"],)).fetchall()
        spent = sum(b["amount"] for b in rows if b["status"] in paid)
        fav = {}
        for b in rows: fav[b["lot_id"]] = fav.get(b["lot_id"], 0) + 1
        favs = []
        for lid, n in sorted(fav.items(), key=lambda x: -x[1])[:3]:
            l = db().execute("SELECT name,area FROM lots WHERE id=?", (lid,)).fetchone()
            if l: favs.append(dict(name=l["name"], area=l["area"], trips=n))
        return jsonify(spent=spent, trips=len(rows), favourites=favs)
    lots_ = db().execute("SELECT * FROM lots WHERE owner_id=?", (u["id"],)).fetchall()
    total = sum(l["total"] for l in lots_); free = sum(max(0, l["total"] - active_count(l["id"])) for l in lots_)
    rows = db().execute("SELECT b.* FROM bookings b JOIN lots l ON l.id=b.lot_id WHERE l.owner_id=?", (u["id"],)).fetchall()
    today = int((now + IST)//86400)
    hours = [0]*24; rev = {}
    for b in rows:
        hours[int(((b["created"] + IST) % 86400)//3600)] += 1
        if b["status"] in paid:
            d = int((b["created"] + IST)//86400); rev[d] = rev.get(d, 0) + b["amount"]
    days = []
    for i in range(6, -1, -1):
        d = today - i
        days.append(dict(label=time.strftime("%d %b", time.gmtime(d*86400)), revenue=round(rev.get(d, 0), 2)))
    return jsonify(lots=len(lots_), total=total, free=free, occupied=total-free,
        today=sum(1 for b in rows if int((b["created"] + IST)//86400) == today),
        revenue=round(sum(b["amount"] for b in rows if b["status"] in paid), 2),
        pending=sum(1 for b in rows if b["status"] == "paid_unverified" and live_status(b) == "paid_unverified"),
        by_hour=hours, by_day=days)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
