import os, re, io, math, statistics, tempfile, threading, requests, urllib.parse
from datetime import datetime, timezone
from dateutil import parser as dparser

# PDF (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib.units import inch

# API
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import uvicorn

# Telegram
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

def _guess_county_from_address(address:str) -> str:
    # light heuristic for demo; refine later
    a = address.lower()
    if "boca raton" in a or "palm beach" in a:
        return "Palm Beach County"
    if "fort lauderdale" in a or "broward" in a:
        return "Broward County"
    return "County Appraiser"

def make_links(address: str):
    """Generate Zillow + County Appraiser links (query-style)."""
    addr_encoded = urllib.parse.quote(address)
    zillow_url = f"https://www.zillow.com/homes/{addr_encoded}_rb/"

    county = _guess_county_from_address(address)
    if county == "Palm Beach County":
        county_url = "https://www.google.com/search?q=" + urllib.parse.quote(f"site:pbcgov.org papa {address}")
    elif county == "Broward County":
        county_url = "https://www.bcpa.net/RecAddr.asp?addr=" + urllib.parse.quote(address)
    else:
        county_url = "https://www.google.com/search?q=" + urllib.parse.quote(f"{address} county property appraiser")
    return zillow_url, county_url

# -------------------------
# PDF generator (ReportLab)
# -------------------------
def generate_pdf(subject, comps, arv, condition, rehab_cost, assignment_fee, mao_rows, dispo_price):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(tmp.name, pagesize=A4, title="Comp Packet")
    styles = getSampleStyleSheet()
    story = []

    title = f"Comp Packet – {subject['address']}"
    sub = f"{subject.get('beds','')} bd • {subject.get('baths','')} ba • {subject.get('sqft','')} sqft • Yr {subject.get('year','—')}"
    meta = f"Condition: {condition.title()} • Assignment Fee: ${assignment_fee:,.0f}"
    summary = f"ARV: ${arv:,.0f} • Rehab: ${rehab_cost:,.0f} • Dispo Ask: ${dispo_price:,.0f}"

    zillow_url, county_url = make_links(subject["address"])

    story += [
        Paragraph(title, styles['Title']),
        Paragraph(sub, styles['Normal']),
        Paragraph(meta, styles['Normal']),
        Spacer(1, 0.1*inch),
        Paragraph(f"🔗 Zillow: <a href='{zillow_url}'>{zillow_url}</a>", styles['Normal']),
        Paragraph(f"🏛 County Appraiser: <a href='{county_url}'>{county_url}</a>", styles['Normal']),
        Spacer(1, 0.2*inch),
        Paragraph(summary, styles['Heading3']),
        Spacer(1, 0.2*inch),
        Paragraph("Comps", styles['Heading3'])
    ]

    comp_hdr = ["Score","Address","Sold","Price","$/sf","Beds","Baths","Sqft","Why","Cash?"]
    comp_rows = [comp_hdr]
    for c in comps:
        comp_rows.append([
            c.get("score",""),
            c.get("address",""),
            c.get("sold_date",""),
            f"${c.get('sold_price',0):,.0f}",
            f"${(c.get('ppsf') or 0):,.0f}",
            c.get("beds",""), c.get("baths",""), f"{c.get('sqft',''):,}" if c.get("sqft") else "",
            c.get("why",""), c.get("cash_status","")
        ])
    t = Table(comp_rows, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.lightgrey),
        ("GRID",(0,0),(-1,-1), 0.25, colors.grey),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("FONTSIZE",(0,0),(-1,-1),8),
    ]))
    story += [t, Spacer(1, 0.2*inch), Paragraph("MAO Tiers", styles['Heading3'])]

    mao_hdr = ["Tier","Buyer Max","Your MAO (fee in)"]
    mao_rows_tbl = [mao_hdr] + [[r["tier"], f"${r['buyer_max']:,.0f}", f"${r['your_mao']:,.0f}"] for r in mao_rows]
    t2 = Table(mao_rows_tbl, repeatRows=1)
    t2.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.lightgrey),
        ("GRID",(0,0),(-1,-1), 0.25, colors.grey),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("FONTSIZE",(0,0),(-1,-1),9),
    ]))
    story.append(t2)

    doc.build(story)
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

    cash_ppsf = median_ppsf * 0.95
    dispo_price = round(cash_ppsf * (subject["sqft"] or 0))

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
    # optional flags; if missing, defaults apply (aggressive/fair/20000)
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

    address = re.sub(r"--\w+\s+[^\-][\S ]+?(?=\s--|$)", "", addr_and_flags).strip().rstrip(",")
    if not address:
        await update.message.reply_text("Please include an address after /comp.")
        return

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
        "• *Outputs*: Zillow + County Appraiser links, comps table, ARV, rehab, MAO tiers, dispo ask."
    )
    await update.message.reply_markdown_v2(msg)

def run_bot():
    # Clear webhook via HTTP (sync) to avoid conflicts, drop pending updates
    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": "true"},
            timeout=10
        )
    except Exception:
        pass

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("comp", comp_cmd))
    application.add_handler(CommandHandler("about", about_cmd))

    # Use the sync variant so PTB manages the event loop (avoids 'Cannot close a running event loop')
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None  # safer in some container runtimes
    )

if __name__ == "__main__":
    # Start FastAPI in a background thread, then the bot
    threading.Thread(target=run_api, daemon=True).start()
    run_bot()
