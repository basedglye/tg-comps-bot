import os, re, io, math, statistics, tempfile, threading, requests
from datetime import datetime, timezone
from dateutil import parser as dparser
from jinja2 import Template
from weasyprint import HTML

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# -------------------------
# Config via ENV VARIABLES
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")           # set in Railway Variables
PORT      = int(os.getenv("PORT", "8000"))   # set to 8000 in Railway Variables

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it in Railway → Variables.")

# Rehab $/sf by condition + MAO tiers
REHAB_PSF = {"excellent": 20.0, "fair": 42.5, "poor": 85.0}
MAO_TIERS = [0.65, 0.70, 0.75]  # aggressive, standard, hot

# -------------------------
# Helpers
# -------------------------
def days_since(date_str):
    d = dparser.parse(date_str).date()
    return (datetime.now(timezone.utc).date() - d).days

def score_comp(subj, comp):
    days = comp["days_since_sale"]
    bedDiff = abs((comp.get("beds") or 0) - (subj.get("beds") or 0))
    bathDiff= abs((comp.get("baths") or 0) - (subj.get("baths") or 0))
    yrDiff  = abs((comp.get("year") or 0) - (subj.get("year") or 0)) if subj.get("year") and comp.get("year") else 0
    sizeTerm= abs(math.log((comp.get("sqft") or 1) / max(1, (subj.get("sqft") or 1))))
    score = 100 - 20*min(days,365)/365 - 30*sizeTerm - 8*bedDiff - 10*bathDiff - 10*min(yrDiff,60)/60
    return max(0, round(score))

def comp_reasons(subj, comp):
    r=[]
    if subj.get("sqft") and comp.get("sqft") and abs(comp["sqft"]-subj["sqft"])/subj["sqft"] <= 0.1: r.append("~size match")
    if comp.get("beds")==subj.get("beds"): r.append("same beds")
    if comp.get("baths")==subj.get("baths"): r.append("same baths")
    if comp.get("days_since_sale")<=45: r.append(f'{comp["days_since_sale"]}d recent')
    return " • ".join(r[:3])

def fetch_portal_comps(address:str):
    """
    Stub data so flow deploys cleanly; replace with live fetchers later.
    """
    return [
        {"address":"17267 Ventana Dr, Boca Raton, FL 33487","sold_price":650000,"sold_date":"2025-06-30","beds":3,"baths":2,"sqft":1820,"year":1992},
        {"address":"17165 Balboa Point Way, Boca Raton, FL 33487","sold_price":800000,"sold_date":"2025-07-07","beds":3,"baths":2.5,"sqft":2304,"year":1992},
        {"address":"17357 Balboa Point Way, Boca Raton, FL 33487","sold_price":735000,"sold_date":"2025-03-07","beds":4,"baths":2,"sqft":2013,"year":1992},
    ]

def verify_true_cash(comp):
    # Hook for Clerk deed/mortgage check — returns Pending in MVP
    return {"cash_status":"Pending"}

def generate_pdf(subject, comps, arv, condition, rehab_cost, assignment_fee, mao_rows, dispo_price):
    template = Template("""
    <h1>Comp Packet</h1>
    <h2>{{ subject.address }}</h2>
    <p>{{ subject.beds }} bd • {{ subject.baths }} ba • {{ "{:,}".format(subject.sqft or 0) }} sqft • Yr {{ subject.year or "—" }}</p>
    <p>Condition: <b>{{ condition|title }}</b> • Assignment Fee: <b>{{ "${:,.0f}".format(assignment_fee) }}</b></p>
    <h3>ARV: {{ "${:,.0f}".format(arv) }} • Rehab: {{ "${:,.0f}".format(rehab_cost) }} • Dispo Ask: {{ "${:,.0f}".format(dispo_price) }}</h3>
    <h3>Comps</h3>
    <table border="1" cellspacing="0" cellpadding="4">
      <tr><th>Score</th><th>Address</th><th>Sold</th><th>Price</th><th>$/sf</th><th>Beds</th><th>Baths</th><th>Sqft</th><th>Why</th><th>Cash?</th></tr>
      {% for c in comps %}
      <tr>
        <td>{{ c.score }}</td>
        <td>{{ c.address }}</td>
        <td>{{ c.sold_date }}</td>
        <td>{{ "${:,.0f}".format(c.sold_price) }}</td>
        <td>{{ "${:,.0f}".format(c.ppsf) }}</td>
        <td>{{ c.beds }}</td><td>{{ c.baths }}</td><td>{{ "{:,}".format(c.sqft or 0) }}</td>
        <td>{{ c.why }}</td>
        <td>{{ c.cash_status }}</td>
      </tr>
      {% endfor %}
    </table>
    <h3>MAO Tiers ({{ condition|title }})</h3>
    <table border="1" cellspacing="0" cellpadding="4">
      <tr><th>Tier</th><th>Buyer Max</th><th>Your MAO (fee in)</th></tr>
      {% for row in mao_rows %}
      <tr><td>{{ row.tier }}</td><td>{{ "${:,.0f}".format(row.buyer_max) }}</td><td>{{ "${:,.0f}".format(row.your_mao) }}</td></tr>
      {% endfor %}
    </table>
    """)
    html = template.render(**locals())
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    HTML(string=html).write_pdf(tmp.name)
    return tmp.name

# -------------------------
# FastAPI (internal API)
# -------------------------
api = FastAPI()

@api.post("/run_comps")
def run_comps(payload=Body(...)):
    address = payload["address"]
    condition = (payload.get("condition") or "fair").lower()            # default: fair
    assignment_fee = int(payload.get("assignment_fee") or 20000)        # default: 20000
    highlight = (payload.get("highlight_tier") or "aggressive").lower() # default: aggressive

    so = payload.get("subject_overrides") or {}
    subject = {
        "address": address,
        "beds": int(so.get("beds") or 3),
        "baths": float(so.get("baths") or 2),
        "sqft": int(so.get("sqft") or 1627),
        "year": int(so.get("year") or 1992),
    }

    raw = fetch_portal_comps(address)
    comps=[]
    for r in raw:
        r["days_since_sale"]=days_since(r["sold_date"])
        r["ppsf"] = r["sold_price"]/r["sqft"] if r.get("sqft") else None
        r["score"]=score_comp(subject, r)
        r["why"]=comp_reasons(subject, r)
        r.update(verify_true_cash(r))
        comps.append(r)

    comps = [c for c in comps if c.get("ppsf")]
    comps.sort(key=lambda x:(-x["score"], x["days_since_sale"]))

    median_ppsf = statistics.median([c["ppsf"] for c in comps])
    arv = round(median_ppsf * (subject["sqft"] or 0))

    rehab_psf = REHAB_PSF.get(condition, REHAB_PSF["fair"])
    rehab_cost = round((subject["sqft"] or 0) * rehab_psf)

    mao_rows=[]
    for t in MAO_TIERS:
        buyer_max = round(arv*t - rehab_cost)
        your_mao  = buyer_max - assignment_fee
        mao_rows.append({"tier": f"{int(t*100)}%", "buyer_max": buyer_max, "your_mao": your_mao})

    # cash lens (proxy)
    cash_ppsf = median_ppsf * 0.95
    dispo_price = round(cash_ppsf * (subject["sqft"] or 0))

    # choose highlight MAO for summary
    tier_map = {"aggressive":"65%", "standard":"70%", "hot":"75%"}
    highlight_label = tier_map.get(highlight, "65%")
    highlight_idx = {"65%":0, "70%":1, "75%":2}[highlight_label]
    highlight_mao = mao_rows[highlight_idx]["your_mao"]

    pdf_path = generate_pdf(subject, comps, arv, condition, rehab_cost, assignment_fee, mao_rows, dispo_price)
    summary = (
        f"ARV ${arv:,} • Rehab ({condition}) ${rehab_cost:,} • "
        f"{highlight_label} MAO ${highlight_mao:,} • Dispo ${dispo_price:,}"
    )
    return JSONResponse({"pdf_path": pdf_path, "summary": summary})

def run_api():
    uvicorn.run(api, host="0.0.0.0", port=PORT)

# -------------------------
# Telegram Bot
# -------------------------
def _parse_flags(text:str):
    out={}
    # accept optional flags; if missing, defaults apply (aggressive/fair/20000)
    for k in ["fee","condition","beds","baths","sqft","year","mao"]:
        m=re.search(rf"--{k}\s+([^\-][\S ]+?)(?=\s--|$)", text, re.I)
        if m: out[k.lower()]=m.group(1).strip()
    return out

async def comp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    parts = text.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage:\n/comp <address> [--condition excellent|fair|poor] [--fee 20000] [--mao aggressive|standard|hot]\n"
            "Defaults if omitted → MAO: aggressive (65%), Condition: fair, Fee: 20000"
        )
        return

    addr_and_flags = parts[1]
    fl = _parse_flags(addr_and_flags)

    # Address is everything with flags removed
    address = re.sub(r"--\w+\s+[^\-][\S ]+?(?=\s--|$)", "", addr_and_flags).strip().rstrip(",")
    if not address:
        await update.message.reply_text("Please include an address after /comp.")
        return

    # Defaults kick in if missing
    condition = (fl.get("condition") or "fair").lower()
    fee = int(float(fl.get("fee") or 20000))
    highlight = (fl.get("mao") or "aggressive").lower()

    await update.message.reply_text(
        f"Running comps for:\n{address}\n"
        f"MAO: {highlight} • Condition: {condition} • Fee: ${fee:,}\nPlease wait…"
    )

    payload = {
        "address": address,
        "condition": condition,
        "assignment_fee": fee,
        "highlight_tier": highlight,
        "subject_overrides": {
            "beds": fl.get("beds"),
            "baths": fl.get("baths"),
            "sqft": fl.get("sqft"),
            "year": fl.get("year"),
        }
    }

    base = f"http://127.0.0.1:{PORT}"
    r = requests.post(f"{base}/run_comps", json=payload, timeout=60)
    data = r.json()

    pdf_path = data.get("pdf_path")
    with open(pdf_path, "rb") as f:
        b = io.BytesIO(f.read())
        b.name = "comps_report.pdf"
        await context.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(b))

    if "summary" in data:
        await update.message.reply_text(data["summary"])

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *About CompsMAObot*\n"
        "• *MAO tiers*: aggressive 65%, standard 70%, hot 75% (applied to ARV).\n"
        "• *Defaults* if you omit flags: MAO=aggressive, condition=fair, fee=$20,000.\n"
        "• *Rehab $/sf*: Excellent $20, Fair $42.5, Poor $85 (× subject sqft).\n"
        "• *Command*: `/comp <address> [--condition excellent|fair|poor] [--fee 20000] [--mao aggressive|standard|hot]`"
    )
    await update.message.reply_markdown_v2(msg)

def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("comp", comp_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.run_polling()

if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    run_bot()
