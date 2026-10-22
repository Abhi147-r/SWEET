Goga Ji Gud - store backend (FastAPI + SQLite).

Run:  python main.py
Store: http://localhost:8000      Admin: http://localhost:8000/admin
"""
import json, os, re, secrets, sqlite3, time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE = Path(__file__).parent


def _load_env():
    """Tiny .env reader (no extra package needed): lines like KEY=value."""
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
DB_PATH = os.environ.get("DB_PATH", str(BASE / "orders.db"))
if not os.environ.get("ADMIN_PASSWORD"):
    # first run: create a random admin password, save it in .env and show it once
    _pw = secrets.token_urlsafe(9)
    os.environ["ADMIN_PASSWORD"] = _pw
    try:
        with open(BASE / ".env", "a", encoding="utf-8") as _f:
            _f.write("\nADMIN_PASSWORD=" + _pw + "\n")
    except OSError:
        pass
    print("\n  ADMIN PASSWORD (saved in .env):", _pw, "\n")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
WHATSAPP = os.environ.get("WHATSAPP", "917900681476")
PRODUCTS = {"badam": "Badam Gud", "kaju": "Kaju Gud", "special": "Badam Special",
            "kishmish": "Kishmish Gud", "plain": "Pure Desi Gud"}
STATUSES = ["New", "Confirmed", "Dispatched", "Delivered", "Cancelled"]

app = FastAPI(title="Goga Ji Gud", docs_url=None, redoc_url=None)


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY, created TEXT, token TEXT, name TEXT, phone TEXT,
            country TEXT, address TEXT, items TEXT, kg REAL, items_total INTEGER,
            cod INTEGER, total INTEGER, payment TEXT, status TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_token ON orders(token)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT OR IGNORE INTO settings VALUES('rate','250')")
        c.execute("INSERT OR IGNORE INTO settings VALUES('cod_fee','50')")


init_db()


def setting(c, k):
    return int(c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()["v"])


# ---------- models ----------
class Item(BaseModel):
    id: str
    kg: float = Field(ge=0.5, le=50)

    @field_validator("id")
    @classmethod
    def known(cls, v):
        if v not in PRODUCTS:
            raise ValueError("unknown product")
        return v

    @field_validator("kg")
    @classmethod
    def half_kg(cls, v):
        if (v * 2) % 1 != 0:
            raise ValueError("kg must be in steps of 0.5")
        return v


class OrderIn(BaseModel):
    token: str = Field(min_length=16, max_length=80)
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=6, max_length=20)
    country: str = Field(min_length=2, max_length=60)
    address: str = Field(min_length=5, max_length=400)
    items: List[Item] = Field(min_length=1, max_length=10)

    @field_validator("phone")
    @classmethod
    def phone_ok(cls, v):
        if not re.fullmatch(r"[0-9+()\-\s]{6,20}", v):
            raise ValueError("invalid phone")
        return v


class StatusIn(BaseModel):
    status: str


class SettingsIn(BaseModel):
    rate: int = Field(ge=1, le=100000)
    cod_fee: int = Field(ge=0, le=10000)


# ---------- helpers ----------
_hits = {}


def rate_limit(request: Request, limit=8, window=3600):
    fwd = request.headers.get("x-forwarded-for", "")
    ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        raise HTTPException(429, "Too many orders. Please try again later.")
    hits.append(now)
    _hits[ip] = hits


def need_admin(key):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not set on the server")
    if not key or not secrets.compare_digest(key.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password")


def row(r, admin=False):
    d = {"id": r["id"], "created": r["created"], "items": json.loads(r["items"]), "kg": r["kg"],
         "items_total": r["items_total"], "cod": r["cod"], "total": r["total"],
         "payment": r["payment"], "status": r["status"]}
    if admin:
        d.update(name=r["name"], phone=r["phone"], country=r["country"], address=r["address"])
    return d


# ---------- public API ----------
@app.get("/api/config")
def config():
    with db() as c:
        return {"rate": setting(c, "rate"), "cod_fee": setting(c, "cod_fee"), "whatsapp": WHATSAPP,
                "products": [{"id": k, "name": v} for k, v in PRODUCTS.items()]}


@app.post("/api/orders")
def create_order(o: OrderIn, request: Request):
    rate_limit(request)
    with db() as c:
        rate, cod_fee = setting(c, "rate"), setting(c, "cod_fee")
        items = [{"id": i.id, "name": PRODUCTS[i.id], "kg": i.kg, "amount": round(i.kg * rate)} for i in o.items]
        items_total = sum(i["amount"] for i in items)
        india = o.country.strip().lower() == "india"
        cod = cod_fee if india else 0
        oid = "GJ-" + secrets.token_hex(3).upper()
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, created, o.token, o.name.strip(), o.phone.strip(), o.country.strip(), o.address.strip(),
                   json.dumps(items), sum(i["kg"] for i in items), items_total, cod, items_total + cod,
                   "COD" if india else "To be confirmed", "New"))
    return {"id": oid, "items": items, "kg": sum(i["kg"] for i in items), "items_total": items_total,
            "cod": cod, "total": items_total + cod, "payment": "COD" if india else "To be confirmed"}


@app.get("/api/my-orders")
def my_orders(token: str):
    if len(token) < 16:
        raise HTTPException(400, "bad token")
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE token=? ORDER BY created DESC LIMIT 50", (token,)).fetchall()
    return [row(r) for r in rows]


# ---------- admin API ----------
@app.get("/api/admin/orders")
def admin_orders(x_admin_key: str = Header(default="")):
    need_admin(x_admin_key)
    with db() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY created DESC LIMIT 1000").fetchall()
        s = c.execute("""SELECT COUNT(*) n,
            SUM(CASE WHEN status='New' THEN 1 ELSE 0 END) new_n,
            COALESCE(SUM(CASE WHEN status!='Cancelled' THEN kg END),0) kg,
            COALESCE(SUM(CASE WHEN status!='Cancelled' THEN total END),0) revenue FROM orders""").fetchone()
        cfg = {"rate": setting(c, "rate"), "cod_fee": setting(c, "cod_fee")}
    return {"stats": {"orders": s["n"], "new": s["new_n"] or 0, "kg": s["kg"], "revenue": s["revenue"]},
            "settings": cfg, "statuses": STATUSES, "orders": [row(r, True) for r in rows]}


@app.patch("/api/admin/orders/{oid}")
def admin_status(oid: str, b: StatusIn, x_admin_key: str = Header(default="")):
    need_admin(x_admin_key)
    if b.status not in STATUSES:
        raise HTTPException(400, "bad status")
    with db() as c:
        if c.execute("UPDATE orders SET status=? WHERE id=?", (b.status, oid)).rowcount == 0:
            raise HTTPException(404, "order not found")
    return {"ok": True}


@app.put("/api/admin/settings")
def admin_settings(b: SettingsIn, x_admin_key: str = Header(default="")):
    need_admin(x_admin_key)
    with db() as c:
        c.execute("UPDATE settings SET v=? WHERE k='rate'", (str(b.rate),))
        c.execute("UPDATE settings SET v=? WHERE k='cod_fee'", (str(b.cod_fee),))
    return {"ok": True}


@app.get("/admin")
def admin_page():
    return FileResponse(BASE / "admin.html")


app.mount("/", StaticFiles(directory=str(BASE), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  Store: http://localhost:{port}\n  Admin: http://localhost:{port}/admin\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
