# TrendCart backend: Flask + SQLite.   Start with:  python app.py
import io, json, os, re, secrets, sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from flask import Flask, Response, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "trendcart.db")
PORT = int(os.environ.get("PORT", 3000))

# Secret used to sign login tokens. Generated once and saved in .secret
# (or set the SECRET_KEY environment variable when hosting online).
SECRET = os.environ.get("SECRET_KEY")
if not SECRET:
    path = os.path.join(BASE, ".secret")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    SECRET = open(path).read().strip()
signer = URLSafeTimedSerializer(SECRET, salt="trendcart-login")
TOKEN_MAX_AGE = 7 * 24 * 3600  # 7 days

# Checkout settings (change these to suit your store)
DELIVERY_FEE = 49          # rupees, charged when the cart is below FREE_DELIVERY_ABOVE
FREE_DELIVERY_ABOVE = 999  # rupees, free delivery from this subtotal
ETA_DAYS = 5               # "Delivery by" = order date + this many days

# Order tracking. Stages: placed -> packed -> shipped -> out_for_delivery -> delivered  (or cancelled)
# AUTO_TRACKING = True : an order moves ahead by itself as days pass (packed after 1 day, shipped after 2 days,
#                        out for delivery the day before delivery, delivered on the delivery date).
# AUTO_TRACKING = False: only the "status" column of the orders table decides (change it in DB Browser).
AUTO_TRACKING = True
STAGES = [("placed", "Order placed"), ("packed", "Packed"), ("shipped", "Shipped"),
          ("out_for_delivery", "Out for delivery"), ("delivered", "Delivered")]
STAGE_KEYS = [k for k, _ in STAGES]

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS products (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,
  cat   TEXT NOT NULL,
  price INTEGER NOT NULL,             -- in rupees
  icon  TEXT NOT NULL DEFAULT 'tee',  -- jacket, sneaker, bag, tee, cap, watch, sunglasses, hoodie
  tag   TEXT NOT NULL DEFAULT 'shop', -- featured, new, shop
  desc  TEXT NOT NULL DEFAULT '',
  fill  TEXT,                         -- icon colour (when no photo)
  bg    TEXT,                         -- card background colour
  img   TEXT                          -- optional photo URL; shown instead of the icon
);
CREATE TABLE IF NOT EXISTS cart_items (
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  size       TEXT NOT NULL DEFAULT '',
  qty        INTEGER NOT NULL CHECK (qty > 0),
  PRIMARY KEY (user_id, product_id, size)
);
CREATE TABLE IF NOT EXISTS addresses (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  phone      TEXT NOT NULL,
  pincode    TEXT NOT NULL,
  line1      TEXT NOT NULL,
  city       TEXT NOT NULL,
  state      TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS orders (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  total      INTEGER NOT NULL,
  status     TEXT NOT NULL DEFAULT 'placed',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS order_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id INTEGER,
  name       TEXT NOT NULL,   -- copied at purchase time so old orders stay correct
  price      INTEGER NOT NULL,
  qty        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  email      TEXT NOT NULL,
  message    TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS subscribers (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

ORDER_COLUMNS = {
    "subtotal":       "INTEGER NOT NULL DEFAULT 0",
    "delivery_fee":   "INTEGER NOT NULL DEFAULT 0",
    "ship_name":      "TEXT",
    "ship_phone":     "TEXT",
    "ship_pincode":   "TEXT",
    "ship_address":   "TEXT",
    "ship_city":      "TEXT",
    "ship_state":     "TEXT",
    "payment_method": "TEXT NOT NULL DEFAULT 'cod'",   # upi | card | cod
    "payment_status": "TEXT NOT NULL DEFAULT 'pending'",  # paid | pending (cod)
    "payment_detail": "TEXT",                          # UPI id or "Card •••• 1234"
    "eta":            "TEXT",
}


def add_missing_columns(con, table, cols):
    have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols.items():
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    """Creates tables and, on the very first run, the starter products + demo admin."""
    con = connect()
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    # Older databases: add the new checkout columns to "orders" if they are missing.
    add_missing_columns(con, "orders", ORDER_COLUMNS)
    add_missing_columns(con, "order_items", {"size": "TEXT NOT NULL DEFAULT ''"})
    add_missing_columns(con, "products", {"sizes": "TEXT", "sizes_out": "TEXT NOT NULL DEFAULT ''"})
    if "size" not in {r["name"] for r in con.execute("PRAGMA table_info(cart_items)")}:
        con.executescript("""
            ALTER TABLE cart_items RENAME TO cart_items_old;
            CREATE TABLE cart_items (
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
              size TEXT NOT NULL DEFAULT '',
              qty INTEGER NOT NULL CHECK (qty > 0),
              PRIMARY KEY (user_id, product_id, size));
            INSERT INTO cart_items (user_id,product_id,size,qty) SELECT user_id,product_id,'',qty FROM cart_items_old;
            DROP TABLE cart_items_old;""")
    if con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        with open(os.path.join(BASE, "products-seed.json"), encoding="utf-8") as f:
            for p in json.load(f):
                con.execute(
                    "INSERT INTO products (id,name,cat,price,icon,tag,desc,fill,bg,img) "
                    "VALUES (:id,:name,:cat,:price,:icon,:tag,:desc,:fill,:bg,:img)", p)
    # Existing databases: fill in photo paths from the seed file where img is still empty.
    with open(os.path.join(BASE, "products-seed.json"), encoding="utf-8") as f:
        for p in json.load(f):
            if p.get("img"):
                # Fill when empty, or when the saved link points to a file that does not exist in static/.
                cur = con.execute("SELECT img FROM products WHERE id=?", (p["id"],)).fetchone()
                cur = (cur["img"] if cur else None) or ""
                local = os.path.join(app.static_folder, cur.lstrip("/").replace("/", os.sep))
                if cur == "" or (not cur.startswith("http") and not os.path.isfile(local)):
                    con.execute("UPDATE products SET img=? WHERE id=?", (p["img"], p["id"]))
    # Starter sizes for products that do not have the "sizes" column filled yet (NULL).
    # sizes = all sizes of the product (comma separated, empty = no size, e.g. bag/watch)
    # sizes_out = sizes that are currently out of stock (shown crossed out, cannot be ordered)
    con.execute("""UPDATE products SET
        sizes = CASE icon WHEN 'jacket' THEN 'S,M,L,XL,XXL' WHEN 'hoodie' THEN 'S,M,L,XL,XXL'
                          WHEN 'tee' THEN 'S,M,L,XL,XXL' WHEN 'sneaker' THEN 'UK 6,UK 7,UK 8,UK 9,UK 10' ELSE '' END,
        sizes_out = CASE icon WHEN 'jacket' THEN 'XXL' WHEN 'hoodie' THEN 'S' WHEN 'sneaker' THEN 'UK 6' ELSE '' END
        WHERE sizes IS NULL""")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute("INSERT INTO users (name,username,email,password_hash) VALUES (?,?,?,?)",
                    ("Admin", "admin", "admin@trendcart.test", generate_password_hash("1234")))
    con.commit()
    con.close()


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


# ---------- helpers ----------
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
USER_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")


def split_csv(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def size_error(p, size):
    """p: a products row. Returns an error message, or None when the size is fine."""
    sizes = split_csv(p["sizes"])
    if not sizes:
        return None if size == "" else "This product has no sizes."
    if size not in sizes:
        return "Please select a size."
    if size in split_csv(p["sizes_out"]):
        return f"Size {size} is out of stock."
    return None


def body():
    return request.get_json(silent=True) or {}


def s(v):
    return v.strip() if isinstance(v, str) else ""


def bad(code, message, field=None):
    return jsonify(error=message, field=field), code


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        token = request.headers.get("Authorization", "").replace("Bearer ", "", 1)
        try:
            uid = signer.loads(token, max_age=TOKEN_MAX_AGE)["uid"]
            user = db().execute("SELECT id,name,username,email FROM users WHERE id=?", (uid,)).fetchone()
            if not user:
                raise BadSignature("no user")
        except (BadSignature, SignatureExpired, KeyError, TypeError):
            return bad(401, "Please sign in again.")
        g.user = user
        return fn(*a, **kw)
    return wrapper


# ---------- pages ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- auth ----------
@app.post("/api/signup")
def signup():
    d = body()
    name, username, email = s(d.get("name")), s(d.get("username")), s(d.get("email"))
    password = d.get("password") if isinstance(d.get("password"), str) else ""
    if len(name) < 2:
        return bad(400, "Enter your full name (at least 2 characters).", "name")
    if not USER_RE.match(username):
        return bad(400, "Use 3–20 letters, numbers or underscores.", "username")
    if not EMAIL_RE.match(email):
        return bad(400, "Enter a valid email, like name@example.com.", "email")
    if len(password) < 8 or not re.search(r"[a-zA-Z]", password) or not re.search(r"\d", password):
        return bad(400, "Use at least 8 characters with a letter and a number.", "password")
    con = db()
    if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        return bad(409, "That username is taken. Try another.", "username")
    if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return bad(409, "An account with this email already exists.", "email")
    con.execute("INSERT INTO users (name,username,email,password_hash) VALUES (?,?,?,?)",
                (name, username, email, generate_password_hash(password)))
    con.commit()
    return jsonify(ok=True), 201


@app.post("/api/login")
def login():
    d = body()
    ident = s(d.get("identifier"))
    password = d.get("password") if isinstance(d.get("password"), str) else ""
    u = db().execute("SELECT * FROM users WHERE username=? OR email=?", (ident, ident)).fetchone()
    if not u or not check_password_hash(u["password_hash"], password):
        return bad(401, "Username/email or password is incorrect.", "password")
    return jsonify(token=signer.dumps({"uid": u["id"]}), user={"name": u["name"], "username": u["username"]})


@app.get("/api/me")
@login_required
def me():
    return jsonify(user=dict(g.user))


# ---------- products (public) ----------
@app.get("/api/products")
def products():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM products ORDER BY id")])


# ---------- cart (per logged-in user) ----------
@app.get("/api/cart")
@login_required
def get_cart():
    rows = db().execute("SELECT product_id, size, qty FROM cart_items WHERE user_id=?", (g.user["id"],))
    return jsonify([dict(r) for r in rows])


@app.put("/api/cart/<int:pid>")
@login_required
def set_cart(pid):
    d = body()
    size = s(d.get("size"))
    try:
        qty = min(int(d.get("qty", 0)), 99)
    except (TypeError, ValueError):
        qty = 0
    con = db()
    p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return bad(404, "Product not found.")
    if qty <= 0:   # removing is always allowed (even if the size went out of stock)
        con.execute("DELETE FROM cart_items WHERE user_id=? AND product_id=? AND size=?", (g.user["id"], pid, size))
    else:
        err = size_error(p, size)
        if err:
            return bad(400, err, "size")
        con.execute("INSERT INTO cart_items (user_id,product_id,size,qty) VALUES (?,?,?,?) "
                    "ON CONFLICT(user_id,product_id,size) DO UPDATE SET qty=excluded.qty",
                    (g.user["id"], pid, size, qty))
    con.commit()
    return jsonify(ok=True)


# ---------- checkout settings ----------
@app.get("/api/config")
@login_required
def config():
    return jsonify(delivery_fee=DELIVERY_FEE, free_delivery_above=FREE_DELIVERY_ABOVE, eta_days=ETA_DAYS)


# ---------- delivery addresses ----------
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
PIN_RE = re.compile(r"^[1-9]\d{5}$")
UPI_RE = re.compile(r"^[a-zA-Z0-9._-]{2,}@[a-zA-Z]{2,}$")


@app.get("/api/addresses")
@login_required
def list_addresses():
    rows = db().execute("SELECT id,name,phone,pincode,line1,city,state FROM addresses "
                        "WHERE user_id=? ORDER BY id DESC", (g.user["id"],))
    return jsonify([dict(r) for r in rows])


@app.post("/api/addresses")
@login_required
def add_address():
    d = body()
    name, phone, pin = s(d.get("name")), re.sub(r"\D", "", s(d.get("phone"))), s(d.get("pincode"))
    line1, city, state = s(d.get("line1")), s(d.get("city")), s(d.get("state"))
    if len(name) < 2:
        return bad(400, "Enter the receiver's full name.", "name")
    if not PHONE_RE.match(phone):
        return bad(400, "Enter a valid 10-digit mobile number.", "phone")
    if not PIN_RE.match(pin):
        return bad(400, "Enter a valid 6-digit pincode.", "pincode")
    if len(line1) < 5:
        return bad(400, "Enter house no., building and area.", "line1")
    if len(city) < 2:
        return bad(400, "Enter your city.", "city")
    if len(state) < 2:
        return bad(400, "Enter your state.", "state")
    con = db()
    aid = con.execute("INSERT INTO addresses (user_id,name,phone,pincode,line1,city,state) VALUES (?,?,?,?,?,?,?)",
                      (g.user["id"], name[:100], phone, pin, line1[:200], city[:80], state[:80])).lastrowid
    con.commit()
    return jsonify(id=aid, name=name[:100], phone=phone, pincode=pin, line1=line1[:200],
                   city=city[:80], state=state[:80]), 201


# ---------- orders ----------
def order_tracking(o):
    """Returns (index of the current stage, {stage: planned date}, delivery date)."""
    created = datetime.strptime(o["created_at"], "%Y-%m-%d %H:%M:%S")
    eta = date.fromisoformat(o["eta"]) if o["eta"] else created.date() + timedelta(days=ETA_DAYS)
    d0 = created.date()
    shipped = d0 + timedelta(days=2)
    out = max(eta - timedelta(days=1), shipped)
    sched = {"placed": d0, "packed": d0 + timedelta(days=1), "shipped": shipped,
             "out_for_delivery": out, "delivered": max(eta, out)}
    idx = STAGE_KEYS.index(o["status"]) if o["status"] in STAGE_KEYS else 0
    if AUTO_TRACKING:
        today = datetime.now(timezone.utc).date()
        for i, k in enumerate(STAGE_KEYS):
            if today >= sched[k]:
                idx = max(idx, i)
    return idx, sched, eta


def order_view(con, o):
    d = dict(o)
    d["items"] = [dict(i) for i in con.execute(
        "SELECT product_id,name,price,qty,size FROM order_items WHERE order_id=?", (d["id"],))]
    idx, sched, eta = order_tracking(d)
    d["eta"] = eta.isoformat()
    if d["status"] == "cancelled":
        d["tracking"], d["can_cancel"] = [], False
    else:
        last = len(STAGES) - 1
        d["status"] = STAGE_KEYS[idx]
        d["tracking"] = [{"key": k, "label": label, "date": sched[k].isoformat(),
                          "state": "done" if (i < idx or idx == last) else ("current" if i == idx else "todo")}
                         for i, (k, label) in enumerate(STAGES)]
        d["can_cancel"] = idx <= 1   # only before it is shipped
        if idx == last:
            d["delivered_on"] = sched["delivered"].isoformat()
    return d


ORDER_FIELDS = ("id,total,subtotal,delivery_fee,status,created_at,eta,payment_method,payment_status,payment_detail,"
                "ship_name,ship_phone,ship_pincode,ship_address,ship_city,ship_state")


@app.post("/api/orders")
@login_required
def place_order():
    """Body: {address_id, payment_method: upi|card|cod, upi_id?, card_last4?, items?: [{product_id, qty}]}
    - without "items"  -> orders the whole cart (and empties it)      = normal checkout
    - with "items"     -> orders only those products (cart untouched) = "Buy now"
    Prices and totals are always calculated here from the database, never trusted from the browser."""
    con, uid, d = db(), g.user["id"], body()

    # 1) what is being bought
    raw = d.get("items")
    from_cart = raw is None
    if from_cart:
        wanted = {(r["product_id"], r["size"]): r["qty"] for r in
                  con.execute("SELECT product_id, size, qty FROM cart_items WHERE user_id=?", (uid,))}
    else:
        wanted = {}
        if not isinstance(raw, list) or len(raw) > 20:
            return bad(400, "Invalid items.")
        for it in raw:
            try:
                pid, q, size = int(it.get("product_id")), int(it.get("qty", 1)), s(it.get("size"))
            except (TypeError, ValueError, AttributeError):
                return bad(400, "Invalid items.")
            wanted[(pid, size)] = min(wanted.get((pid, size), 0) + max(q, 0), 99)
    items = []
    for (pid, size), q in wanted.items():
        p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if p and q > 0:
            err = size_error(p, size)
            if err:
                return bad(400, f"{p['name']}: {err}", "size")
            items.append({"id": p["id"], "name": p["name"], "price": p["price"], "qty": q, "size": size})
    if not items:
        return bad(400, "Your cart is empty.")

    # 2) delivery address (must belong to this user)
    try:
        aid = int(d.get("address_id"))
    except (TypeError, ValueError):
        aid = 0
    addr = con.execute("SELECT * FROM addresses WHERE id=? AND user_id=?", (aid, uid)).fetchone()
    if not addr:
        return bad(400, "Please select a delivery address.", "address")

    # 3) payment (demo: no real gateway; see README to connect Razorpay etc.)
    method = d.get("payment_method")
    if method == "upi":
        upi = s(d.get("upi_id"))
        if not UPI_RE.match(upi):
            return bad(400, "Enter a valid UPI ID, like name@upi.", "upi")
        detail, pay_status = upi[:80], "paid"
    elif method == "card":
        last4 = s(d.get("card_last4"))
        if not re.fullmatch(r"\d{4}", last4):
            return bad(400, "Enter a valid card.", "card")
        detail, pay_status = f"Card \u2022\u2022\u2022\u2022 {last4}", "paid"
    elif method == "cod":
        detail, pay_status = "Cash on Delivery", "pending"
    else:
        return bad(400, "Choose a payment method.", "payment")

    # 4) totals
    subtotal = sum(i["price"] * i["qty"] for i in items)
    fee = 0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_FEE
    total = subtotal + fee
    eta = (date.today() + timedelta(days=ETA_DAYS)).isoformat()

    with con:  # one transaction: either everything is saved or nothing
        oid = con.execute(
            "INSERT INTO orders (user_id,total,subtotal,delivery_fee,ship_name,ship_phone,ship_pincode,ship_address,"
            "ship_city,ship_state,payment_method,payment_status,payment_detail,eta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, total, subtotal, fee, addr["name"], addr["phone"], addr["pincode"], addr["line1"],
                addr["city"], addr["state"], method, pay_status, detail, eta)).lastrowid
        con.executemany("INSERT INTO order_items (order_id,product_id,name,price,qty,size) VALUES (?,?,?,?,?,?)",
                        [(oid, i["id"], i["name"], i["price"], i["qty"], i["size"]) for i in items])
        if from_cart:
            con.execute("DELETE FROM cart_items WHERE user_id=?", (uid,))
    o = con.execute(f"SELECT {ORDER_FIELDS} FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(order_view(con, o)), 201


@app.post("/api/orders/<int:oid>/cancel")
@login_required
def cancel_order(oid):
    con = db()
    o = con.execute(f"SELECT {ORDER_FIELDS} FROM orders WHERE id=? AND user_id=?", (oid, g.user["id"])).fetchone()
    if not o:
        return bad(404, "Order not found.")
    v = order_view(con, o)
    if v["status"] == "cancelled":
        return bad(400, "This order is already cancelled.")
    if not v["can_cancel"]:
        return bad(400, "This order has already been shipped, so it can't be cancelled now.")
    # online payments: a real store would start the refund with the payment gateway here
    pay = "refund_pending" if o["payment_status"] == "paid" else "cancelled"
    with con:
        con.execute("UPDATE orders SET status='cancelled', payment_status=? WHERE id=?", (pay, oid))
    o = con.execute(f"SELECT {ORDER_FIELDS} FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(order_view(con, o))


@app.get("/api/orders")
@login_required
def my_orders():
    con = db()
    rows = con.execute(f"SELECT {ORDER_FIELDS} FROM orders WHERE user_id=? ORDER BY id DESC", (g.user["id"],))
    return jsonify([order_view(con, o) for o in rows])


# ---------- bill / invoice (PDF download) ----------
STORE_NAME = "TrendCart"
STORE_TAGLINE = "Clothes & Caps"
IST = timezone(timedelta(hours=5, minutes=30))
STATUS_LABEL = {"placed": "Order placed", "packed": "Packed", "shipped": "Shipped",
                "out_for_delivery": "Out for delivery", "delivered": "Delivered", "cancelled": "Cancelled"}


def invoice_no(oid):
    return f"TC-{oid:05d}"


def pay_status_text(o):
    """Same wording as the website's order page."""
    if o["payment_status"] == "refund_pending":
        return "Refund initiated"
    if o["payment_status"] == "cancelled":
        return "Cancelled"
    if o["payment_method"] == "cod":
        return "Paid in cash on delivery" if o["status"] == "delivered" else "To pay on delivery"
    return "Paid"


def build_invoice_pdf(o, user):
    """o: dict from order_view(). Returns the PDF as bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    # DejaVu Sans has the rupee sign. If the font files are missing we fall back to "Rs." so it never crashes.
    reg, bold = os.path.join(BASE, "fonts", "DejaVuSans.ttf"), os.path.join(BASE, "fonts", "DejaVuSans-Bold.ttf")
    if os.path.exists(reg) and os.path.exists(bold):
        if "TC-Regular" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("TC-Regular", reg))
            pdfmetrics.registerFont(TTFont("TC-Bold", bold))
        F, FB, rupee = "TC-Regular", "TC-Bold", "\u20b9"
    else:
        F, FB, rupee = "Helvetica", "Helvetica-Bold", "Rs. "

    def money(n):
        return f"{rupee}{n:,}"

    def fmt_date(d):
        return d.strftime("%d %b %Y")

    INK, GREY, LINE, ACCENT = colors.HexColor("#1C1712"), colors.HexColor("#6B655C"), colors.HexColor("#DDD7CB"), colors.HexColor("#E8402A")
    st = lambda name, **kw: ParagraphStyle(name, fontName=kw.pop("fontName", F), fontSize=kw.pop("fontSize", 9.5),
                                           leading=kw.pop("leading", 13.5), textColor=kw.pop("textColor", INK), **kw)
    small, small_r = st("s", textColor=GREY, fontSize=8.5, leading=12), st("sr", textColor=GREY, fontSize=8.5, leading=12, alignment=2)
    body, body_r = st("b"), st("br", alignment=2)
    head = st("h", fontName=FB, fontSize=8, leading=11, textColor=GREY)
    head_r = st("hr", fontName=FB, fontSize=8, leading=11, textColor=GREY, alignment=2)
    e = lambda v: escape(str(v or ""))

    created = datetime.strptime(o["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(IST)
    eta = date.fromisoformat(o["eta"])
    cancelled = o["status"] == "cancelled"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Bill {invoice_no(o['id'])} - {STORE_NAME}", author=STORE_NAME)
    W = doc.width
    story = []

    # header: store name (left) / BILL + number (right)
    hdr = Table([[Paragraph(f'<font name="{FB}" size="22">Trend<font color="#E8402A">Cart</font></font><br/>'
                            f'<font size="8.5" color="#6B655C">{STORE_TAGLINE}</font>', body),
                  Paragraph(f'<font name="{FB}" size="16">BILL</font><br/><font size="9" color="#6B655C">'
                            f'No. {invoice_no(o["id"])}</font>', body_r)]], colWidths=[W * .6, W * .4])
    hdr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1.2, INK),
                             ("BOTTOMPADDING", (0, 0), (-1, 0), 10), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [hdr, Spacer(1, 10)]

    if cancelled:
        note = Table([[Paragraph(f'<font name="{FB}" color="#C62828">THIS ORDER WAS CANCELLED</font>', body)]], colWidths=[W])
        note.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FDECEA")),
                                  ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
        story += [note, Spacer(1, 10)]

    # order info (left) / billed to (right)
    left = (f'<font name="{FB}" size="8" color="#6B655C">ORDER DETAILS</font><br/>'
            f'Order ID: <font name="{FB}">#{o["id"]}</font><br/>Order date: {fmt_date(created)}<br/>'
            f'Delivery by: {fmt_date(eta)}<br/>Status: {STATUS_LABEL.get(o["status"], o["status"])}')
    right = (f'<font name="{FB}" size="8" color="#6B655C">BILLED &amp; DELIVERED TO</font><br/>'
             f'<font name="{FB}">{e(o["ship_name"])}</font><br/>{e(o["ship_address"])}<br/>'
             f'{e(o["ship_city"])}, {e(o["ship_state"])} - {e(o["ship_pincode"])}<br/>Mobile: {e(o["ship_phone"])}')
    info = Table([[Paragraph(left, body), Paragraph(right, body)]], colWidths=[W * .45, W * .55])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [info, Spacer(1, 16)]

    # items table
    rows = [[Paragraph("#", head), Paragraph("PRODUCT", head), Paragraph("SIZE", head), Paragraph("QTY", head_r),
             Paragraph("PRICE", head_r), Paragraph("AMOUNT", head_r)]]
    for n, i in enumerate(o["items"], 1):
        rows.append([Paragraph(str(n), body), Paragraph(e(i["name"]), body), Paragraph(e(i["size"]) or "-", body),
                     Paragraph(str(i["qty"]), body_r), Paragraph(money(i["price"]), body_r),
                     Paragraph(f'<font name="{FB}">{money(i["price"] * i["qty"])}</font>', body_r)])
    cw = [10 * mm, W - 10 * mm - 24 * mm - 14 * mm - 28 * mm - 30 * mm, 24 * mm, 14 * mm, 28 * mm, 30 * mm]
    items = Table(rows, colWidths=cw, repeatRows=1)
    items.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F1E8")),
                               ("LINEBELOW", (0, 0), (-1, 0), .8, INK), ("LINEBELOW", (0, 1), (-1, -1), .4, LINE),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 7),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    story += [items, Spacer(1, 10)]

    # totals (right aligned)
    fee = f"{money(o['delivery_fee'])}" if o["delivery_fee"] else "FREE"
    trows = [[Paragraph("Subtotal", body), Paragraph(money(o["subtotal"]), body_r)],
             [Paragraph("Delivery charge", body), Paragraph(fee, body_r)],
             [Paragraph(f'<font name="{FB}" size="11">Total</font>', body),
              Paragraph(f'<font name="{FB}" size="11">{money(o["total"])}</font>', body_r)]]
    tot = Table(trows, colWidths=[40 * mm, 36 * mm], hAlign="RIGHT")
    tot.setStyle(TableStyle([("LINEABOVE", (0, 2), (-1, 2), .8, INK), ("TOPPADDING", (0, 0), (-1, -1), 4),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [tot, Spacer(1, 16)]

    # payment box
    pm = "UPI - " + e(o["payment_detail"]) if o["payment_method"] == "upi" else e(o["payment_detail"])
    pay = Table([[Paragraph(f'<font name="{FB}" size="8" color="#6B655C">PAYMENT</font><br/>Method: {pm}<br/>'
                            f'Status: <font name="{FB}">{pay_status_text(o)}</font>', body)]], colWidths=[W])
    pay.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .6, LINE), ("TOPPADDING", (0, 0), (-1, -1), 9),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
    story += [pay, Spacer(1, 22)]

    story += [Paragraph("Thank you for shopping with TrendCart!", st("ty", fontName=FB, alignment=1)), Spacer(1, 3),
              Paragraph("This is a computer-generated bill and does not need a signature. All amounts are in Indian Rupees (INR).",
                        st("ft", textColor=GREY, fontSize=8, leading=11, alignment=1))]

    doc.build(story)
    return buf.getvalue()


@app.get("/api/orders/<int:oid>/invoice.pdf")
@login_required
def order_invoice_pdf(oid):
    con = db()
    o = con.execute(f"SELECT {ORDER_FIELDS} FROM orders WHERE id=? AND user_id=?", (oid, g.user["id"])).fetchone()
    if not o:
        return bad(404, "Order not found.")
    try:
        pdf = build_invoice_pdf(order_view(con, o), g.user)
    except ImportError:
        return bad(500, "PDF library missing. Run:  pip install reportlab   (or just close and start start.bat again).")
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="TrendCart-Bill-{invoice_no(oid)}.pdf"',
                             "Cache-Control": "no-store"})


# ---------- newsletter & contact form (public) ----------
@app.post("/api/subscribe")
def subscribe():
    email = s(body().get("email"))
    if not EMAIL_RE.match(email):
        return bad(400, "Enter a valid email.")
    con = db()
    con.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
    con.commit()
    return jsonify(ok=True)


@app.post("/api/contact")
def contact():
    d = body()
    name, email, message = s(d.get("name")), s(d.get("email")), s(d.get("message"))
    if not name or not EMAIL_RE.match(email) or not message:
        return bad(400, "Please fill in all fields.")
    con = db()
    con.execute("INSERT INTO contact_messages (name,email,message) VALUES (?,?,?)",
                (name[:100], email[:200], message[:2000]))
    con.commit()
    return jsonify(ok=True)


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api"):
        return bad(404, "Not found.")
    return "Not found", 404


init_db()

if __name__ == "__main__":
    print(f"\n  TrendCart is running ->  http://localhost:{PORT}\n  (Close this window or press Ctrl+C to stop)\n")
    app.run(host="127.0.0.1", port=PORT)
