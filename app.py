import os, re, json, sqlite3, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import requests
import streamlit as st
from bs4 import BeautifulSoup
import pandas as pd

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "lp_research.db")
HEADERS = {"User-Agent": "LPResearchIntelligence/0.1 (research prototype)"}

DEFAULT_DATAPOINTS = [{
    "name": "AUM",
    "definition": "Total market value of all financial and alternative investments, including private equity, private debt, hedge funds, infrastructure, real estate and natural resources.",
    "terms": ["Total Assets", "Total investment", "Fair value", "Asset under management", "Market value"],
    "active": 1
}]

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS datapoints (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
          definition TEXT NOT NULL, terms TEXT NOT NULL, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS firms (
          id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, domain TEXT NOT NULL,
          researched_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Complete'
        );
        CREATE TABLE IF NOT EXISTS findings (
          id INTEGER PRIMARY KEY AUTOINCREMENT, firm_id INTEGER NOT NULL,
          datapoint_name TEXT NOT NULL, value TEXT, status TEXT NOT NULL,
          confidence TEXT, source_type TEXT, source_url TEXT, excerpt TEXT,
          FOREIGN KEY(firm_id) REFERENCES firms(id)
        );
        """)
        count = c.execute("SELECT COUNT(*) FROM datapoints").fetchone()[0]
        if count == 0:
            for d in DEFAULT_DATAPOINTS:
                c.execute("INSERT INTO datapoints(name,definition,terms,active) VALUES(?,?,?,?)",
                          (d["name"], d["definition"], json.dumps(d["terms"]), d["active"]))

def valid_http_url(url):
    p = urlparse(url.strip())
    return p.scheme in ("http", "https") and bool(p.netloc)

def fetch_page(url, timeout=12):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type","").lower():
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script","style","noscript","svg","header","footer"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        return {"url": r.url, "title": soup.title.get_text(" ", strip=True) if soup.title else "",
                "text": text[:18000]}
    except Exception:
        return None

def crawl_site(start_url, max_pages=10):
    host = urlparse(start_url).netloc.lower()
    queue, seen, pages = [start_url], set(), []
    while queue and len(pages) < max_pages:
        u = queue.pop(0)
        if u in seen: continue
        seen.add(u)
        page = fetch_page(u)
        if not page: continue
        pages.append({**page, "source_type":"Firm website"})
        try:
            html = requests.get(u, headers=HEADERS, timeout=12).text
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                nxt = urljoin(u, a["href"]).split("#")[0]
                p = urlparse(nxt)
                if p.scheme in ("http","https") and p.netloc.lower() == host and nxt not in seen:
                    if not any(x in p.path.lower() for x in [".pdf",".jpg",".png",".zip"]):
                        queue.append(nxt)
        except Exception:
            pass
    return pages

def external_search(query, max_results=5):
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key: return []
    try:
        r = requests.post("https://api.tavily.com/search",
          json={"api_key":key, "query":query, "search_depth":"advanced",
                "max_results":max_results, "include_raw_content":True}, timeout=25)
        r.raise_for_status()
        out=[]
        for x in r.json().get("results",[]):
            txt = x.get("raw_content") or x.get("content") or ""
            out.append({"url":x.get("url",""),"title":x.get("title",""),
                        "text":txt[:18000],"source_type":"External source"})
        return out
    except Exception:
        return []

def extract_with_openai(datapoint, pages):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key: return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        evidence = [{"url":p["url"],"source_type":p["source_type"],"title":p.get("title",""),
                     "text":p["text"][:7000]} for p in pages[:18]]
        prompt = f"""Extract the datapoint only when supported by the supplied evidence.
Datapoint: {datapoint['name']}
Definition: {datapoint['definition']}
Candidate terms: {', '.join(datapoint['terms'])}
Return JSON with keys: value (string or null), confidence (High/Medium/Low),
source_url (exact supplied URL or null), source_type, excerpt (verbatim short evidence excerpt).
Do not infer or calculate. Distinguish AUM from firm-wide balance-sheet assets where possible.
If no reliable evidence, return null value and null source fields.
Evidence: {json.dumps(evidence, ensure_ascii=False)}"""
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL","gpt-4.1-mini"),
            messages=[{"role":"user","content":prompt}],
            response_format={"type":"json_object"}, temperature=0)
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return None

def heuristic_extract(dp, pages):
    terms = [t.lower() for t in dp["terms"]]
    candidates=[]
    for p in pages:
        text=p["text"]
        for term in terms:
            for m in re.finditer(re.escape(term), text, re.I):
                snippet=text[max(0,m.start()-160):min(len(text),m.end()+220)]
                money=re.search(r'(?:(?:US\$|USD|EUR|GBP|[$€£])\s*)?\d[\d,.]*(?:\s?(?:bn|billion|million|mn|m|k|trillion))?',snippet,re.I)
                if money:
                    candidates.append((p,term,snippet,money.group(0)))
    if not candidates: return None
    p,term,excerpt,value=candidates[0]
    return {"value":value.strip(),"confidence":"Low","source_url":p["url"],
            "source_type":p["source_type"],"excerpt":excerpt.strip()}

def research(url, datapoints, max_pages):
    pages = crawl_site(url, max_pages=max_pages)
    domain = urlparse(url).netloc
    all_results=[]
    for dp in datapoints:
        external = external_search(f'"{domain}" {dp["name"]} { " ".join(dp["terms"][:4]) }', 5)
        evidence = pages + external
        found = extract_with_openai(dp, evidence)
        if not found:
            found = heuristic_extract(dp, evidence)
        if found and found.get("value") and found.get("source_url"):
            all_results.append({"datapoint_name":dp["name"],"value":found["value"],
              "status":"Found","confidence":found.get("confidence","Low"),
              "source_type":found.get("source_type","Unknown"),
              "source_url":found["source_url"],"excerpt":found.get("excerpt","")})
        else:
            all_results.append({"datapoint_name":dp["name"],"value":"",
              "status":"Not Found","confidence":"—","source_type":"",
              "source_url":"","excerpt":""})
    return all_results, len(pages), sum(1 for p in pages if p["source_type"]=="External source")

def save_run(url, results):
    domain=urlparse(url).netloc
    with db() as c:
        cur=c.execute("INSERT INTO firms(url,domain,researched_at,status) VALUES(?,?,?,?)",
          (url,domain,datetime.now(timezone.utc).isoformat(),"Complete"))
        fid=cur.lastrowid
        for r in results:
            c.execute("""INSERT INTO findings(firm_id,datapoint_name,value,status,confidence,source_type,source_url,excerpt)
                         VALUES(?,?,?,?,?,?,?,?)""",
              (fid,r["datapoint_name"],r["value"],r["status"],r["confidence"],
               r["source_type"],r["source_url"],r["excerpt"]))
        return fid

st.set_page_config(page_title="LP Research Intelligence", page_icon="◈", layout="wide")
init_db()
st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 1450px;}
.hero {padding: 1.5rem 1.8rem; border-radius: 18px; background: linear-gradient(120deg,#14263d,#274d67); color:white; margin-bottom:1.2rem;}
.hero h1 {color:white; margin:0 0 .35rem 0;}
.hero p {color:#d7e5ef; margin:0;}
.small-muted {color:#728096;font-size:.9rem;}
</style>
""", unsafe_allow_html=True)
st.markdown('<div class="hero"><h1>LP Research Intelligence</h1><p>Research limited partners, capture evidence, and build a reusable datapoint library.</p></div>', unsafe_allow_html=True)

with db() as c:
    firms=c.execute("SELECT * FROM firms ORDER BY id DESC").fetchall()
    dps=c.execute("SELECT * FROM datapoints ORDER BY id").fetchall()
    total_findings=c.execute("SELECT COUNT(*) FROM findings").fetchone()[0]

tab_dash, tab_new, tab_lib, tab_history = st.tabs(["Overview","New research","Datapoint library","Research history"])

with tab_dash:
    a,b,c1=st.columns(3)
    a.metric("Firms researched",len(firms))
    b.metric("Datapoints configured",len(dps))
    c1.metric("Evidence records saved",total_findings)
    st.subheader("Recent research")
    if firms:
        st.dataframe(pd.DataFrame([dict(x) for x in firms])[["domain","url","researched_at","status"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No research runs yet. Start with a firm URL in **New research**.")
    st.caption("Prototype note: research quality improves when OPENAI_API_KEY and TAVILY_API_KEY are configured.")

with tab_new:
    st.subheader("Start a firm research run")
    with st.form("research_form"):
        url=st.text_input("Firm website URL", placeholder="https://www.example.com")
        max_pages=st.slider("Maximum firm-site pages to inspect",3,25,10)
        selected=[d for d in dps if d["active"]]
        st.write("Active datapoints:", ", ".join(d["name"] for d in selected) if selected else "None")
        submitted=st.form_submit_button("Research firm", type="primary")
    if submitted:
        if not valid_http_url(url):
            st.error("Enter a valid http:// or https:// URL.")
        elif not selected:
            st.error("Activate at least one datapoint in the Datapoint library.")
        else:
            with st.spinner("Crawling the firm's website and checking external sources…"):
                results, site_count, ext_count=research(url.strip(),selected,max_pages)
                fid=save_run(url.strip(),results)
            st.success(f"Research saved (run #{fid}). Inspected {site_count} firm-site pages. External results: {ext_count}.")
            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
            csv=pd.DataFrame(results).to_csv(index=False).encode("utf-8")
            st.download_button("Download this run as CSV",csv,file_name=f"{urlparse(url).netloc}_research.csv",mime="text/csv")
            if not os.getenv("TAVILY_API_KEY"):
                st.warning("External search is not configured. Add TAVILY_API_KEY to .env to enable external-source discovery.")
            if not os.getenv("OPENAI_API_KEY"):
                st.warning("AI extraction is not configured. Add OPENAI_API_KEY to .env for stronger contextual extraction; current fallback is keyword-based.")

with tab_lib:
    st.subheader("Configure datapoints")
    st.caption("Add datapoints as your research process evolves. Terms are used to locate candidate evidence.")
    with st.form("add_dp"):
        name=st.text_input("Datapoint name",placeholder="e.g. Number of funds")
        definition=st.text_area("Definition / qualification criteria",height=90)
        terms=st.text_input("Alternative terms (comma-separated)",placeholder="e.g. funds raised, fund count, vehicles")
        add=st.form_submit_button("Add datapoint")
    if add:
        if not name.strip() or not definition.strip():
            st.error("Name and definition are required.")
        else:
            try:
                with db() as c:
                    c.execute("INSERT INTO datapoints(name,definition,terms,active) VALUES(?,?,?,1)",
                              (name.strip(),definition.strip(),json.dumps([x.strip() for x in terms.split(",") if x.strip()])))
                st.success("Datapoint added. It will be included in the next research run.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("A datapoint with that name already exists.")
    for d in dps:
        with st.expander(f"{'🟢' if d['active'] else '⚪'} {d['name']}"):
            st.write(d["definition"])
            st.caption("Search terms: " + ", ".join(json.loads(d["terms"])))
            col1,col2=st.columns([1,5])
            if col1.button("Deactivate" if d["active"] else "Activate",key=f"toggle{d['id']}"):
                with db() as c: c.execute("UPDATE datapoints SET active=? WHERE id=?", (0 if d["active"] else 1,d["id"]))
                st.rerun()
            if col2.button("Delete datapoint",key=f"del{d['id']}"):
                with db() as c: c.execute("DELETE FROM datapoints WHERE id=?",(d["id"],))
                st.rerun()

with tab_history:
    st.subheader("Saved research history")
    with db() as c:
        runs=c.execute("SELECT * FROM firms ORDER BY id DESC").fetchall()
    if not runs:
        st.info("Saved research runs will appear here.")
    for run in runs:
        with st.expander(f"{run['domain']} · {run['researched_at'][:19]} · Run #{run['id']}"):
            with db() as c:
                rows=c.execute("SELECT * FROM findings WHERE firm_id=? ORDER BY datapoint_name",(run["id"],)).fetchall()
            df=pd.DataFrame([dict(x) for x in rows])
            if not df.empty:
                st.dataframe(df[["datapoint_name","value","status","confidence","source_type","source_url","excerpt"]],
                             use_container_width=True,hide_index=True)
                st.download_button("Export run CSV",df.to_csv(index=False).encode("utf-8"),
                                   file_name=f"{run['domain']}_run_{run['id']}.csv",mime="text/csv",key=f"csv{run['id']}")
