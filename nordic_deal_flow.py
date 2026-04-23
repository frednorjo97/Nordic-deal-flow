"""
Nordic Deal Flow Agent v3
=========================
Skraper nordiske eiendomskilder, analyserer med Claude API,
og sender daglig e-post kl. 09:00 norsk tid.
 
v3 – Rikere data, kortere format:
- Henter ut yield, WAULT, leieinntekt, occupancy, pris/kvm
- Identifiserer katalysatorer og leietakermiks
- Kort oppsummering (maks 2 setninger) + strukturerte nøkkeltall
"""
 
import os
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, quote_plus
 
import feedparser
import requests
from bs4 import BeautifulSoup
 
# ─── CONFIG ──────────────────────────────────────────────────────────────────
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
 
LOOKBACK_HOURS = 26
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealflow")
 
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,no;q=0.8,sv;q=0.7",
}
 
 
# ─── DATA ────────────────────────────────────────────────────────────────────
 
@dataclass
class RawArticle:
    title: str
    url: str
    source: str
    country: str
    published: Optional[datetime] = None
    snippet: str = ""
 
@dataclass
class AnalyzedDeal:
    title: str
    url: str
    source: str
    country: str
    published: str
    # Kjerne
    signal: str = ""
    segment: str = ""
    city: str = ""
    area: str = ""
    size: str = ""
    estimated_value: str = ""
    # Oppsummering
    summary: str = ""
    catalyst: str = ""
    # Finansielt
    yield_pct: str = ""
    wault: str = ""
    rental_income: str = ""
    occupancy: str = ""
    price_per_sqm: str = ""
    # Leietakere
    main_tenant: str = ""
    # Parter
    seller: str = ""
    broker: str = ""
    buyer: str = ""
    legal: str = ""
    # Meta
    is_relevant: bool = False
 
 
# ─── KILDER ──────────────────────────────────────────────────────────────────
 
GOOGLE_NEWS_QUERIES = [
    {"query": "eiendom transaksjon salg", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "næringseiendom kontor oslo", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "eiendom kjøp investering norge", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "logistikk eiendom lager norge", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "fastighet transaktion försäljning", "hl": "sv", "gl": "SE", "country": "SE"},
    {"query": "kommersiell fastighet stockholm", "hl": "sv", "gl": "SE", "country": "SE"},
    {"query": "fastighetsaffär kontor logistik", "hl": "sv", "gl": "SE", "country": "SE"},
    {"query": "erhvervsejendom transaktion salg", "hl": "da", "gl": "DK", "country": "DK"},
    {"query": "ejendom kontor københavn investering", "hl": "da", "gl": "DK", "country": "DK"},
    {"query": "commercial real estate finland transaction", "hl": "en", "gl": "FI", "country": "FI"},
    {"query": "nordic real estate transaction deal", "hl": "en", "gl": "NO", "country": "Norden"},
    {"query": "estate nyheter eiendom", "hl": "no", "gl": "NO", "country": "NO"},
]
 
DIRECT_RSS = [
    {"name": "Estate Nyheter",     "country": "NO", "url": "https://www.estatenyheter.no/feed/"},
    {"name": "Finansavisen",       "country": "NO", "url": "https://finansavisen.no/eiendom/rss"},
    {"name": "Fastighetsvärlden",  "country": "SE", "url": "https://www.fastighetsvarlden.se/feed/"},
    {"name": "DI Fastigheter",     "country": "SE", "url": "https://www.di.se/rss/nyheter/fastigheter"},
    {"name": "Estate Media DK",    "country": "DK", "url": "https://estatemedia.dk/feed/"},
]
 
WEB_SOURCES = [
    {"name": "Newsec Norge",     "country": "NO",     "url": "https://www.newsec.no/nyheter/",            "selector": "a[href*='nyheter']"},
    {"name": "Newsec Sverige",   "country": "SE",     "url": "https://www.newsec.se/nyheter/",            "selector": "a[href*='nyheter']"},
    {"name": "Akershus Eiendom", "country": "NO",     "url": "https://www.akershus-eiendom.no/aktuelt/", "selector": "a[href*='aktuelt']"},
]
 
 
# ─── SKRAPING ────────────────────────────────────────────────────────────────
 
def fetch_google_news(q_config: dict, since: datetime) -> list[RawArticle]:
    articles = []
    q = quote_plus(q_config["query"])
    url = f"https://news.google.com/rss/search?q={q}&hl={q_config['hl']}&gl={q_config['gl']}&ceid={q_config['gl']}:{q_config['hl']}"
    try:
        feed = feedparser.parse(url, request_headers=BROWSER_HEADERS)
        for entry in feed.entries:
            pub = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if pub and pub < since:
                continue
 
            title = entry.get("title", "")
            source_name = "Google News"
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                title = parts[0]
                source_name = parts[1]
 
            snippet = ""
            if hasattr(entry, "summary"):
                snippet = BeautifulSoup(entry.summary, "html.parser").get_text(strip=True)[:800]
 
            articles.append(RawArticle(
                title=title, url=entry.get("link", ""),
                source=source_name, country=q_config["country"],
                published=pub, snippet=snippet,
            ))
        if articles:
            log.info(f"Google News '{q_config['query'][:30]}': {len(articles)} artikler")
    except Exception as e:
        log.warning(f"Google News feilet: {e}")
    return articles
 
 
def fetch_rss(source: dict, since: datetime) -> list[RawArticle]:
    articles = []
    try:
        feed = feedparser.parse(source["url"], request_headers=BROWSER_HEADERS)
        for entry in feed.entries:
            pub = None
            for attr in ("published_parsed", "updated_parsed"):
                parsed = getattr(entry, attr, None)
                if parsed:
                    pub = datetime(*parsed[:6], tzinfo=timezone.utc)
                    break
            if pub and pub < since:
                continue
            snippet = ""
            if hasattr(entry, "summary"):
                snippet = BeautifulSoup(entry.summary, "html.parser").get_text(strip=True)[:800]
            articles.append(RawArticle(
                title=entry.get("title", ""), url=entry.get("link", ""),
                source=source["name"], country=source["country"],
                published=pub, snippet=snippet,
            ))
        if articles:
            log.info(f"RSS  {source['name']}: {len(articles)} artikler")
    except Exception as e:
        log.warning(f"RSS  {source['name']}: {e}")
    return articles
 
 
def fetch_web(source: dict) -> list[RawArticle]:
    articles = []
    try:
        resp = requests.get(source["url"], timeout=15, headers=BROWSER_HEADERS)
        if resp.status_code != 200:
            return articles
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.select(source["selector"])[:10]:
            href = link.get("href", "")
            if not href.startswith("http"):
                href = urljoin(source["url"], href)
            title = link.get_text(strip=True)
            if len(title) < 15:
                continue
            articles.append(RawArticle(
                title=title, url=href,
                source=source["name"], country=source["country"],
                published=datetime.now(timezone.utc),
            ))
        if articles:
            log.info(f"WEB  {source['name']}: {len(articles)} artikler")
    except Exception as e:
        log.warning(f"WEB  {source['name']}: {e}")
    return articles
 
 
def fetch_article_content(url: str) -> str:
    try:
        if "news.google.com" in url:
            resp = requests.get(url, timeout=10, headers=BROWSER_HEADERS, allow_redirects=True)
            url = resp.url
        resp = requests.get(url, timeout=10, headers=BROWSER_HEADERS)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        for sel in ["article", ".article-body", ".entry-content", ".post-content",
                    ".article-content", ".story-body", "main", "[role='main']"]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text[:3500]
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)[:3500]
    except Exception:
        pass
    return ""
 
 
def fetch_all_articles() -> list[RawArticle]:
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_arts = []
    for gn in GOOGLE_NEWS_QUERIES:
        all_arts.extend(fetch_google_news(gn, since))
    for src in DIRECT_RSS:
        all_arts.extend(fetch_rss(src, since))
    for src in WEB_SOURCES:
        all_arts.extend(fetch_web(src))
 
    seen_urls, seen_titles, unique = set(), set(), []
    for a in all_arts:
        u = hashlib.md5(a.url.encode()).hexdigest()
        t = hashlib.md5(a.title.lower().encode()).hexdigest()
        if u not in seen_urls and t not in seen_titles:
            seen_urls.add(u); seen_titles.add(t); unique.append(a)
    log.info(f"Totalt {len(unique)} unike artikler")
    return unique
 
 
# ─── AI-ANALYSE ──────────────────────────────────────────────────────────────
 
ANALYSIS_PROMPT = """Du er en nordisk eiendomsanalytiker. Analyser denne artikkelen og hent ut strukturerte nøkkeltall.
 
ARTIKKEL:
Tittel: {title}
Kilde: {source} ({country})
Innhold:
{content}
 
INSTRUKSJONER:
- is_relevant=true KUN hvis artikkelen handler om kjøp/salg/utleie av næringseiendom, utviklingsprosjekter, reguleringsendringer, eller refinansiering i Norden.
- Skriv en SVÆRT KORT oppsummering: maks 2 setninger. Fokus på HVA som skjer og HVEM som er involvert. Ingen fyllord.
- Hent ut alle konkrete tall du kan finne i artikkelen. Hvis et tall ikke er nevnt, la feltet være "".
- catalyst: Den viktigste grunnen til at dealen skjer (f.eks. "Exit etter 7 års eierskap", "Refinansiering", "Leietaker flytter 2027").
 
Svar KUN med dette JSON-formatet:
{{
  "is_relevant": true,
  "signal": "<Salgsprosess|Ny listing|Prosess pågår|Off-market|Tidlig fase|Regulering|Refinansiering|Ikke relevant>",
  "segment": "<Kontor|Logistikk|Bolig|Handel|Hotell|Regulering|Blandet|Annet>",
  "city": "<By>",
  "area": "<Bydel/delområde som Skøyen, Solna, Nordhavn eller ''>",
  "size": "<Areal (kvm) eller antall enheter>",
  "estimated_value": "<Verdi med valuta, eller ''>",
  "summary": "<Maks 2 korte setninger>",
  "catalyst": "<Hvorfor selger selger nå, eller ''>",
  "yield_pct": "<F.eks. '4,5%' eller ''>",
  "wault": "<F.eks. '8,2 år' eller ''>",
  "rental_income": "<F.eks. '42 MNOK/år' eller ''>",
  "occupancy": "<F.eks. '89%' eller ''>",
  "price_per_sqm": "<F.eks. '52 000 NOK/kvm' eller ''>",
  "main_tenant": "<Hovedleietaker eller ''>",
  "seller": "<Selger eller ''>",
  "broker": "<Megler/rådgiver eller ''>",
  "buyer": "<Kjøper eller ''>",
  "legal": "<Juridisk rådgiver eller ''>"
}}
 
Hvis IKKE relevant: {{"is_relevant": false, "signal": "Ikke relevant", "segment": "", "city": "", "area": "", "size": "", "estimated_value": "", "summary": "", "catalyst": "", "yield_pct": "", "wault": "", "rental_income": "", "occupancy": "", "price_per_sqm": "", "main_tenant": "", "seller": "", "broker": "", "buyer": "", "legal": ""}}"""
 
 
def analyze_article(article: RawArticle) -> Optional[AnalyzedDeal]:
    if not ANTHROPIC_API_KEY:
        return None
 
    content = article.snippet
    if not content or len(content) < 100:
        full = fetch_article_content(article.url)
        if full:
            content = full
    if not content:
        content = article.title
 
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "content-type": "application/json",
                     "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": ANALYSIS_PROMPT.format(
                    title=article.title, source=article.source,
                    country=article.country, content=content[:3000],
                )}],
            }, timeout=60,
        )
        if resp.status_code != 200:
            log.error(f"Claude API HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        text = data["content"][0]["text"].strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        r = json.loads(text)
 
        return AnalyzedDeal(
            title=article.title, url=article.url,
            source=article.source, country=article.country,
            published=article.published.strftime("%Y-%m-%d %H:%M") if article.published else "",
            signal=r.get("signal", ""), segment=r.get("segment", ""),
            city=r.get("city", ""), area=r.get("area", ""),
            size=r.get("size", ""), estimated_value=r.get("estimated_value", ""),
            summary=r.get("summary", ""), catalyst=r.get("catalyst", ""),
            yield_pct=r.get("yield_pct", ""), wault=r.get("wault", ""),
            rental_income=r.get("rental_income", ""), occupancy=r.get("occupancy", ""),
            price_per_sqm=r.get("price_per_sqm", ""), main_tenant=r.get("main_tenant", ""),
            seller=r.get("seller", ""), broker=r.get("broker", ""),
            buyer=r.get("buyer", ""), legal=r.get("legal", ""),
            is_relevant=r.get("is_relevant", False),
        )
    except Exception as e:
        log.warning(f"AI feilet for '{article.title[:50]}': {e}")
        return None
 
 
def analyze_all(articles: list[RawArticle]) -> list[AnalyzedDeal]:
    deals = []
    for i, art in enumerate(articles):
        log.info(f"Analyserer {i+1}/{len(articles)}: {art.title[:60]}")
        d = analyze_article(art)
        if d and d.is_relevant and d.signal != "Ikke relevant":
            deals.append(d)
    log.info(f"{len(deals)} relevante deals av {len(articles)} artikler")
    return deals
 
 
# ─── E-POST ──────────────────────────────────────────────────────────────────
 
FLAGS = {"NO": "🇳🇴", "SE": "🇸🇪", "DK": "🇩🇰", "FI": "🇫🇮", "Norden": "🌐"}
SIG_COLORS = {
    "Salgsprosess":    ("#e8f5e9", "#2e7d32"),
    "Ny listing":      ("#e3f2fd", "#1565c0"),
    "Prosess pågår":   ("#fff8e1", "#f57f17"),
    "Off-market":      ("#f3e5f5", "#7b1fa2"),
    "Tidlig fase":     ("#fff3e0", "#e65100"),
    "Regulering":      ("#f5f5f5", "#616161"),
    "Refinansiering":  ("#e0f7fa", "#00838f"),
}
 
 
def render_deal(d: AnalyzedDeal) -> str:
    bg, fg = SIG_COLORS.get(d.signal, ("#f5f5f5", "#666"))
    flag = FLAGS.get(d.country, "")
 
    # Location string
    location = d.city
    if d.area:
        location = f"{d.area}, {d.city}" if d.city else d.area
 
    # Header meta
    meta_parts = []
    if location: meta_parts.append(location)
    if d.segment: meta_parts.append(d.segment)
    if d.size: meta_parts.append(d.size)
    header_meta = " · ".join(meta_parts)
 
    # Key number (value) – prominent
    value_html = ""
    if d.estimated_value:
        value_html = f"""<div style="text-align:right;">
          <div style="font-size:9px;color:#999;font-family:'Courier New',monospace;text-transform:uppercase;letter-spacing:1px;">Verdi</div>
          <div style="font-size:16px;font-weight:700;color:#1a1a1a;font-family:-apple-system,sans-serif;">{d.estimated_value}</div>
        </div>"""
 
    # Financial metrics grid (only show what exists)
    fin_items = []
    if d.yield_pct: fin_items.append(("Yield", d.yield_pct))
    if d.wault: fin_items.append(("WAULT", d.wault))
    if d.occupancy: fin_items.append(("Utleie", d.occupancy))
    if d.price_per_sqm: fin_items.append(("Pris/kvm", d.price_per_sqm))
    if d.rental_income: fin_items.append(("Leie", d.rental_income))
 
    fin_html = ""
    if fin_items:
        cells = "".join(f"""<td style="padding:8px 10px;border-right:1px solid #eee;">
            <div style="font-size:8px;color:#999;font-family:'Courier New',monospace;text-transform:uppercase;letter-spacing:1px;">{l}</div>
            <div style="font-size:13px;font-weight:600;color:#1a1a1a;margin-top:2px;">{v}</div>
          </td>""" for l, v in fin_items)
        fin_html = f"""<table cellpadding="0" cellspacing="0" style="width:100%;background:#fafafa;border:1px solid #f0f0f0;border-radius:3px;margin-bottom:10px;">
          <tr>{cells}</tr>
        </table>"""
 
    # Parties – compact inline
    parties_items = []
    if d.seller: parties_items.append(("Selger", d.seller))
    if d.broker: parties_items.append(("Megler", d.broker))
    if d.buyer: parties_items.append(("Kjøper", d.buyer))
    if d.legal: parties_items.append(("Legal", d.legal))
    if d.main_tenant: parties_items.append(("Leietaker", d.main_tenant))
 
    parties_html = ""
    if parties_items:
        rows = "".join(f"""<tr>
          <td style="padding:3px 0;color:#888;font-family:'Courier New',monospace;font-size:9px;text-transform:uppercase;letter-spacing:.5px;width:70px;vertical-align:top;">{l}</td>
          <td style="padding:3px 0;color:#333;font-size:12px;">{v}</td>
        </tr>""" for l, v in parties_items)
        parties_html = f"""<table cellpadding="0" cellspacing="0" style="margin-bottom:8px;">{rows}</table>"""
 
    # Catalyst (only if present)
    catalyst_html = ""
    if d.catalyst:
        catalyst_html = f"""<div style="font-size:11px;color:#666;font-style:italic;margin-bottom:8px;padding:6px 10px;background:#fffbea;border-left:2px solid #f57f17;">
          ⚡ {d.catalyst}
        </div>"""
 
    return f"""<tr><td style="padding:18px 22px;border-bottom:1px solid #eee;">
      <table cellpadding="0" cellspacing="0" width="100%">
        <tr>
          <td style="vertical-align:top;">
            <div style="margin-bottom:4px;">
              <span style="font-size:13px;">{flag}</span>
              <span style="font-size:9px;font-family:'Courier New',monospace;padding:2px 7px;border-radius:3px;background:{bg};color:{fg};font-weight:600;letter-spacing:.3px;">{d.signal}</span>
              <span style="font-size:9px;color:#bbb;font-family:'Courier New',monospace;margin-left:6px;">{d.published}</span>
            </div>
            <h3 style="margin:2px 0 4px;font-size:14px;font-weight:600;color:#1a1a1a;line-height:1.35;">
              <a href="{d.url}" style="color:#1a1a1a;text-decoration:none;">{d.title}</a>
            </h3>
            <div style="font-size:11px;color:#888;font-family:'Courier New',monospace;">{header_meta}</div>
          </td>
          <td style="vertical-align:top;width:90px;text-align:right;padding-left:12px;">{value_html}</td>
        </tr>
      </table>
      <p style="font-size:12px;color:#444;line-height:1.55;margin:10px 0 10px;">{d.summary}</p>
      {catalyst_html}
      {fin_html}
      {parties_html}
      <a href="{d.url}" style="font-size:10px;color:#1565c0;text-decoration:none;font-family:'Courier New',monospace;">↗ {d.source}</a>
    </td></tr>"""
 
 
def build_email(deals: list[AnalyzedDeal]) -> str:
    today = datetime.now().strftime("%d. %B %Y")
    countries = sorted(set(FLAGS.get(d.country, d.country) for d in deals)) if deals else []
 
    if deals:
        rows = "".join(render_deal(d) for d in deals)
    else:
        rows = """<tr><td style="padding:40px 24px;text-align:center;color:#999;font-family:'Courier New',monospace;font-size:12px;">
          Ingen relevante transaksjoner funnet siste 24 timer.
        </td></tr>"""
 
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f7f7f7;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;background:#fff;">
  <tr><td style="padding:26px 22px 14px;border-bottom:2px solid #1a1a1a;">
    <h1 style="margin:0;font-size:17px;font-weight:700;color:#1a1a1a;letter-spacing:-.3px;">Nordic Deal Flow</h1>
    <p style="margin:3px 0 0;font-size:10px;color:#aaa;font-family:'Courier New',monospace;">
      {today} · Siste 24 timer · {len(deals)} {"transaksjon" if len(deals) == 1 else "transaksjoner"}
      {" · " + ' '.join(countries) if countries else ""}
    </p>
  </td></tr>
  {rows}
  <tr><td style="padding:16px 22px;border-top:1px solid #eee;background:#fafafa;">
    <p style="margin:0;font-size:9px;color:#bbb;font-family:'Courier New',monospace;">
      Nordic Deal Flow Agent · Claude API
    </p>
  </td></tr>
</table>
</body></html>"""
 
 
def send_email(deals: list[AnalyzedDeal]):
    html = build_email(deals)
    filename = f"deal_flow_{datetime.now().strftime('%Y%m%d')}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Lagret {filename}")
 
    if not RESEND_API_KEY or not EMAIL_TO:
        return
 
    try:
        today = datetime.now().strftime("%d.%m.%Y")
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": EMAIL_FROM, "to": [EMAIL_TO],
                "subject": f"Nordic Deal Flow – {len(deals)} deals – {today}",
                "html": html,
            }, timeout=15,
        )
        resp.raise_for_status()
        log.info(f"E-post sendt til {EMAIL_TO}")
    except Exception as e:
        log.error(f"E-post feilet: {e}")
 
 
def run():
    log.info("=" * 50)
    log.info("Nordic Deal Flow Agent v3")
    log.info("=" * 50)
    articles = fetch_all_articles()
    if not articles:
        send_email([])
        return
    deals = analyze_all(articles)
    send_email(deals)
    log.info(f"Ferdig – {len(deals)} deals sendt")
 
 
if __name__ == "__main__":
    run()
