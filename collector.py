
import os, time, json, sqlite3, requests
from pathlib import Path

DB_PATH=Path("fomo_runner_radar.db")
BASE=os.getenv("FOMOSCAN_BASE","https://api.fomoscan.sh")
KEY=os.getenv("FOMOSCAN_API_KEY","")
INTERVAL=int(os.getenv("FOMO_SCAN_INTERVAL","60"))

if not KEY:
    raise SystemExit("Imposta FOMOSCAN_API_KEY nell'ambiente.")

def headers():
    return {"Authorization":f"Bearer {KEY}","Accept":"application/json"}

def unpack_list(obj):
    if isinstance(obj,list): return obj
    if not isinstance(obj,dict): return []
    for k in ("data","items","results","tokens","leaderboard","rows"):
        v=obj.get(k)
        if isinstance(v,list): return v
        if isinstance(v,dict):
            for kk in ("items","rows","tokens","data"):
                vv=v.get(kk)
                if isinstance(vv,list): return vv
    return []

def first(d,*keys,default=None):
    for k in keys:
        if isinstance(d,dict) and k in d and d[k] is not None:
            return d[k]
    return default

def num(v):
    try:
        if isinstance(v,str):
            v=v.replace(",","").replace("$","").replace("%","").strip()
        return float(v)
    except: return None

def parse(item,idx):
    token=item.get("token") if isinstance(item,dict) and isinstance(item.get("token"),dict) else item
    addr=first(token,"tokenAddress","address","mint","contractAddress","contract","token_address") or first(item,"tokenAddress","address","mint","contractAddress","contract","token_address")
    symbol=first(token,"symbol","ticker",default="") or first(item,"symbol","ticker",default="")
    name=first(token,"name","tokenName",default="") or first(item,"name","tokenName",default="")
    rank=first(item,"rank","position","index",default=idx)
    metric=first(item,"value","score","holders","holderCount","count","marketCap","market_cap")
    return addr,symbol,name,rank,num(metric)

def ensure_db():
    con=sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS tokens (
        token_address TEXT PRIMARY KEY,symbol TEXT,name TEXT,first_seen INTEGER,
        first_source TEXT,first_rank INTEGER,metadata_json TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS board_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,board TEXT,token_address TEXT,
        rank INTEGER,metric_value REAL,payload_json TEXT)""")
    con.commit(); con.close()

def one_cycle():
    ts=int(time.time())
    con=sqlite3.connect(DB_PATH)
    for board in ("trending","most-held","graduated"):
        r=requests.get(f"{BASE}/v2/leaderboard/tokens/{board}",headers=headers(),timeout=20)
        r.raise_for_status()
        for idx,item in enumerate(unpack_list(r.json()),1):
            if not isinstance(item,dict): continue
            addr,symbol,name,rank,metric=parse(item,idx)
            if not addr: continue
            con.execute("""INSERT OR IGNORE INTO tokens(token_address,symbol,name,first_seen,first_source,first_rank,metadata_json)
                           VALUES(?,?,?,?,?,?,?)""",
                        (addr,symbol,name,ts,board,rank,json.dumps(item,ensure_ascii=False)))
            con.execute("""INSERT INTO board_snapshots(ts,board,token_address,rank,metric_value,payload_json)
                           VALUES(?,?,?,?,?,?)""",
                        (ts,board,addr,rank,metric,json.dumps(item,ensure_ascii=False)))
    con.commit(); con.close()
    print("scan",ts,"ok")

ensure_db()
while True:
    try:
        one_cycle()
    except Exception as e:
        print("errore:",e)
    time.sleep(INTERVAL)
