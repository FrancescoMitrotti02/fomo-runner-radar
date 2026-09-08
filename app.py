
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
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

def api_get(base, key, path, params=None):
    url = base.rstrip("/") + path
    r = requests.get(url, headers=headers(key), params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()

def unpack_list(obj):
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    for k in ("data","items","results","tokens","leaderboard","rows"):
        v = obj.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for kk in ("items","rows","tokens","data"):
                vv = v.get(kk)
                if isinstance(vv, list):
                    return vv
    return []

def first(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def num(v):
    try:
        if isinstance(v, str):
            v = v.replace(",","").replace("$","").replace("%","").strip()
        return float(v)
    except:
        return None

def parse_token_item(item, rank_fallback=None):
    token = item.get("token") if isinstance(item, dict) and isinstance(item.get("token"), dict) else item
    addr = first(token, "tokenAddress","address","mint","contractAddress","contract","token_address")
    if not addr and isinstance(item, dict):
        addr = first(item, "tokenAddress","address","mint","contractAddress","contract","token_address")
    symbol = first(token, "symbol","ticker", default="") or first(item, "symbol","ticker", default="")
    name = first(token, "name","tokenName", default="") or first(item, "name","tokenName", default="")
    rank = first(item, "rank","position","index", default=rank_fallback)
    metric = first(item, "value","score","holders","holderCount","count","marketCap","market_cap")
    d = {
        "address": addr,
        "symbol": symbol or "",
        "name": name or "",
        "rank": int(rank) if str(rank).isdigit() else rank_fallback,
        "metric": num(metric),
        "price": num(first(token,"price","priceUsd","price_usd") or first(item,"price","priceUsd","price_usd")),
        "market_cap": num(first(token,"marketCap","market_cap","mc","fdv") or first(item,"marketCap","market_cap","mc","fdv")),
        "liquidity": num(first(token,"liquidity","liquidityUsd","liquidity_usd") or first(item,"liquidity","liquidityUsd","liquidity_usd")),
        "volume_24h": num(first(token,"volume24h","volume_24h","volume24H") or first(item,"volume24h","volume_24h","volume24H")),
        "holders": num(first(token,"holders","holderCount","holder_count") or first(item,"holders","holderCount","holder_count")),
        "buys_24h": num(first(token,"buys24h","buys_24h") or first(item,"buys24h","buys_24h")),
        "sells_24h": num(first(token,"sells24h","sells_24h") or first(item,"sells24h","sells_24h")),
        "payload": item
    }
    return d

def fetch_board(base,key,board):
    path = f"/v2/leaderboard/tokens/{board}"
    raw = api_get(base,key,path)
    items = unpack_list(raw)
    out = []
    for i,x in enumerate(items,1):
        if isinstance(x, dict):
            d = parse_token_item(x, i)
            if d["address"]:
                out.append(d)
    return out, raw

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
    if s>=85: return "🔥 setup raro"
    if s>=75: return "🚀 early runner"
    if s>=62: return "👀 watchlist forte"
    if s>=48: return "⚠️ incompleta"
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
        st.caption("La chiave è caricata in modo sicuro dalle variabili ambiente di Render e non viene mostrata nell'app.")
    else:
        api_key = st.text_input("API key", type="password", placeholder="fsk_live_…")
        st.warning("⚠️ Nessuna FOMOSCAN_API_KEY trovata sul server.")
        st.caption("Per uso stabile su iPhone, aggiungi FOMOSCAN_API_KEY nelle Environment Variables di Render.")

    st.divider()
    st.header("Scanner")
    scan_boards = st.multiselect(
        "Leaderboard da scansionare",
        ["trending","most-held","graduated"],
        default=["trending","most-held","graduated"]
    )
    thesis_enabled = st.checkbox("Acquisisci thesis feed", value=True)
    do_scan = st.button("📡 SCANSIONE FOMO", type="primary", use_container_width=True)

if do_scan:
    if not api_key:
        st.error("API key FomoScan non disponibile. Controlla FOMOSCAN_API_KEY su Render oppure inseriscila manualmente.")
    else:
        ts = int(time.time())
        total = 0
        errors = []
        for board in scan_boards:
            try:
                items, raw = fetch_board(base_url,api_key,board)
                for d in items:
                    upsert_token(d["address"],d["symbol"],d["name"],board,d["rank"],d["payload"])
                    add_board_snapshot(ts,board,d["address"],d["rank"],d["metric"],d["payload"])
                    if any(d.get(k) is not None for k in ("price","market_cap","liquidity","volume_24h","holders","buys_24h","sells_24h")):
                        add_metric(ts,d["address"],d,source=board)
                    total += 1
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
        if errors:
            st.warning(" | ".join(errors))


# Quick mobile dashboard
toks_quick = get_tokens()
if not toks_quick.empty:
    quick_rows = []
    for _, t in toks_quick.head(80).iterrows():
        s = score_token(t["token_address"])
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
            s = score_token(t["token_address"])
            rows.append({
                "Symbol": t["symbol"],
                "Nome": t["name"],
                "Runner Score": round(s["score"],1),
                "Stato": band(s["score"]),
                "Δ rank 6h": s["rank_move_6h"],
                "Prezzo 1h %": s["price_1h"],
                "Prezzo 6h %": s["price_6h"],
                "Liquidità 6h %": s["liq_6h"],
                "Holder 6h %": s["holders_6h"],
                "Thesis 6h": s["thesis_6h"],
                "First source": t["first_source"],
                "CA": t["token_address"],
            })
        df = pd.DataFrame(rows).sort_values("Runner Score",ascending=False)
        st.dataframe(df,use_container_width=True,hide_index=True)
        if len(df):
            top=df.iloc[0]
            st.success(f"Radar leader: {top['Symbol']} — {top['Runner Score']}/100 ({top['Stato']})")

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
            s=score_token(t["token_address"])
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

with tabs[2]: render_board("trending")
with tabs[3]: render_board("most-held")
with tabs[4]: render_board("graduated")

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
        s=score_token(addr)
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
Questa v3 è **FOMO-first**: usa le leaderboard FOMO di FomoScan (`trending`, `most-held`, `graduated`) e il thesis feed.
Il punteggio Early Runner dà più peso a:
- salita di posizione nella leaderboard Trending;
- performance osservata 1h/6h;
- crescita di liquidità e holder;
- accelerazione delle thesis sul token.

**Runner Score non è probabilità di profitto.** Serve a ordinare i candidati e trovare anomalie prima che diventino ovvie.
""")
