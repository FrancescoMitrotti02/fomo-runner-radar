
import streamlit as st
import requests, sqlite3, time, math, json, os
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

DB_PATH = Path("fomo_runner_radar.db")
DEFAULT_BASE = "https://api.fomoscan.sh"


st.set_page_config(
    page_title="FOMO Runner Radar",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
/* Mobile-first layout */
.block-container {
    max-width: 1180px;
    padding-top: 1rem;
    padding-bottom: 5rem;
}
[data-testid="stSidebar"] {
    min-width: 310px;
}
div[data-testid="stMetric"] {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    padding: 12px;
    border-radius: 16px;
}
.stButton > button {
    min-height: 48px;
    border-radius: 14px;
    font-weight: 700;
}
div[data-baseweb="tab-list"] {
    gap: 4px;
    overflow-x: auto;
    scrollbar-width: none;
}
div[data-baseweb="tab"] {
    white-space: nowrap;
}
@media (max-width: 768px) {
    .block-container {
        padding-left: .7rem;
        padding-right: .7rem;
        padding-top: .5rem;
    }
    h1 { font-size: 1.7rem !important; }
    h2 { font-size: 1.35rem !important; }
    h3 { font-size: 1.1rem !important; }
    div[data-testid="stDataFrame"] {
        overflow-x: auto;
    }
    [data-testid="column"] {
        min-width: 0 !important;
    }
}
</style>
""", unsafe_allow_html=True)


# ---------------------------
# DB
# ---------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tokens (
        token_address TEXT PRIMARY KEY,
        symbol TEXT,
        name TEXT,
        first_seen INTEGER,
        first_source TEXT,
        first_rank INTEGER,
        metadata_json TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS board_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        board TEXT,
        token_address TEXT,
        rank INTEGER,
        metric_value REAL,
        payload_json TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS token_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        token_address TEXT,
        price REAL,
        market_cap REAL,
        liquidity REAL,
        volume_24h REAL,
        holders REAL,
        buys_24h REAL,
        sells_24h REAL,
        source TEXT,
        payload_json TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS thesis_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        thesis_id TEXT,
        token_address TEXT,
        author_id TEXT,
        text TEXT,
        payload_json TEXT,
        UNIQUE(thesis_id)
    )
    """)
    con.commit()
    con.close()

def db():
    return sqlite3.connect(DB_PATH)

def upsert_token(addr, symbol="", name="", source="", rank=None, payload=None):
    if not addr:
        return
    con = db()
    cur = con.cursor()
    cur.execute("SELECT token_address FROM tokens WHERE token_address=?", (addr,))
    exists = cur.fetchone()
    if not exists:
        cur.execute("""
        INSERT INTO tokens(token_address,symbol,name,first_seen,first_source,first_rank,metadata_json)
        VALUES(?,?,?,?,?,?,?)
        """, (addr, symbol or "", name or "", int(time.time()), source or "", rank,
              json.dumps(payload or {}, ensure_ascii=False)))
    else:
        cur.execute("""
        UPDATE tokens
        SET symbol=CASE WHEN ?<>'' THEN ? ELSE symbol END,
            name=CASE WHEN ?<>'' THEN ? ELSE name END
        WHERE token_address=?
        """, (symbol or "", symbol or "", name or "", name or "", addr))
    con.commit()
    con.close()

def add_board_snapshot(ts, board, addr, rank, metric, payload):
    con = db()
    con.execute("""
    INSERT INTO board_snapshots(ts,board,token_address,rank,metric_value,payload_json)
    VALUES(?,?,?,?,?,?)
    """, (ts, board, addr, rank, metric, json.dumps(payload, ensure_ascii=False)))
    con.commit()
    con.close()

def add_metric(ts, addr, d, source="board"):
    con = db()
    con.execute("""
    INSERT INTO token_metrics(
        ts, token_address, price, market_cap, liquidity, volume_24h, holders,
        buys_24h, sells_24h, source, payload_json
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts, addr, d.get("price"), d.get("market_cap"), d.get("liquidity"),
        d.get("volume_24h"), d.get("holders"), d.get("buys_24h"), d.get("sells_24h"),
        source, json.dumps(d.get("payload") or {}, ensure_ascii=False)
    ))
    con.commit()
    con.close()

def save_thesis(ts, thesis_id, token_address, author_id, text, payload):
    if not thesis_id:
        return
    con = db()
    try:
        con.execute("""
        INSERT OR IGNORE INTO thesis_snapshots(ts,thesis_id,token_address,author_id,text,payload_json)
        VALUES(?,?,?,?,?,?)
        """, (ts, thesis_id, token_address, author_id, text,
              json.dumps(payload, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()

init_db()

# ---------------------------
# API helpers
# ---------------------------
def headers(key):
    return {
        "Authorization": f"Bearer {key}",
        "X-Api-Key": key,
        "Accept": "application/json",
        "User-Agent": "FOMO-Runner-Radar/3.1"
    }

def api_get(base, key, path, params=None):
    url = base.rstrip("/") + path
    r = requests.get(url, headers=headers(key), params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()

def unpack_list(obj):
    """
    Estrae in modo robusto la lista principale da risposte API che possono essere:
    - una lista diretta
    - {data:[...]} / {items:[...]} / {results:[...]}
    - {snapshot:{entries:[...]}} / wrapper annidati
    """
    if isinstance(obj, list):
        return obj

    preferred = (
        "items", "entries", "rows", "tokens", "results", "leaderboard",
        "rankings", "data", "snapshot", "value"
    )

    def walk(x, depth=0):
        if depth > 7:
            return []
        if isinstance(x, list):
            if x and all(isinstance(i, dict) for i in x):
                return x
            return x
        if isinstance(x, dict):
            # first try known wrapper keys
            for k in preferred:
                if k in x:
                    got = walk(x[k], depth+1)
                    if isinstance(got, list) and len(got) > 0:
                        return got
            # then inspect any nested values
            candidates = []
            for v in x.values():
                got = walk(v, depth+1)
                if isinstance(got, list) and len(got) > 0:
                    candidates.append(got)
            if candidates:
                candidates.sort(key=len, reverse=True)
                return candidates[0]
        return []

    return walk(obj)

def first(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def deep_find(obj, keys, max_depth=5):
    """Trova il primo valore con una delle chiavi, anche in oggetti annidati."""
    wanted = {k.lower() for k in keys}

    def walk(x, depth):
        if depth > max_depth:
            return None
        if isinstance(x, dict):
            # exact key pass
            for k, v in x.items():
                if str(k).lower() in wanted and v not in (None, ""):
                    return v
            for v in x.values():
                found = walk(v, depth+1)
                if found not in (None, ""):
                    return found
        elif isinstance(x, list):
            for v in x[:20]:
                found = walk(v, depth+1)
                if found not in (None, ""):
                    return found
        return None
    return walk(obj, 0)

def num(v):
    try:
        if isinstance(v, str):
            v = v.replace(",","").replace("$","").replace("%","").strip()
        return float(v)
    except:
        return None

def parse_token_item(item, rank_fallback=None):
    if not isinstance(item, dict):
        return {
            "address":None,"symbol":"","name":"","rank":rank_fallback,"metric":None,
            "price":None,"market_cap":None,"liquidity":None,"volume_24h":None,
            "holders":None,"buys_24h":None,"sells_24h":None,"payload":item
        }

    addr = deep_find(item, (
        "id","tokenAddress","token_address","address","mint","mintAddress",
        "contractAddress","contract_address","contract","tokenMint","token_mint"
    ))
    symbol = deep_find(item, ("handle","symbol","ticker","tokenSymbol","token_symbol")) or ""
    name = deep_find(item, ("label","name","tokenName","token_name")) or ""

    rank = deep_find(item, ("rank","position","ranking","place","index"))
    try:
        rank = int(rank)
    except:
        rank = rank_fallback

    metric = deep_find(item, (
        "value","score","memberCount","member_count","holders","holderCount","holder_count","count",
        "marketCap","market_cap","volume","totalHolders","total_holders"
    ))

    return {
        "address": str(addr) if addr else None,
        "symbol": str(symbol) if symbol else "",
        "name": str(name) if name else "",
        "rank": rank,
        "metric": num(metric),
        "price": num(deep_find(item, ("price","priceUsd","price_usd","usdPrice"))),
        "market_cap": num(deep_find(item, ("marketCap","market_cap","mc","fdv","fullyDilutedValuation"))),
        "liquidity": num(deep_find(item, ("liquidity","liquidityUsd","liquidity_usd","usdLiquidity"))),
        "volume_24h": num(deep_find(item, ("volume","volume24h","volume_24h","volume24H","volumeUsd24h"))),
        "holders": num(deep_find(item, ("memberCount","member_count","holders","holderCount","holder_count","totalHolders"))),
        "buys_24h": num(deep_find(item, ("buys24h","buys_24h","buyCount24h"))),
        "sells_24h": num(deep_find(item, ("sells24h","sells_24h","sellCount24h"))),
        "payload": item
    }

def api_probe(base, key):
    """Verifica chiave, piano/entitlements e usage tramite endpoint ufficiale /v2/me."""
    return api_get(base, key, "/v2/me")

def fetch_board(base,key,board):
    path = f"/v2/leaderboard/tokens/{board}"
    raw = api_get(base,key,path)
    items = unpack_list(raw)
    out = []
    rejected = []
    for i,x in enumerate(items,1):
        if isinstance(x, dict):
            d = parse_token_item(x, i)
            if d["address"]:
                out.append(d)
            else:
                rejected.append(x)
    return out, raw, len(items), rejected[:3]

def fetch_thesis(base,key,limit=100):
    raw = api_get(base,key,"/v2/thesis")
    items = unpack_list(raw)
    return items[:limit], raw

# ---------------------------
# Analytics
# ---------------------------
def get_latest_board(board):
    con = db()
    q = """
    SELECT b.* FROM board_snapshots b
    JOIN (
      SELECT token_address, MAX(ts) mts
      FROM board_snapshots WHERE board=?
      GROUP BY token_address
    ) x ON b.token_address=x.token_address AND b.ts=x.mts
    WHERE b.board=?
    """
    df = pd.read_sql_query(q, con, params=(board,board))
    con.close()
    return df

def get_tokens():
    con = db()
    df = pd.read_sql_query("SELECT * FROM tokens ORDER BY first_seen DESC", con)
    con.close()
    return df

def get_metrics():
    con = db()
    df = pd.read_sql_query("SELECT * FROM token_metrics ORDER BY ts", con)
    con.close()
    return df

def get_board_history(addr):
    con = db()
    df = pd.read_sql_query("""
    SELECT ts,board,rank,metric_value FROM board_snapshots
    WHERE token_address=? ORDER BY ts
    """, con, params=(addr,))
    con.close()
    return df

def get_theses():
    con = db()
    df = pd.read_sql_query("SELECT * FROM thesis_snapshots ORDER BY ts DESC", con)
    con.close()
    return df

def latest_metric(addr):
    con = db()
    df = pd.read_sql_query("""
    SELECT * FROM token_metrics
    WHERE token_address=?
    ORDER BY ts DESC LIMIT 1
    """, con, params=(addr,))
    con.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()

def first_metric(addr):
    con = db()
    df = pd.read_sql_query("""
    SELECT * FROM token_metrics
    WHERE token_address=?
    ORDER BY ts ASC LIMIT 1
    """, con, params=(addr,))
    con.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()

def board_presence(addr):
    """Quante leaderboard FOMO diverse hanno visto il token."""
    con = db()
    row = con.execute("""
    SELECT COUNT(DISTINCT board)
    FROM board_snapshots
    WHERE token_address=?
    """, (addr,)).fetchone()
    con.close()
    return int(row[0] or 0)

def current_best_rank(addr):
    con = db()
    row = con.execute("""
    SELECT MIN(rank)
    FROM board_snapshots
    WHERE token_address=? AND rank IS NOT NULL
    """, (addr,)).fetchone()
    con.close()
    return None if not row or row[0] is None else int(row[0])

def board_rank_for(addr, board):
    con = db()
    row = con.execute("""
    SELECT rank
    FROM board_snapshots
    WHERE token_address=? AND board=? AND rank IS NOT NULL
    ORDER BY ts DESC LIMIT 1
    """, (addr, board)).fetchone()
    con.close()
    return None if not row else int(row[0])

def snapshot_age_minutes(addr):
    con = db()
    row = con.execute("""
    SELECT MIN(ts), MAX(ts)
    FROM token_metrics
    WHERE token_address=?
    """, (addr,)).fetchone()
    con.close()
    if not row or row[0] is None or row[1] is None:
        return 0
    return max(0, (row[1]-row[0]) / 60)

def ath_stats(addr):
    con = db()
    df = pd.read_sql_query("""
    SELECT ts, price, market_cap, liquidity
    FROM token_metrics
    WHERE token_address=?
    ORDER BY ts ASC
    """, con, params=(addr,))
    con.close()
    if df.empty:
        return {
            "first_price":None,"last_price":None,"ath_price":None,"ath_x":None,
            "first_mc":None,"last_mc":None,"ath_mc":None,"ath_mc_x":None,
            "max_drawdown_pct":None
        }

    out = {}
    prices = df["price"].dropna()
    mcs = df["market_cap"].dropna()
    out["first_price"] = float(prices.iloc[0]) if len(prices) else None
    out["last_price"] = float(prices.iloc[-1]) if len(prices) else None
    out["ath_price"] = float(prices.max()) if len(prices) else None
    out["ath_x"] = (out["ath_price"]/out["first_price"]) if out["first_price"] not in (None,0) else None

    out["first_mc"] = float(mcs.iloc[0]) if len(mcs) else None
    out["last_mc"] = float(mcs.iloc[-1]) if len(mcs) else None
    out["ath_mc"] = float(mcs.max()) if len(mcs) else None
    out["ath_mc_x"] = (out["ath_mc"]/out["first_mc"]) if out["first_mc"] not in (None,0) else None

    # Price max drawdown across observed samples
    if len(prices) >= 2:
        running_max = prices.cummax()
        dd = (prices / running_max - 1.0) * 100
        out["max_drawdown_pct"] = float(dd.min())
    else:
        out["max_drawdown_pct"] = None
    return out

def current_market_scores(addr):
    """
    Scores computabili già dalla prima scansione.
    Sono indicatori euristici, non probabilità.
    """
    m = latest_metric(addr) or {}
    mc = m.get("market_cap")
    liq = m.get("liquidity")
    vol = m.get("volume_24h")
    thesis6 = thesis_count(addr, 6)
    presence = board_presence(addr)
    trank = board_rank_for(addr, "trending")
    best_rank = current_best_rank(addr)

    # FOMO position: current board rank + multi-board presence
    rank_component = 45
    if trank is not None:
        rank_component = clamp(105 - trank * 2.0)
    elif best_rank is not None:
        rank_component = clamp(95 - best_rank * 1.5)
    board_component = clamp(25 + presence * 22)
    fomo_position = 0.72*rank_component + 0.28*board_component

    # Volume intensity relative to market cap
    if mc and mc > 0 and vol is not None:
        vm = vol/mc
        volume_intensity = clamp(vm/1.5*100)
    else:
        volume_intensity = 35

    # Liquidity score: absolute + ratio
    if liq is None:
        liquidity_score = 30
    else:
        abs_score = 10
        if liq >= 1_000_000: abs_score = 100
        elif liq >= 500_000: abs_score = 90
        elif liq >= 250_000: abs_score = 80
        elif liq >= 100_000: abs_score = 68
        elif liq >= 50_000: abs_score = 55
        elif liq >= 25_000: abs_score = 42
        elif liq >= 10_000: abs_score = 28

        if mc and mc > 0:
            ratio = liq/mc
            ratio_score = clamp((ratio - 0.02)/(0.25-0.02)*100)
        else:
            ratio_score = 40
        liquidity_score = 0.60*abs_score + 0.40*ratio_score

    # Small-cap asymmetry (not "safety")
    if not mc or mc <= 0:
        asymmetry = 35
    elif mc <= 100_000: asymmetry = 96
    elif mc <= 300_000: asymmetry = 92
    elif mc <= 1_000_000: asymmetry = 84
    elif mc <= 3_000_000: asymmetry = 74
    elif mc <= 10_000_000: asymmetry = 60
    elif mc <= 30_000_000: asymmetry = 45
    elif mc <= 100_000_000: asymmetry = 28
    else: asymmetry = 12

    thesis_signal = clamp(thesis6 * 12)

    # Risk penalty
    risk = 0
    if liq is None or liq < 10_000: risk += 30
    elif liq < 25_000: risk += 20
    elif liq < 50_000: risk += 10

    if mc and liq:
        ratio = liq/mc
        if ratio < 0.03: risk += 25
        elif ratio < 0.07: risk += 12

    if mc and mc < 50_000:
        risk += 10

    return {
        "fomo_position": clamp(fomo_position),
        "volume_intensity": clamp(volume_intensity),
        "liquidity_quality": clamp(liquidity_score),
        "asymmetry": clamp(asymmetry),
        "thesis_signal": clamp(thesis_signal),
        "risk_penalty": clamp(risk),
        "market_cap": mc,
        "liquidity": liq,
        "volume_24h": vol,
        "trending_rank": trank,
        "board_presence": presence
    }

def score_token_v4(addr):
    """
    V4: combina segnali immediati + accelerazione storica quando disponibile.
    """
    base = current_market_scores(addr)

    r5m = metric_return(addr, "price", 5/60)
    r15m = metric_return(addr, "price", 15/60)
    r1 = metric_return(addr, "price", 1)
    r6 = metric_return(addr, "price", 6)
    l1 = metric_return(addr, "liquidity", 1)
    l6 = metric_return(addr, "liquidity", 6)
    h1 = metric_return(addr, "holders", 1)
    rh = rank_velocity(addr, "trending", 6)

    def centered(ret, factor=1.0):
        if ret is None:
            return 50
        return clamp(50 + ret*factor)

    momentum = (
        centered(r5m, 1.0)*0.18 +
        centered(r15m, 0.8)*0.18 +
        centered(r1, 0.55)*0.28 +
        centered(r6, 0.22)*0.20 +
        (50 if rh is None else clamp(50 + rh*3))*0.16
    )

    liquidity_accel = 50 if l1 is None and l6 is None else (
        centered(l1, 0.8)*0.55 + centered(l6, 0.35)*0.45
    )

    holder_accel = 50 if h1 is None else centered(h1, 0.8)

    # If little history exists, current market structure carries more weight.
    age_min = snapshot_age_minutes(addr)
    history_weight = clamp(age_min / 360 * 100) / 100  # ramps up over 6h

    structural = (
        base["fomo_position"]*0.28 +
        base["volume_intensity"]*0.22 +
        base["liquidity_quality"]*0.20 +
        base["asymmetry"]*0.18 +
        base["thesis_signal"]*0.12
    )
    dynamic = (
        momentum*0.50 +
        liquidity_accel*0.25 +
        holder_accel*0.15 +
        base["thesis_signal"]*0.10
    )

    score = structural*(1-history_weight*0.65) + dynamic*(history_weight*0.65)
    score -= base["risk_penalty"]*0.25
    score = clamp(score)

    # Attention/MC gap: strong FOMO attention at a still-small MC
    attention_mc = clamp(
        base["fomo_position"]*0.40 +
        base["volume_intensity"]*0.25 +
        base["thesis_signal"]*0.15 +
        base["asymmetry"]*0.20
    )

    return {
        "score": score,
        "momentum": momentum,
        "fomo_signal": base["fomo_position"],
        "volume_intensity": base["volume_intensity"],
        "liquidity_quality": base["liquidity_quality"],
        "liquidity_accel": liquidity_accel,
        "holder_accel": holder_accel,
        "asymmetry": base["asymmetry"],
        "thesis_signal": base["thesis_signal"],
        "risk_penalty": base["risk_penalty"],
        "attention_mc_gap": attention_mc,
        "price_5m": r5m,
        "price_15m": r15m,
        "price_1h": r1,
        "price_6h": r6,
        "liq_1h": l1,
        "liq_6h": l6,
        "rank_move_6h": rh,
        "age_minutes": age_min,
        "market_cap": base["market_cap"],
        "liquidity": base["liquidity"],
        "volume_24h": base["volume_24h"],
        "trending_rank": base["trending_rank"],
        "board_presence": base["board_presence"]
    }

def rank_velocity(addr, board="trending", hours=6):
    con = db()
    df = pd.read_sql_query("""
    SELECT ts,rank FROM board_snapshots
    WHERE token_address=? AND board=? ORDER BY ts
    """, con, params=(addr,board))
    con.close()
    if len(df)<2:
        return None
    latest = df.iloc[-1]
    cutoff = latest["ts"] - hours*3600
    old = df[df["ts"]<=cutoff]
    if old.empty:
        old = df.iloc[[0]]
    oldr = old.iloc[-1]["rank"]
    newr = latest["rank"]
    if pd.isna(oldr) or pd.isna(newr):
        return None
    return float(oldr-newr)  # positive = climbing

def metric_return(addr, field, hours):
    con = db()
    df = pd.read_sql_query(f"""
    SELECT ts,{field} v FROM token_metrics
    WHERE token_address=? AND {field} IS NOT NULL
    ORDER BY ts
    """, con, params=(addr,))
    con.close()
    if len(df)<2:
        return None
    latest = df.iloc[-1]
    cutoff = latest["ts"] - hours*3600
    old = df[df["ts"]<=cutoff]
    if old.empty:
        return None
    old = old.iloc[-1]
    if old["v"] in (0,None) or pd.isna(old["v"]):
        return None
    return (latest["v"]/old["v"]-1)*100

def thesis_count(addr, hours=6):
    con = db()
    now = int(time.time())
    q = "SELECT COUNT(*) c FROM thesis_snapshots WHERE token_address=? AND ts>=?"
    c = con.execute(q,(addr,now-hours*3600)).fetchone()[0]
    con.close()
    return c

def clamp(x,a=0,b=100):
    return max(a,min(b,x))

def score_token(addr):
    # FOMO-first score: platform trend + thesis velocity + observed price/liquidity growth.
    rh = rank_velocity(addr, "trending", 6)
    r1 = metric_return(addr,"price",1)
    r6 = metric_return(addr,"price",6)
    l6 = metric_return(addr,"liquidity",6)
    h6 = metric_return(addr,"holders",6)
    tc = thesis_count(addr,6)

    trend = 50 if rh is None else clamp(50 + rh*3)
    p1 = 50 if r1 is None else clamp(50 + r1*1.2)
    p6 = 50 if r6 is None else clamp(50 + r6*0.5)
    liq = 50 if l6 is None else clamp(50 + l6*0.8)
    holders = 50 if h6 is None else clamp(50 + h6*1.0)
    thesis = clamp(tc*12)

    score = trend*.30 + p1*.15 + p6*.15 + liq*.15 + holders*.15 + thesis*.10
    return {
        "score": score, "trend":trend, "p1":p1, "p6":p6, "liq":liq,
        "holders":holders, "thesis":thesis, "rank_move_6h":rh,
        "price_1h":r1, "price_6h":r6, "liq_6h":l6, "holders_6h":h6,
        "thesis_6h":tc
    }

def band(s):
    if s>=82: return "🔥 setup raro"
    if s>=72: return "🚀 early runner"
    if s>=62: return "👀 watchlist forte"
    if s>=50: return "⚠️ incompleta"
    return "🧊 debole"

# ---------------------------
# Sidebar config
# ---------------------------
st.title("📡 FOMO Runner Radar")
st.caption("Web radar FOMO-first • ottimizzato per iPhone • Trending, Most-held, Graduated, thesis velocity e performance storica.")

with st.sidebar:
    st.header("Connessione FomoScan")

    env_base = os.getenv("FOMOSCAN_BASE", DEFAULT_BASE)
    env_key = os.getenv("FOMOSCAN_API_KEY", "").strip()
    base_url = st.text_input("API base", value=env_base)

    if env_key:
        api_key = env_key
        st.success("✅ API FomoScan collegata dal server Render")
        st.caption("La chiave è caricata dalle variabili ambiente di Render e non viene mostrata.")
    else:
        api_key = st.text_input("API key", type="password", placeholder="fsk_live_…")
        st.warning("⚠️ Nessuna FOMOSCAN_API_KEY trovata sul server.")

    st.divider()
    st.header("Scanner")
    scan_boards = st.multiselect(
        "Leaderboard da scansionare",
        ["trending","most-held","graduated"],
        default=["trending","most-held","graduated"]
    )
    thesis_enabled = st.checkbox("Acquisisci thesis feed", value=True)
    test_api = st.button("🧪 TEST API / PIANO", use_container_width=True)
    do_scan = st.button("📡 SCANSIONE FOMO", type="primary", use_container_width=True)

if test_api:
    if not api_key:
        st.error("API key non disponibile.")
    else:
        try:
            me = api_probe(base_url, api_key)
            st.success("✅ FomoScan API risponde correttamente.")
            with st.expander("Dettagli piano / entitlements"):
                st.json(me)
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", "?")
            body = getattr(e.response, "text", "")
            st.error(f"❌ Test API fallito — HTTP {code}")
            st.code(body[:3000] if body else str(e))
        except Exception as e:
            st.error(f"❌ Test API fallito: {e}")

if do_scan:
    if not api_key:
        st.error("API key FomoScan non disponibile. Controlla FOMOSCAN_API_KEY su Render.")
    else:
        ts = int(time.time())
        total = 0
        errors = []
        diagnostics = []
        for board in scan_boards:
            try:
                items, raw, raw_count, rejected = fetch_board(base_url,api_key,board)
                diagnostics.append({
                    "board": board,
                    "elementi_risposta": raw_count,
                    "token_parsati": len(items),
                    "scartati": max(raw_count-len(items), 0)
                })
                for d in items:
                    upsert_token(d["address"],d["symbol"],d["name"],board,d["rank"],d["payload"])
                    add_board_snapshot(ts,board,d["address"],d["rank"],d["metric"],d["payload"])
                    if any(d.get(k) is not None for k in ("price","market_cap","liquidity","volume_24h","holders","buys_24h","sells_24h")):
                        add_metric(ts,d["address"],d,source=board)
                    total += 1

                if raw_count > 0 and len(items) == 0:
                    # Store a safe structural diagnostic in session only.
                    st.session_state[f"diag_{board}"] = {
                        "response_type": type(raw).__name__,
                        "top_keys": list(raw.keys()) if isinstance(raw,dict) else None,
                        "sample": rejected[0] if rejected else (unpack_list(raw)[0] if unpack_list(raw) else raw)
                    }
            except requests.HTTPError as e:
                code = getattr(e.response, "status_code", "?")
                body = getattr(e.response, "text", "")
                errors.append(f"{board}: HTTP {code} — {body[:500]}")
            except Exception as e:
                errors.append(f"{board}: {e}")
        if thesis_enabled:
            try:
                theses,_ = fetch_thesis(base_url,api_key,200)
                for th in theses:
                    if not isinstance(th,dict):
                        continue
                    tid = str(first(th,"id","thesisId","thesis_id",default=""))
                    token = first(th,"tokenAddress","token_address","address")
                    if not token and isinstance(th.get("token"),dict):
                        token = first(th["token"],"address","tokenAddress","mint")
                    aid = str(first(th,"authorId","author_id","userId","user_id",default=""))
                    txt = first(th,"text","body","content","thesis",default="") or ""
                    save_thesis(ts,tid,token,aid,txt,th)
            except Exception as e:
                errors.append(f"thesis: {e}")
        st.success(f"Scansione salvata: {total} righe leaderboard.")
        if diagnostics:
            st.subheader("Diagnostica scansione")
            st.dataframe(pd.DataFrame(diagnostics), use_container_width=True, hide_index=True)
        if errors:
            st.error(" | ".join(errors))

        zero_parsed = [d for d in diagnostics if d["elementi_risposta"] > 0 and d["token_parsati"] == 0]
        if zero_parsed:
            st.warning("L'API sta restituendo dati, ma alcuni campi non sono ancora riconosciuti dal parser.")
            for d in zero_parsed:
                board = d["board"]
                diag = st.session_state.get(f"diag_{board}")
                if diag:
                    with st.expander(f"Struttura risposta {board} — utile per il fix"):
                        st.json(diag)


# Quick mobile dashboard
toks_quick = get_tokens()
if not toks_quick.empty:
    quick_rows = []
    for _, t in toks_quick.head(80).iterrows():
        s = score_token_v4(t["token_address"])
        quick_rows.append((t["symbol"], s["score"], t["token_address"]))
    quick_rows.sort(key=lambda x: x[1], reverse=True)
    if quick_rows:
        q1, q2, q3 = st.columns(3)
        for col, item, label in zip(
            (q1,q2,q3),
            quick_rows[:3] + [("",0,"")] * max(0, 3-len(quick_rows)),
            ("#1 Radar","#2 Radar","#3 Radar")
        ):
            sym, sc, _ = item
            if sym:
                col.metric(label, sym, f"{sc:.0f}/100")

tabs = st.tabs([
    "🚨 Early Runner Radar",
    "🏆 Migliori dopo X tempo",
    "🏛️ Hall of Fame",
    "📈 Trending FOMO",
    "💎 Most-held",
    "🎓 Graduated",
    "🧠 Thesis Feed",
    "🔎 Token",
    "📚 Storico"
])

# ---------------------------
# Early Runner Radar
# ---------------------------
with tabs[0]:
    toks = get_tokens()
    if toks.empty:
        st.info("Esegui la prima scansione FOMO.")
    else:
        rows = []
        for _,t in toks.iterrows():
            s = score_token_v4(t["token_address"])
            rows.append({
                "Symbol": t["symbol"],
                "Nome": t["name"],
                "Runner Score": round(s["score"],1),
                "Stato": band(s["score"]),
                "FOMO": round(s["fomo_signal"],0),
                "Momentum": round(s["momentum"],0),
                "Vol/MC": round(s["volume_intensity"],0),
                "Liquidity": round(s["liquidity_quality"],0),
                "Asimmetria": round(s["asymmetry"],0),
                "Attention/MC": round(s["attention_mc_gap"],0),
                "Risk": round(s["risk_penalty"],0),
                "Prezzo 5m %": None if s["price_5m"] is None else round(s["price_5m"],2),
                "Prezzo 15m %": None if s["price_15m"] is None else round(s["price_15m"],2),
                "Prezzo 1h %": None if s["price_1h"] is None else round(s["price_1h"],2),
                "Δ rank 6h": s["rank_move_6h"],
                "MC": s["market_cap"],
                "Liq": s["liquidity"],
                "Thesis 6h": int(round(s["thesis_signal"]/12)) if s["thesis_signal"] else 0,
                "First source": t["first_source"],
                "CA": t["token_address"],
            })

        df = pd.DataFrame(rows).sort_values(["Runner Score","Attention/MC"],ascending=False)
        st.dataframe(df,use_container_width=True,hide_index=True)

        if len(df):
            top = df.iloc[0]
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Radar Leader", top["Symbol"], f"{top['Runner Score']}/100")
            c2.metric("Momentum", f"{top['Momentum']}/100")
            c3.metric("Attention/MC", f"{top['Attention/MC']}/100")
            c4.metric("Risk", f"{top['Risk']}/100")
            if top["Runner Score"] >= 72:
                st.success(f"🚨 Early Runner Alert: {top['Symbol']} — score {top['Runner Score']}/100")
            else:
                st.info("Nessun candidato supera ancora la soglia Early Runner 72/100.")

        st.caption("I punteggi sono euristiche di ranking. Non rappresentano probabilità di profitto.")

# ---------------------------
# Winners after X time
# ---------------------------
with tabs[1]:
    st.subheader("Performance reale dopo il rilevamento")
    horizon = st.select_slider(
        "Orizzonte",
        options=[1,2,4,6,12,24,48,72,168],
        value=24,
        format_func=lambda x: f"{x}h" if x<24 else f"{x//24}g"
    )
    metric_choice = st.selectbox(
        "Classifica per",
        ["Prezzo","Market cap","Liquidità","Holder"]
    )
    field = {"Prezzo":"price","Market cap":"market_cap","Liquidità":"liquidity","Holder":"holders"}[metric_choice]
    toks = get_tokens()
    rows=[]
    for _,t in toks.iterrows():
        r=metric_return(t["token_address"],field,horizon)
        if r is not None:
            s=score_token_v4(t["token_address"])
            rows.append({
                "Symbol":t["symbol"],"Nome":t["name"],
                f"{metric_choice} {horizon}h %":round(r,2),
                "Runner Score attuale":round(s["score"],1),
                "First source":t["first_source"],
                "CA":t["token_address"]
            })
    if not rows:
        st.warning(f"Non ci sono ancora abbastanza snapshot distanti {horizon} ore. Continua a scansionare nel tempo.")
    else:
        col=f"{metric_choice} {horizon}h %"
        df=pd.DataFrame(rows).sort_values(col,ascending=False).reset_index(drop=True)
        df.insert(0,"#",range(1,len(df)+1))
        st.dataframe(df,use_container_width=True,hide_index=True)


# ---------------------------
# Hall of Fame
# ---------------------------
with tabs[2]:
    st.subheader("🏛️ Runner Hall of Fame")
    st.caption("Confronta il primo snapshot registrato dal radar con i massimi successivi osservati.")
    toks = get_tokens()
    rows = []
    for _, t in toks.iterrows():
        a = ath_stats(t["token_address"])
        if a["first_price"] is None and a["first_mc"] is None:
            continue
        rows.append({
            "Symbol": t["symbol"],
            "Nome": t["name"],
            "MC discovery": a["first_mc"],
            "MC attuale": a["last_mc"],
            "ATH MC": a["ath_mc"],
            "ATH MC x": None if a["ath_mc_x"] is None else round(a["ath_mc_x"],2),
            "Prezzo discovery": a["first_price"],
            "Prezzo attuale": a["last_price"],
            "ATH prezzo x": None if a["ath_x"] is None else round(a["ath_x"],2),
            "Max drawdown %": None if a["max_drawdown_pct"] is None else round(a["max_drawdown_pct"],2),
            "First source": t["first_source"],
            "CA": t["token_address"]
        })
    if not rows:
        st.info("Servono più snapshot per costruire la Hall of Fame.")
    else:
        hof = pd.DataFrame(rows)
        hof["sort"] = hof["ATH prezzo x"].fillna(hof["ATH MC x"]).fillna(1)
        hof = hof.sort_values("sort", ascending=False).drop(columns=["sort"])
        hof.insert(0, "#", range(1, len(hof)+1))
        st.dataframe(hof, use_container_width=True, hide_index=True)

# ---------------------------
# Board views
# ---------------------------
def render_board(board):
    df=get_latest_board(board)
    toks=get_tokens()
    if df.empty:
        st.info("Nessun dato salvato per questa leaderboard.")
        return
    df=df.merge(toks[["token_address","symbol","name"]],on="token_address",how="left")
    df=df.sort_values("rank")
    show=df[["rank","symbol","name","metric_value","token_address","ts"]].copy()
    show["time"]=pd.to_datetime(show["ts"],unit="s",utc=True)
    show=show.drop(columns=["ts"])
    show.columns=["Rank","Symbol","Nome","Valore","CA","Snapshot"]
    st.dataframe(show,use_container_width=True,hide_index=True)

with tabs[3]: render_board("trending")
with tabs[4]: render_board("most-held")
with tabs[8]: render_board("graduated")

# ---------------------------
# Thesis
# ---------------------------
with tabs[5]:
    th=get_theses()
    if th.empty:
        st.info("Nessuna thesis acquisita.")
    else:
        th["time"]=pd.to_datetime(th["ts"],unit="s",utc=True)
        st.dataframe(
            th[["time","token_address","author_id","text"]].head(300),
            use_container_width=True,hide_index=True
        )

# ---------------------------
# Single token
# ---------------------------
with tabs[6]:
    addr=st.text_input("Token address FOMO/Solana")
    if addr:
        bh=get_board_history(addr)
        mets=get_metrics()
        mets=mets[mets["token_address"]==addr].copy()
        if not bh.empty:
            bh["time"]=pd.to_datetime(bh["ts"],unit="s",utc=True)
            st.subheader("Movimento in leaderboard")
            for b,g in bh.groupby("board"):
                st.write(f"**{b}**")
                st.line_chart(g.set_index("time")["rank"])
        if not mets.empty:
            mets["time"]=pd.to_datetime(mets["ts"],unit="s",utc=True)
            if mets["price"].notna().sum()>1:
                st.subheader("Prezzo osservato")
                st.line_chart(mets.dropna(subset=["price"]).set_index("time")["price"])
            st.dataframe(
                mets[["time","price","market_cap","liquidity","volume_24h","holders","buys_24h","sells_24h","source"]],
                use_container_width=True,hide_index=True
            )
        s=score_token_v4(addr)
        st.metric("Runner Score",f"{s['score']:.1f}/100",band(s["score"]))

# ---------------------------
# History / diagnostics
# ---------------------------
with tabs[7]:
    st.write("Token rilevati:",len(get_tokens()))
    st.write("Snapshot metriche:",len(get_metrics()))
    st.write("Thesis salvate:",len(get_theses()))
    st.caption("Il radar migliora con scansioni ripetute: la classifica 'dopo X tempo' richiede almeno due snapshot sufficientemente distanti.")

st.divider()
st.markdown("""
### Logica del radar
Questa v4 è **FOMO-first**: usa le leaderboard FOMO di FomoScan (`trending`, `most-held`, `graduated`) e il thesis feed.
La v4 usa lo schema reale FomoScan (`id`, `handle`, `label`, `volume`, `marketCap`, `price`, `liquidity`). Il punteggio Early Runner dà più peso a:
- salita di posizione nella leaderboard Trending;
- performance osservata 1h/6h;
- crescita di liquidità e holder;
- accelerazione delle thesis sul token.

**Runner Score non è probabilità di profitto.** Serve a ordinare i candidati e trovare anomalie prima che diventino ovvie.
""")
