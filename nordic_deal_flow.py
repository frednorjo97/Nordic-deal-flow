"""
Nordic Deal Flow Agent v2
=========================
Skraper nordiske eiendomskilder, analyserer med Claude API,
og sender daglig e-post kl. 09:00 norsk tid.
 
Forbedringer i v2:
- Bedre User-Agent headers som ikke blokkeres
- Google News RSS som hovedkilde (aggregerer fra alle kilder)
- Direkte RSS-feeds som supplement
- Lavere terskel og mer robust feilhåndtering
"""
 
import os
import json
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
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
 
MIN_RELEVANCE = 40          # Lav terskel – la Claude bestemme hva som er relevant
LOOKBACK_HOURS = 26         # Litt over 24t for å fange alt
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealflow")
 
# Browser-like headers for å unngå blokkering
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,no;q=0.8,sv;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
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
    signal: str = ""
    segment: str = ""
    city: str = ""
    size: str = ""
    estimated_value: str = ""
    description: str = ""
    seller: str = ""
    broker: str = ""
    buyer: str = ""
    legal: str = ""
    is_relevant: bool = False
 
 
# ─── STEG 1: KILDER ─────────────────────────────────────────────────────────
 
# Google News RSS-søk (mest pålitelig fra servere)
GOOGLE_NEWS_QUERIES = [
    # Norske søk
    {"query": "eiendom transaksjon salg", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "næringseiendom kontor oslo", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "eiendom kjøp investering norge", "hl": "no", "gl": "NO", "country": "NO"},
    {"query": "logistikk eiendom lager norge", "hl": "no", "gl": "NO", "country": "NO"},
    # Svenske søk
    {"query": "fastighet transaktion försäljning", "hl": "sv", "gl": "SE", "country": "SE"},
    {"query": "kommersiell fastighet stockholm", "hl": "sv", "gl": "SE", "country": "SE"},
    {"query": "fastighetsaffär kontor logistik", "hl": "sv", "gl": "SE", "country": "SE"},
    # Danske søk
    {"query": "erhvervsejendom transaktion salg", "hl": "da", "gl": "DK", "country": "DK"},
    {"query": "ejendom kontor københavn investering", "hl": "da", "gl": "DK", "country": "DK"},
    # Finske søk (engelsk)
    {"query": "commercial real estate finland transaction", "hl": "en", "gl": "FI", "country": "FI"},
    # Brede nordiske søk
    {"query": "nordic real estate transaction deal", "hl": "en", "gl": "NO", "country": "Norden"},
    {"query": "estate nyheter eiendom", "hl": "no", "gl": "NO", "country": "NO"},
]
 
# Direkte RSS-feeds (fungerer fra noen servere)
DIRECT_RSS = [
    {"name": "Estate Nyheter",     "country": "NO", "url": "https://www.estatenyheter.no/feed/"},
    {"name": "Finansavisen",       "country": "NO", "url": "https://finansavisen.no/eiendom/rss"},
    {"name": "Fastighetsvärlden",  "country": "SE", "url": "https://www.fastighetsvarlden.se/feed/"},
    {"name": "DI Fastigheter",    "country": "SE", "url": "https://www.di.se/rss/nyheter/fastigheter"},
    {"name": "Estate Media DK",   "country": "DK", "url": "https://estatemedia.dk/feed/"},
]
 
# Websider å skrape
WEB_SOURCES = [
    {"name": "Newsec Norge",   "country": "NO", "url": "https://www.newsec.no/nyheter/",   "selector": "a[href*='nyheter']"},
    {"name": "Newsec Sverige", "country": "SE", "url": "https://www.newsec.se/nyheter/",   "selector": "a[href*='nyheter']"},
    {"name": "Newsec Finland", "country": "FI", "url": "https://www.newsec.fi/en/news/",   "selector": "a[href*='news']"},
    {"name": "Akershus Eiendom", "country": "NO", "url": "https://www.akershus-eiendom.no/aktuelt/", "selector": "a[href*='aktuelt']"},
    {"name": "JLL Nordics", "country": "Norden", "url": "https://www.jll.no/no/trender-og-innsikt", "selector": "a[href*='trender']"},
]
 
 
def fetch_google_news(query_config: dict, since: datetime) -> list[RawArticle]:
    """Henter artikler fra Google News RSS."""
    articles = []
    q = quote_plus(query_config["query"])
    hl = query_config["hl"]
    gl = query_config["gl"]
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{hl}"
 
    try:
        feed = feedparser.parse(url, request_headers=BROWSER_HEADERS)
        for entry in feed.entries:
            pub = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
 
            if pub and pub < since:
                continue
 
            # Google News lenker via redirect – hent original URL
            link = entry.get("link", "")
 
            # Hent kildenavn fra tittel (Google News format: "Tittel - Kilde")
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
                title=title,
                url=link,
                source=source_name,
                country=query_config["country"],
                published=pub,
                snippet=snippet,
            ))
        if articles:
            log.info(f"Google News '{query_config['query'][:30]}': {len(articles)} artikler")
    except Exception as e:
        log.warning(f"Google News '{query_config['query'][:30]}' feilet: {e}")
    return articles
 
 
def fetch_rss(source: dict, since: datetime) -> list[RawArticle]:
    """Henter artikler fra en direkte RSS-feed."""
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
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                source=source["name"],
                country=source["country"],
                published=pub,
                snippet=snippet,
            ))
        if articles:
            log.info(f"RSS  {source['name']}: {len(articles)} artikler")
    except Exception as e:
        log.warning(f"RSS  {source['name']}: {e}")
    return articles
 
 
def fetch_web(source: dict) -> list[RawArticle]:
    """Skraper artikler fra en webside."""
    articles = []
    try:
        resp = requests.get(source["url"], timeout=15, headers=BROWSER_HEADERS)
        if resp.status_code != 200:
            log.warning(f"WEB  {source['name']}: HTTP {resp.status_code}")
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
    """Henter fulltekst fra en artikkel for bedre AI-analyse."""
    try:
        # Google News redirect-URLer
        if "news.google.com" in url:
            resp = requests.get(url, timeout=10, headers=BROWSER_HEADERS, allow_redirects=True)
            url = resp.url  # Følg redirect til original artikkel
 
        resp = requests.get(url, timeout=10, headers=BROWSER_HEADERS)
        if resp.status_code != 200:
            return ""
 
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
 
        for selector in ["article", ".article-body", ".entry-content", ".post-content",
                         ".article-content", ".story-body", "main", "[role='main']"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text[:3000]
 
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)[:3000]
    except Exception:
        pass
    return ""
 
 
def fetch_all_articles() -> list[RawArticle]:
    """Henter artikler fra alle kilder."""
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_arts = []
 
    # 1. Google News (hovedkilde – fungerer alltid fra servere)
    for gn in GOOGLE_NEWS_QUERIES:
        all_arts.extend(fetch_google_news(gn, since))
 
    # 2. Direkte RSS-feeds
    for src in DIRECT_RSS:
        all_arts.extend(fetch_rss(src, since))
 
    # 3. Webscraping
    for src in WEB_SOURCES:
        all_arts.extend(fetch_web(src))
 
    # Dedupliser på URL og tittel
    seen_urls = set()
    seen_titles = set()
    unique = []
    for a in all_arts:
        url_hash = hashlib.md5(a.url.encode()).hexdigest()
        title_hash = hashlib.md5(a.title.lower().encode()).hexdigest()
        if url_hash not in seen_urls and title_hash not in seen_titles:
            seen_urls.add(url_hash)
            seen_titles.add(title_hash)
            unique.append(a)
 
    log.info(f"Totalt {len(unique)} unike artikler fra alle kilder")
    return unique
 
 
# ─── STEG 2: AI-ANALYSE ─────────────────────────────────────────────────────
 
ANALYSIS_PROMPT = """Du er en erfaren nordisk eiendomsanalytiker. Analyser denne artikkelen og vurder
om den beskriver en reell eiendomstransaksjon, salgsprosess, eller relevant deal-mulighet.
 
ARTIKKEL:
Tittel: {title}
Kilde: {source} ({country})
URL: {url}
Innhold:
{content}
 
INSTRUKSJONER:
1. Er dette en reell transaksjon, salgsprosess, eller deal-relevant nyhet? 
   Sett is_relevant til true kun hvis det handler om kjøp/salg/utleie av næringseiendom,
   utviklingsprosjekter, reguleringsendringer, eller refinansiering av eiendom i Norden.
2. Skriv en utfyllende beskrivelse på 4-6 setninger som oppsummerer det viktigste:
   hvem selger/kjøper, hva som selges, estimert størrelse/verdi, status i prosessen,
   og hva som gjør dealen interessant. Bruk konkrete tall og navn fra artikkelen.
3. Identifiser alle involverte parter nevnt i artikkelen.
 
Svar KUN med dette JSON-formatet, ingen annen tekst:
{{
  "is_relevant": true,
  "signal": "<Salgsprosess|Ny listing|Prosess pågår|Off-market|Tidlig fase|Regulering|Refinansiering|Ikke relevant>",
  "segment": "<Kontor|Logistikk|Bolig|Handel|Hotell|Regulering|Blandet|Annet>",
  "city": "<By eller region>",
  "size": "<Areal, antall enheter, eller Ukjent>",
  "estimated_value": "<Verdi med valuta, eller Ukjent>",
  "description": "<4-6 setninger med analyse basert på artikkelens innhold>",
  "seller": "<Selger eller Ukjent>",
  "broker": "<Megler/rådgiver eller Ukjent>",
  "buyer": "<Kjøper eller Ikke offentliggjort>",
  "legal": "<Juridisk rådgiver eller Ukjent>"
}}
 
Hvis artikkelen IKKE handler om eiendomstransaksjoner, svar med:
{{"is_relevant": false, "signal": "Ikke relevant", "segment": "Annet", "city": "", "size": "", "estimated_value": "", "description": "", "seller": "", "broker": "", "buyer": "", "legal": ""}}"""
 
 
def analyze_article(article: RawArticle) -> Optional[AnalyzedDeal]:
    """Sender en artikkel til Claude API for analyse."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY mangler")
        return None
 
    # Hent fulltekst for bedre analyse
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
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": ANALYSIS_PROMPT.format(
                    title=article.title, source=article.source,
                    country=article.country, url=article.url, content=content[:3000],
                )}],
            },
            timeout=60,
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
            city=r.get("city", ""), size=r.get("size", ""),
            estimated_value=r.get("estimated_value", ""),
            description=r.get("description", ""),
            seller=r.get("seller", ""), broker=r.get("broker", ""),
            buyer=r.get("buyer", ""), legal=r.get("legal", ""),
            is_relevant=r.get("is_relevant", False),
        )
    except Exception as e:
        log.warning(f"AI-analyse feilet for '{article.title[:50]}': {e}")
        return None
 
 
def analyze_all(articles: list[RawArticle]) -> list[AnalyzedDeal]:
    """Analyserer alle artikler og returnerer relevante deals."""
    deals = []
    for i, art in enumerate(articles):
        log.info(f"Analyserer {i+1}/{len(articles)}: {art.title[:60]}...")
        deal = analyze_article(art)
        if deal and deal.is_relevant and deal.signal != "Ikke relevant":
            deals.append(deal)
    log.info(f"{len(deals)} relevante deals av {len(articles)} artikler")
    return deals
 
 
# ─── STEG 3: HTML E-POST ────────────────────────────────────────────────────
 
def build_email(deals: list[AnalyzedDeal]) -> str:
    today = datetime.now().strftime("%d. %B %Y")
    flags = {"NO": "🇳🇴", "SE": "🇸🇪", "DK": "🇩🇰", "FI": "🇫🇮", "Norden": "🌐"}
    sig_colors = {
        "Salgsprosess":    ("#e8f5e9", "#2e7d32"),
        "Ny listing":      ("#e3f2fd", "#1565c0"),
        "Prosess pågår":   ("#fff8e1", "#f57f17"),
        "Off-market":      ("#f3e5f5", "#7b1fa2"),
        "Tidlig fase":     ("#fff3e0", "#e65100"),
        "Regulering":      ("#f5f5f5", "#616161"),
        "Refinansiering":  ("#e0f7fa", "#00838f"),
    }
 
    rows = ""
    for d in deals:
        bg, fg = sig_colors.get(d.signal, ("#f5f5f5", "#666"))
        flag = flags.get(d.country, "")
 
        parties_html = ""
        for label, val in [("Selger", d.seller), ("Megler", d.broker),
                           ("Kjøper", d.buyer), ("Legal", d.legal)]:
            if val and val not in ("Ukjent", "", "N/A"):
                parties_html += f"""
                <tr>
                  <td style="padding:4px 8px;color:#999;font-family:'Courier New',monospace;font-size:9px;
                    text-transform:uppercase;letter-spacing:.5px;vertical-align:top;width:50px;">{label}</td>
                  <td style="padding:4px 8px;color:#333;font-size:12px;">{val}</td>
                </tr>"""
 
        metrics = f"{d.city}" if d.city else ""
        if d.segment:
            metrics += f" &nbsp;|&nbsp; {d.segment}" if metrics else d.segment
        if d.size and d.size != "Ukjent":
            metrics += f" &nbsp;|&nbsp; {d.size}"
        if d.estimated_value and d.estimated_value != "Ukjent":
            metrics += f" &nbsp;|&nbsp; <b style='color:#1a1a1a;'>{d.estimated_value}</b>"
 
        rows += f"""
        <tr><td style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
          <div>
            <span style="font-size:13px;">{flag}</span>
            <span style="font-size:10px;font-family:'Courier New',monospace;padding:2px 8px;
              border-radius:3px;background:{bg};color:{fg};font-weight:600;">{d.signal}</span>
            <span style="font-size:10px;color:#bbb;font-family:'Courier New',monospace;
              margin-left:8px;">{d.published}</span>
          </div>
          <h3 style="margin:8px 0 4px;font-size:15px;font-weight:600;color:#1a1a1a;">
            <a href="{d.url}" style="color:#1a1a1a;text-decoration:none;">{d.title}</a>
          </h3>
          <div style="font-size:11px;color:#888;font-family:'Courier New',monospace;
            margin-bottom:10px;">{metrics}</div>
          <p style="font-size:13px;color:#444;line-height:1.7;margin:0 0 12px;">{d.description}</p>
          {f'<table style="font-size:11px;border-collapse:collapse;width:100%;background:#fafafa;border:1px solid #f0f0f0;border-radius:4px;margin-bottom:10px;">{parties_html}</table>' if parties_html else ''}
          <a href="{d.url}" style="font-size:11px;color:#1565c0;text-decoration:none;
            font-family:'Courier New',monospace;">↗ {d.source}</a>
        </td></tr>"""
 
    if not deals:
        rows = """<tr><td style="padding:40px 24px;text-align:center;color:#999;
          font-family:'Courier New',monospace;font-size:12px;">
          Ingen relevante transaksjoner funnet siste 24 timer.
        </td></tr>"""
 
    countries = sorted(set(flags.get(d.country, d.country) for d in deals)) if deals else []
 
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f7f7f7;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;background:#fff;">
  <tr><td style="padding:28px 24px;border-bottom:2px solid #1a1a1a;">
    <h1 style="margin:0;font-size:18px;font-weight:700;color:#1a1a1a;">Nordic Deal Flow</h1>
    <p style="margin:4px 0 0;font-size:11px;color:#aaa;font-family:'Courier New',monospace;">
      {today} · Siste 24 timer · {len(deals)} {"transaksjon" if len(deals) == 1 else "transaksjoner"}
    </p>
  </td></tr>
  <tr><td style="padding:14px 24px;background:#fafafa;border-bottom:1px solid #eee;">
    <table cellpadding="0" cellspacing="0" width="100%"><tr>
      <td style="font-family:'Courier New',monospace;">
        <span style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:1px;">Deals</span><br>
        <span style="font-size:22px;font-weight:700;color:#1a1a1a;">{len(deals)}</span>
      </td>
      <td style="font-family:'Courier New',monospace;">
        <span style="font-size:9px;color:#999;text-transform:uppercase;letter-spacing:1px;">Land</span><br>
        <span style="font-size:14px;color:#1a1a1a;">{' '.join(countries) if countries else '–'}</span>
      </td>
    </tr></table>
  </td></tr>
  {rows}
  <tr><td style="padding:20px 24px;border-top:1px solid #eee;background:#fafafa;">
    <p style="margin:0;font-size:10px;color:#bbb;font-family:'Courier New',monospace;line-height:1.6;">
      Nordic Deal Flow Agent · Analyse: Claude API
    </p>
  </td></tr>
</table>
</body></html>"""
 
 
# ─── STEG 4: SEND ───────────────────────────────────────────────────────────
 
def send_email(deals: list[AnalyzedDeal]):
    html = build_email(deals)
    today_str = datetime.now().strftime("%Y%m%d")
    filename = f"deal_flow_{today_str}.html"
 
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Lagret {filename}")
 
    if not RESEND_API_KEY or not EMAIL_TO:
        log.info("Mangler RESEND_API_KEY eller EMAIL_TO – e-post ikke sendt")
        return
 
    try:
        today = datetime.now().strftime("%d.%m.%Y")
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM,
                "to": [EMAIL_TO],
                "subject": f"Nordic Deal Flow – {len(deals)} deals – {today}",
                "html": html,
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"E-post sendt til {EMAIL_TO}")
    except Exception as e:
        log.error(f"E-post feilet: {e}")
 
 
# ─── KJØR ────────────────────────────────────────────────────────────────────
 
def run():
    log.info("=" * 50)
    log.info("Nordic Deal Flow Agent v2")
    log.info("=" * 50)
 
    articles = fetch_all_articles()
    if not articles:
        log.warning("Ingen artikler funnet – sender tom e-post")
        send_email([])
        return
 
    deals = analyze_all(articles)
    send_email(deals)
    log.info(f"Ferdig – {len(deals)} deals sendt")
 
 
if __name__ == "__main__":
    run()
