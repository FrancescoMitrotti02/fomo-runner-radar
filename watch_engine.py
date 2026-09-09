import os, time, json, sqlite3, re, html
from pathlib import Path
from urllib.parse import urlparse
import requests

DB_PATH = Path("fomo_runner_radar.db")
EARLY_RUNNER_THRESHOLD = float(os.getenv("EARLY_RUNNER_THRESHOLD", "72"))
WATCH_RECHECK_SECONDS = max(900, int(os.getenv("WATCH_RECHECK_SECONDS", "1800")))
WATCH_CHECKS_PER_CYCLE = max(1, int(os.getenv("WATCH_CHECKS_PER_CYCLE", "3")))
HTTP_TIMEOUT = max(5, int(os.getenv("WATCH_HTTP_TIMEOUT", "12")))

DEX = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1"
HONEYPOT = "https://api.honeypot.is"

CHAIN_IDS = {
    "ethereum": 1, "eth": 1,
    "bsc": 56, "binance-smart-chain": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "avalanche": 43114,
    "optimism": 10,
    "base": 8453,
    "fantom": 250,
    "linea": 59144,
    "mantle": 5000,
}


def db():
    return sqlite3.connect(DB_PATH)


def ensure_watch_db():
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS watchlist (
        token_address TEXT PRIMARY KEY,
        symbol TEXT,
        name TEXT,
        promoted_at INTEGER,
        last_checked INTEGER,
        runner_score REAL,
        chain TEXT,
        risk_score REAL,
        risk_level TEXT,
        fraud_flags_json TEXT,
        security_json TEXT,
        social_json TEXT,
        public_summary TEXT,
        status TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS watch_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        token_address TEXT,
        event TEXT,
        details_json TEXT
    )""")
    con.commit(); con.close()


def clamp(x, a=0, b=100):
    return max(a, min(b, x))


def latest_metric(addr):
    con = db(); con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM token_metrics WHERE token_address=? ORDER BY ts DESC LIMIT 1", (addr,)).fetchone()
    con.close()
    return dict(row) if row else {}


def metric_return(addr, field, hours):
    con = db()
    rows = con.execute(f"SELECT ts,{field} FROM token_metrics WHERE token_address=? AND {field} IS NOT NULL ORDER BY ts", (addr,)).fetchall()
    con.close()
    if len(rows) < 2: return None
    latest_ts, latest_v = rows[-1]
    cutoff = latest_ts - hours*3600
    old = [r for r in rows if r[0] <= cutoff]
    if not old or old[-1][1] in (0, None): return None
    return (float(latest_v)/float(old[-1][1]) - 1) * 100


def rank_velocity(addr, board="trending", hours=6):
    con = db()
    rows = con.execute("SELECT ts,rank FROM board_snapshots WHERE token_address=? AND board=? AND rank IS NOT NULL ORDER BY ts", (addr, board)).fetchall()
    con.close()
    if len(rows) < 2: return None
    latest_ts, newr = rows[-1]
    cutoff = latest_ts - hours*3600
    old = [r for r in rows if r[0] <= cutoff]
    if not old: old = [rows[0]]
    return float(old[-1][1] - newr)


def thesis_count(addr, hours=6):
    con = db()
    try:
        row = con.execute("SELECT COUNT(*) FROM thesis_snapshots WHERE token_address=? AND ts>=?", (addr, int(time.time())-hours*3600)).fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def board_presence(addr):
    con = db(); row = con.execute("SELECT COUNT(DISTINCT board) FROM board_snapshots WHERE token_address=?", (addr,)).fetchone(); con.close()
    return int(row[0] or 0)


def board_rank_for(addr, board):
    con = db(); row = con.execute("SELECT rank FROM board_snapshots WHERE token_address=? AND board=? AND rank IS NOT NULL ORDER BY ts DESC LIMIT 1", (addr, board)).fetchone(); con.close()
    return int(row[0]) if row else None


def best_rank(addr):
    con = db(); row = con.execute("SELECT MIN(rank) FROM board_snapshots WHERE token_address=? AND rank IS NOT NULL", (addr,)).fetchone(); con.close()
    return int(row[0]) if row and row[0] is not None else None


def age_minutes(addr):
    con = db(); row = con.execute("SELECT MIN(ts),MAX(ts) FROM token_metrics WHERE token_address=?", (addr,)).fetchone(); con.close()
    if not row or row[0] is None or row[1] is None: return 0
    return max(0, (row[1]-row[0])/60)


def runner_score(addr):
    m = latest_metric(addr)
    mc, liq, vol = m.get("market_cap"), m.get("liquidity"), m.get("volume_24h")
    presence = board_presence(addr); trank = board_rank_for(addr, "trending"); brank = best_rank(addr)
    rank_component = 45
    if trank is not None: rank_component = clamp(105 - trank*2.0)
    elif brank is not None: rank_component = clamp(95 - brank*1.5)
    board_component = clamp(25 + presence*22)
    fomo = .72*rank_component + .28*board_component
    volume = clamp((vol/mc)/1.5*100) if mc and mc > 0 and vol is not None else 35
    if liq is None: liq_score = 30
    else:
        abs_score = 100 if liq>=1_000_000 else 90 if liq>=500_000 else 80 if liq>=250_000 else 68 if liq>=100_000 else 55 if liq>=50_000 else 42 if liq>=25_000 else 28 if liq>=10_000 else 10
        ratio_score = clamp(((liq/mc)-.02)/(.25-.02)*100) if mc and mc>0 else 40
        liq_score = .60*abs_score + .40*ratio_score
    if not mc or mc<=0: asym=35
    elif mc<=100_000: asym=96
    elif mc<=300_000: asym=92
    elif mc<=1_000_000: asym=84
    elif mc<=3_000_000: asym=74
    elif mc<=10_000_000: asym=60
    elif mc<=30_000_000: asym=45
    elif mc<=100_000_000: asym=28
    else: asym=12
    thesis = clamp(thesis_count(addr, 6)*12)
    risk=0
    if liq is None or liq<10_000: risk += 30
    elif liq<25_000: risk += 20
    elif liq<50_000: risk += 10
    if mc and liq:
        ratio=liq/mc
        if ratio<.03: risk+=25
        elif ratio<.07: risk+=12
    if mc and mc<50_000: risk+=10
    r5=metric_return(addr,"price",5/60); r15=metric_return(addr,"price",15/60); r1=metric_return(addr,"price",1); r6=metric_return(addr,"price",6)
    l1=metric_return(addr,"liquidity",1); l6=metric_return(addr,"liquidity",6); h1=metric_return(addr,"holders",1); rh=rank_velocity(addr,"trending",6)
    centered=lambda ret,f=1: 50 if ret is None else clamp(50+ret*f)
    momentum=centered(r5,1)*.18+centered(r15,.8)*.18+centered(r1,.55)*.28+centered(r6,.22)*.20+(50 if rh is None else clamp(50+rh*3))*.16
    liq_acc=50 if l1 is None and l6 is None else centered(l1,.8)*.55+centered(l6,.35)*.45
    holder_acc=50 if h1 is None else centered(h1,.8)
    hw=clamp(age_minutes(addr)/360*100)/100
    structural=fomo*.28+volume*.22+liq_score*.20+asym*.18+thesis*.12
    dynamic=momentum*.50+liq_acc*.25+holder_acc*.15+thesis*.10
    return clamp(structural*(1-hw*.65)+dynamic*(hw*.65)-clamp(risk)*.25)


def _safe_get(url, **kwargs):
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent":"FOMO-Runner-Radar/5.0"}, **kwargs)
        r.raise_for_status(); return r.json()
    except Exception:
        return None


def discover_pair(addr):
    data = _safe_get(f"{DEX}/latest/dex/search", params={"q": addr}) or {}
    pairs = data.get("pairs") or []
    exact=[]
    for p in pairs:
        ba=str((p.get("baseToken") or {}).get("address") or "")
        qa=str((p.get("quoteToken") or {}).get("address") or "")
        if addr.lower() in (ba.lower(), qa.lower()): exact.append(p)
    pool = exact or pairs
    if not pool: return None
    def liq(p):
        try: return float((p.get("liquidity") or {}).get("usd") or 0)
        except: return 0
    return max(pool, key=liq)


def _flag(flags, severity, code, text, source):
    flags.append({"severity":severity,"code":code,"text":text,"source":source})


def security_analysis(addr, pair):
    flags=[]; raw={"dexscreener": pair or {}}
    chain=(pair or {}).get("chainId") or "unknown"
    mc=(pair or {}).get("marketCap") or (pair or {}).get("fdv")
    liq=((pair or {}).get("liquidity") or {}).get("usd")
    created=(pair or {}).get("pairCreatedAt")
    try:
        if liq is not None and float(liq)<10_000: _flag(flags,"HIGH","LOW_LIQ",f"Liquidità molto bassa (${float(liq):,.0f})","DexScreener")
        elif liq is not None and float(liq)<25_000: _flag(flags,"MEDIUM","LOW_LIQ",f"Liquidità ridotta (${float(liq):,.0f})","DexScreener")
        if liq and mc and float(mc)>0 and float(liq)/float(mc)<.03: _flag(flags,"HIGH","LIQ_MC","Rapporto liquidità/market cap sotto 3%","DexScreener")
    except: pass
    if created:
        try:
            age_h=(time.time()-float(created)/1000)/3600
            if age_h<.25: _flag(flags,"MEDIUM","VERY_NEW",f"Pool creato da circa {max(1,int(age_h*60))} minuti","DexScreener")
            elif age_h<2: _flag(flags,"LOW","NEW_POOL",f"Pool molto recente ({age_h:.1f}h)","DexScreener")
        except: pass
    tx=(pair or {}).get("txns") or {}; h1=tx.get("h1") or tx.get("m5") or {}
    try:
        buys=float(h1.get("buys") or 0); sells=float(h1.get("sells") or 0); total=buys+sells
        if total>=20 and (min(buys,sells)/max(buys,sells,1))<.08:
            _flag(flags,"MEDIUM","TX_IMBALANCE",f"Forte squilibrio buy/sell ({int(buys)}/{int(sells)})","DexScreener")
    except: pass

    chain_id=CHAIN_IDS.get(str(chain).lower())
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", addr) and chain_id:
        gp_headers={"User-Agent":"FOMO-Runner-Radar/5.0"}
        tok=os.getenv("GOPLUS_ACCESS_TOKEN","").strip()
        if tok: gp_headers["Authorization"]=f"Bearer {tok}"
        try:
            r=requests.get(f"{GOPLUS}/token_security/{chain_id}",params={"contract_addresses":addr},headers=gp_headers,timeout=HTTP_TIMEOUT)
            if r.ok:
                gp=r.json(); raw["goplus"]=gp
                result=gp.get("result") or {}; sec=result.get(addr.lower()) or result.get(addr) or (next(iter(result.values())) if isinstance(result,dict) and result else {})
                critical={"is_honeypot":"Honeypot rilevato","cannot_sell_all":"Vendita completa bloccata","owner_change_balance":"Owner può modificare i saldi","selfdestruct":"Contratto può autodistruggersi"}
                high={"hidden_owner":"Owner nascosto","is_blacklisted":"Funzione blacklist presente","transfer_pausable":"Trasferimenti sospendibili","personal_slippage_modifiable":"Slippage/tasse modificabili per indirizzo","is_mintable":"Token mintabile","is_proxy":"Contratto proxy/modificabile"}
                for k,t in critical.items():
                    if str(sec.get(k))=="1": _flag(flags,"CRITICAL",k.upper(),t,"GoPlus")
                for k,t in high.items():
                    if str(sec.get(k))=="1": _flag(flags,"HIGH",k.upper(),t,"GoPlus")
                if str(sec.get("is_open_source"))=="0": _flag(flags,"HIGH","CLOSED_SOURCE","Contratto non open-source/verificato","GoPlus")
                for tax_key,label in (("buy_tax","Buy tax"),("sell_tax","Sell tax")):
                    try:
                        tax=float(sec.get(tax_key) or 0)*100
                        if tax>=15: _flag(flags,"HIGH",tax_key.upper(),f"{label} elevata ({tax:.1f}%)","GoPlus")
                        elif tax>=8: _flag(flags,"MEDIUM",tax_key.upper(),f"{label} significativa ({tax:.1f}%)","GoPlus")
                    except: pass
                holders=sec.get("holders") or []
                try:
                    top=[]
                    for h in holders[:10]:
                        tag=str(h.get("tag") or "").lower()
                        if "burn" in tag or "dead" in tag or str(h.get("is_locked"))=="1": continue
                        top.append(float(h.get("percent") or 0)*100)
                    conc=sum(top)
                    if conc>=60: _flag(flags,"HIGH","TOP10_CONC",f"Top holder non-lock/burn molto concentrati (~{conc:.0f}%)","GoPlus")
                    elif conc>=40: _flag(flags,"MEDIUM","TOP10_CONC",f"Concentrazione top holder rilevante (~{conc:.0f}%)","GoPlus")
                except: pass
        except Exception as e:
            raw["goplus_error"]=str(e)

        if chain_id in (1,56,8453):
            hp=_safe_get(f"{HONEYPOT}/v2/IsHoneypot", params={"address":addr,"chainID":chain_id})
            if hp:
                raw["honeypot_is"]=hp
                hr=hp.get("honeypotResult") or {}
                if hr.get("isHoneypot") is True: _flag(flags,"CRITICAL","HONEYPOT","Simulazione buy/sell indica honeypot","Honeypot.is")
                sim=hp.get("simulationResult") or {}
                for key,label in (("buyTax","Buy tax"),("sellTax","Sell tax"),("transferTax","Transfer tax")):
                    try:
                        tax=float(sim.get(key) or 0)
                        if tax>=15: _flag(flags,"HIGH",key.upper(),f"{label} elevata ({tax:.1f}%)","Honeypot.is")
                        elif tax>=8: _flag(flags,"MEDIUM",key.upper(),f"{label} significativa ({tax:.1f}%)","Honeypot.is")
                    except: pass
    else:
        raw["contract_security_note"]="Controlli contract-specific EVM non applicabili o chain non mappata; restano i controlli di mercato/social."

    coverage = bool(pair) or bool(raw.get("goplus")) or bool(raw.get("honeypot_is"))
    weights={"CRITICAL":45,"HIGH":22,"MEDIUM":10,"LOW":4}
    risk=min(100,sum(weights.get(f["severity"],0) for f in flags))
    if not coverage:
        # Non confondere assenza di dati con assenza di rischio.
        risk=max(risk,35)
        level="NESSUNA ANOMALIA RILEVABILE"
        _flag(flags,"MEDIUM","NO_SECURITY_DATA","Fonti anti-frode esterne temporaneamente non disponibili o token non ancora indicizzato","Sistema")
    elif any(f["severity"]=="CRITICAL" for f in flags): level="CRITICO"
    elif risk>=45: level="ALTO"
    elif risk>=25: level="MEDIO"
    elif risk>0: level="BASSO"
    else: level="NESSUN PROBLEMA RILEVATO"
    raw["coverage_ok"] = coverage
    return chain, risk, level, flags, raw


def _ddg(q, limit=6):
    if os.getenv("WEB_SOCIAL_SEARCH","1") == "0": return []
    try:
        r=requests.get("https://html.duckduckgo.com/html/",params={"q":q},headers={"User-Agent":"Mozilla/5.0"},timeout=HTTP_TIMEOUT)
        if not r.ok: return []
        text=r.text
        titles=re.findall(r'class="result__a"[^>]*>(.*?)</a>',text,re.S|re.I)
        snippets=re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>',text,re.S|re.I)
        clean=lambda s: re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]+>"," ",s))).strip()
        out=[]
        for i,t in enumerate(titles[:limit]): out.append({"title":clean(t),"snippet":clean(snippets[i]) if i<len(snippets) else ""})
        return out
    except: return []


def social_research(addr, symbol, name, pair):
    info=(pair or {}).get("info") or {}
    socials=info.get("socials") or []; websites=info.get("websites") or []
    qname=(name or symbol or "token").strip()
    web=_ddg(f'"{addr}" OR "{qname}" crypto token')
    x=_ddg(f'site:x.com "{addr}" OR "{symbol}" crypto') if symbol else []
    reddit=_ddg(f'site:reddit.com "{addr}" OR "{symbol}" crypto') if symbol else []
    alltext=" ".join((r.get("title","")+" "+r.get("snippet","")).lower() for r in web+x+reddit)
    positive=sum(alltext.count(k) for k in ("bullish","strong community","growing","trending","viral","legit","promising"))
    negative=sum(alltext.count(k) for k in ("scam","rug","honeypot","warning","avoid","fake","fraud"))
    hype=sum(alltext.count(k) for k in ("100x","moon","gem","ape","pump","buy now"))
    footprint=len(web)+len(x)+len(reddit)+len(socials)+len(websites)
    if footprint==0:
        perception="Completamente sconosciuta o non indicizzata: nessuna presenza web/social significativa rilevata automaticamente."
    elif footprint<=3:
        perception="Presenza pubblica molto debole: poche tracce web/social; progetto ancora poco conosciuto."
    elif negative>=max(2,positive+1):
        perception="Presenza pubblica esistente, ma i risultati automatici contengono più segnali di cautela/accuse che segnali positivi. Verifica manuale consigliata."
    elif hype>=max(3,positive):
        perception="Presenza pubblica soprattutto speculativa/promozionale: molte parole da hype rispetto a segnali di reputazione verificabile."
    elif positive>negative:
        perception="Presenza pubblica discreta con tono automatico tendenzialmente favorevole, senza che questo costituisca una verifica di affidabilità."
    else:
        perception="Presenza pubblica rilevabile ma percezione mista/neutra; non emerge un consenso netto dai risultati automatici."
    return {"web_results":web,"x_results":x,"reddit_results":reddit,"dex_socials":socials,"dex_websites":websites,"boosts":(pair or {}).get("boosts") or {},"footprint":footprint,"positive_hits":positive,"negative_hits":negative,"hype_hits":hype}, perception


def analyze_candidate(addr, symbol, name, score):
    pair=discover_pair(addr)
    chain,risk,level,flags,security=security_analysis(addr,pair)
    if risk <= 24 and not any(f["severity"] in ("CRITICAL","HIGH") for f in flags):
        social, summary=social_research(addr,symbol,name,pair)
        status="WEB+SOCIAL COMPLETATO"
    else:
        social={"skipped":True,"reason":"Rischi tecnici/materiali rilevati prima della verifica reputazionale."}
        summary="Verifica web/social non usata come filtro finale perché i controlli tecnici hanno già rilevato rischi materiali."
        status="RISCHIO TECNICO"
    now=int(time.time())
    con=db()
    con.execute("""UPDATE watchlist SET last_checked=?,runner_score=?,chain=?,risk_score=?,risk_level=?,fraud_flags_json=?,security_json=?,social_json=?,public_summary=?,status=? WHERE token_address=?""",
                (now,score,chain,risk,level,json.dumps(flags,ensure_ascii=False),json.dumps(security,ensure_ascii=False),json.dumps(social,ensure_ascii=False),summary,status,addr))
    con.execute("INSERT INTO watch_events(ts,token_address,event,details_json) VALUES(?,?,?,?)",(now,addr,"analysis",json.dumps({"risk":risk,"level":level,"flags":flags},ensure_ascii=False)))
    con.commit(); con.close()


def process_watchlist():
    ensure_watch_db(); now=int(time.time())
    con=db(); con.row_factory=sqlite3.Row
    tokens=con.execute("SELECT token_address,symbol,name FROM tokens").fetchall()
    existing={r["token_address"]:dict(r) for r in con.execute("SELECT * FROM watchlist").fetchall()}
    candidates=[]
    for t in tokens:
        addr=t["token_address"]
        try: score=runner_score(addr)
        except Exception: continue
        if addr in existing:
            con.execute("UPDATE watchlist SET runner_score=?,symbol=?,name=? WHERE token_address=?",(score,t["symbol"],t["name"],addr))
        if score >= EARLY_RUNNER_THRESHOLD:
            if addr not in existing:
                con.execute("""INSERT OR IGNORE INTO watchlist(token_address,symbol,name,promoted_at,last_checked,runner_score,risk_score,risk_level,fraud_flags_json,security_json,social_json,public_summary,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (addr,t["symbol"],t["name"],now,0,score,None,"IN ANALISI","[]","{}","{}","In attesa dei controlli anti-frode.","IN ANALISI"))
                con.execute("INSERT INTO watch_events(ts,token_address,event,details_json) VALUES(?,?,?,?)",(now,addr,"promoted",json.dumps({"runner_score":score})))
                candidates.append((addr,t["symbol"],t["name"],score,0))
            else:
                lc=int(existing[addr].get("last_checked") or 0)
                if now-lc >= WATCH_RECHECK_SECONDS:
                    candidates.append((addr,t["symbol"],t["name"],score,lc))
    con.commit(); con.close()
    candidates.sort(key=lambda x:(x[4] != 0,-x[3]))
    for addr,sym,name,score,_ in candidates[:WATCH_CHECKS_PER_CYCLE]:
        try: analyze_candidate(addr,sym,name,score)
        except Exception as e:
            con=db(); con.execute("UPDATE watchlist SET last_checked=?,status=?,public_summary=? WHERE token_address=?",(now,"ERRORE ANALISI",f"Analisi temporaneamente non riuscita: {e}",addr)); con.commit(); con.close()
    return len(candidates)


ensure_watch_db()
