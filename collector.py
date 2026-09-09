
import os, time, json, sqlite3, requests
from pathlib import Path

DB_PATH = Path("fomo_runner_radar.db")
BASE = os.getenv("FOMOSCAN_BASE","https://api.fomoscan.sh")
KEY = os.getenv("FOMOSCAN_API_KEY","").strip()
INTERVAL = max(30, int(os.getenv("FOMO_SCAN_INTERVAL","60")))

if not KEY:
    raise SystemExit("Imposta FOMOSCAN_API_KEY nell'ambiente.")

HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "X-Api-Key": KEY,
    "Accept":"application/json",
    "User-Agent":"FOMO-Runner-Radar/4"
}

def ensure_db():
    con=sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS tokens (
        token_address TEXT PRIMARY KEY,symbol TEXT,name TEXT,first_seen INTEGER,
        first_source TEXT,first_rank INTEGER,metadata_json TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS board_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,board TEXT,token_address TEXT,
        rank INTEGER,metric_value REAL,payload_json TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS token_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,token_address TEXT,
        price REAL,market_cap REAL,liquidity REAL,volume_24h REAL,holders REAL,
        buys_24h REAL,sells_24h REAL,source TEXT,payload_json TEXT)""")
    con.commit(); con.close()

def unpack(obj):
    if isinstance(obj,list): return obj
    if isinstance(obj,dict):
        for key in ("entries","items","data","rows","tokens","results"):
            v=obj.get(key)
            if isinstance(v,list): return v
            if isinstance(v,dict):
                got=unpack(v)
                if got: return got
    return []

def num(v):
    try:
        if isinstance(v,str):
            v=v.replace(",","").replace("$","").replace("%","").strip()
        return float(v)
    except:
        return None

def cycle():
    ts=int(time.time())
    con=sqlite3.connect(DB_PATH)
    total=0
    for board in ("trending","most-held","graduated"):
        r=requests.get(f"{BASE}/v2/leaderboard/tokens/{board}",headers=HEADERS,timeout=20)
        r.raise_for_status()
        for i,item in enumerate(unpack(r.json()),1):
            if not isinstance(item,dict): 
                continue
            addr=item.get("id")
            if not addr:
                continue
            symbol=item.get("handle") or ""
            name=item.get("label") or ""
            rank=item.get("rank",i)
            market_cap=num(item.get("marketCap"))
            price=num(item.get("price"))
            liquidity=num(item.get("liquidity"))
            volume=num(item.get("volume"))
            holders=num(item.get("memberCount"))

            con.execute("""INSERT OR IGNORE INTO tokens(
                token_address,symbol,name,first_seen,first_source,first_rank,metadata_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (addr,symbol,name,ts,board,rank,json.dumps(item,ensure_ascii=False)))

            con.execute("""UPDATE tokens SET
                symbol=CASE WHEN ?<>'' THEN ? ELSE symbol END,
                name=CASE WHEN ?<>'' THEN ? ELSE name END
                WHERE token_address=?""",
                (symbol,symbol,name,name,addr))

            con.execute("""INSERT INTO board_snapshots(
                ts,board,token_address,rank,metric_value,payload_json
            ) VALUES(?,?,?,?,?,?)""",
            (ts,board,addr,rank,market_cap,json.dumps(item,ensure_ascii=False)))

            con.execute("""INSERT INTO token_metrics(
                ts,token_address,price,market_cap,liquidity,volume_24h,holders,
                buys_24h,sells_24h,source,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (ts,addr,price,market_cap,liquidity,volume,holders,None,None,board,
             json.dumps(item,ensure_ascii=False)))
            total += 1
    con.commit(); con.close()
    print(f"scan {ts}: {total} righe")

ensure_db()
while True:
    try:
        cycle()
    except Exception as e:
        print("errore:",e)
    time.sleep(INTERVAL)
