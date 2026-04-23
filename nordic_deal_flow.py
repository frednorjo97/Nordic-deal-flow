"""
Nordic Deal Flow Agent
======================
Skraper nordiske eiendomskilder, analyserer med Claude API,
og sender daglig e-post kl. 09:00 norsk tid.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

# ─── CONFIG ──────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")  # Resend test-avsender

MIN_RELEVANCE = 60
LOOKBACK_HOURS = 24

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealflow")


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


# ─── KILDER ──────────────────────────────────────────────────────────────────

RSS_SOURCES = [
    {"name": "Estate Nyheter",     "country": "NO", "url": "https://www.estatenyheter.no/feed/"},
    {"name": "Finansavisen",       "country": "NO", "url": "https://finansavisen.no/eiendom/rss"},
    {"name": "Fastighetsvärlden",  "country": "SE", "url": "https://www.fastighetsvarlden.se/feed/"},
    {"name": "DI Fastigheter",    "country": "SE", "url": "https://www.di.se/rss/nyheter/fastigheter"},
    {"name": "Estate Media DK",   "country": "DK", "url": "https://estatemedia.dk/feed/"},
]

WEB_SOURCES = [
    {"name": "Newsec Norge",   "country": "NO", "url": "https://www.newsec.no/nyheter/",   "selector": "article a"},
    {"name": "Newsec Sverige", "country": "SE", "url": "https://www.newsec.se/nyheter/",   "selector": "article a"},
    {"name": "Newsec Finland", "country": "FI", "url": "https://www.newsec.fi/en/news/",   "selector": "article a"},
    {"name": "EDC Erhverv",    "country": "DK", "url": "https://www.edcerhverv.dk/nyheder", "selector": ".news-item a"},
]

HEADERS = {"User-Agent": "NordicDealFlowBot/1.0 (+https://github.com)"}


# ─── STEG 1: SKRAPING ───────────────────────────────────────────────────────

def fetch_rss(source: dict, since: datetime) -> list[RawArticle]:
    articles = []
    try:
        feed = feedparser.parse(source["url"])
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
        log.info(f"RSS  {source['name']}: {len(articles)} artikler")
    except Exception as e:
        log.warning(f"RSS  {source['name']} feilet: {e}")
    return articles


def fetch_web(source: dict) -> list[RawArticle]:
    articles = []
    try:
        resp = requests.get(source["url"], timeout=15, headers=HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for link in soup.select(source["selector"])[:15]:
            href = link.get("href", "")
            if not href.startswith("http"):
                href = urljoin(source["url"], href)
            title = link.get_text(strip=True)
            if len(title) < 10:
                continue
            articles.append(RawArticle(
                title=title, url=href,
                source=source["name"], country=source["country"],
                published=datetime.now(timezone.utc),
            ))
        log.info(f"WEB  {source['name']}: {len(articles)} artikler")
    except Exception as e:
        log.warning(f"WEB  {source['name']} feilet: {e}")
    return articles


def fetch_article_content(url: str) -> str:
    """Henter fulltekst fra en artikkel-URL for bedre AI-analyse."""
    try:
        resp = requests.get(url, timeout=10, headers=HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Fjern script, style, nav, footer
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Prøv vanlige artikkel-containere
        for selector in ["article", ".article-body", ".entry-content", ".post-content", "main"]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text[:3000]

        # Fallback: all tekst fra body
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)[:3000]
    except Exception:
        pass
    return ""


def fetch_all_articles() -> list[RawArticle]:
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_arts = []

    for src in RSS_SOURCES:
        all_arts.extend(fetch_rss(src, since))
    for src in WEB_SOURCES:
        all_arts.extend(fetch_web(src))

    # Dedupliser på URL
    seen = set()
    unique = []
    for a in all_arts:
        h = hashlib.md5(a.url.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(a)

    log.info(f"Totalt {len(unique)} unike artikler")
    return unique


# ─── STEG 2: AI-ANALYSE MED CLAUDE ──────────────────────────────────────────

ANALYSIS_PROMPT = """Du er en erfaren nordisk eiendomsanalytiker. Analyser denne artikkelen og vurder
om den beskriver en reell eiendomstransaksjon, salgsprosess, eller relevant deal-mulighet.

ARTIKKEL:
Tittel: {title}
Kilde: {source} ({country})
URL: {url}
Innhold:
{content}

INSTRUKSJONER:
1. Er dette en reell transaksjon eller deal-relevant nyhet? Sett is_relevant til true/false.
2. Skriv en utfyllende beskrivelse på 4-6 setninger som oppsummerer det viktigste fra artikkelen:
   hvem selger, hva selges, estimert størrelse/verdi, hvor i prosessen man er, og hva som gjør
   dealen interessant. Bruk konkrete tall og navn fra artikkelen.
3. Identifiser alle involverte parter du kan finne i artikkelen.

Svar KUN med dette JSON-formatet, ingen annen tekst:
{{
  "is_relevant": true/false,
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
}}"""


def analyze_article(article: RawArticle) -> Optional[AnalyzedDeal]:
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY mangler – hopper over AI-analyse")
        return None

    # Hent fulltekst for bedre analyse
    content = article.snippet
    if not content or len(content) < 100:
        content = fetch_article_content(article.url)
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
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": ANALYSIS_PROMPT.format(
                    title=article.title, source=article.source,
                    country=article.country, url=article.url, content=content[:3000],
                )}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        r = json.loads(text)

        return AnalyzedDeal(
            title=article.title, url=article.url,
            source=article.source, country=article.country,
            published=article.published.strftime("%Y-%m-%d %H:%M") if article.published else "Ukjent",
            signal=r.get("signal", ""), segment=r.get("segment", ""),
            city=r.get("city", ""), size=r.get("size", ""),
            estimated_value=r.get("estimated_value", ""),
            description=r.get("description", ""),
            seller=r.get("seller", "Ukjent"), broker=r.get("broker", "Ukjent"),
            buyer=r.get("buyer", "Ukjent"), legal=r.get("legal", "Ukjent"),
            is_relevant=r.get("is_relevant", False),
        )
    except Exception as e:
        log.warning(f"AI-analyse feilet for '{article.title[:50]}': {e}")
        return None


def analyze_all(articles: list[RawArticle]) -> list[AnalyzedDeal]:
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
    flags = {"NO": "🇳🇴", "SE": "🇸🇪", "DK": "🇩🇰", "FI": "🇫🇮"}
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
        for label, val in [("Selger", d.seller), ("Megler", d.broker), ("Kjøper", d.buyer), ("Legal", d.legal)]:
            if val and val != "Ukjent":
                parties_html += f"""
                <tr>
                  <td style="padding:4px 8px;color:#999;font-family:'Courier New',monospace;font-size:9px;
                    text-transform:uppercase;letter-spacing:.5px;vertical-align:top;width:50px;">{label}</td>
                  <td style="padding:4px 8px;color:#333;font-size:12px;">{val}</td>
                </tr>"""

        rows += f"""
        <tr><td style="padding:20px 24px;border-bottom:1px solid #f0f0f0;">
          <div>
            <span style="font-size:13px;">{flag}</span>
            <span style="font-size:10px;font-family:'Courier New',monospace;padding:2px 8px;
              border-radius:3px;background:{bg};color:{fg};font-weight:600;">{d.signal}</span>
            <span style="font-size:10px;color:#bbb;font-family:'Courier New',monospace;margin-left:8px;">{d.published}</span>
          </div>
          <h3 style="margin:8px 0 4px;font-size:15px;font-weight:600;color:#1a1a1a;">
            <a href="{d.url}" style="color:#1a1a1a;text-decoration:none;">{d.title}</a>
          </h3>
          <div style="font-size:11px;color:#888;font-family:'Courier New',monospace;margin-bottom:10px;">
            {d.city} &nbsp;|&nbsp; {d.segment}
            {"&nbsp;|&nbsp; " + d.size if d.size and d.size != "Ukjent" else ""}
            {"&nbsp;|&nbsp; <b style='color:#1a1a1a;'>" + d.estimated_value + "</b>" if d.estimated_value and d.estimated_value != "Ukjent" else ""}
          </div>
          <p style="font-size:13px;color:#444;line-height:1.7;margin:0 0 12px;">{d.description}</p>
          {f'''<table style="font-size:11px;border-collapse:collapse;width:100%;
            background:#fafafa;border:1px solid #f0f0f0;border-radius:4px;margin-bottom:10px;">
            {parties_html}</table>''' if parties_html else ""}
          <a href="{d.url}" style="font-size:11px;color:#1565c0;text-decoration:none;
            font-family:'Courier New',monospace;">↗ Les originalartikkel ({d.source})</a>
        </td></tr>"""

    countries = sorted(set(flags.get(d.country, d.country) for d in deals))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f7f7f7;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;background:#fff;">
  <tr><td style="padding:28px 24px;border-bottom:2px solid #1a1a1a;">
    <h1 style="margin:0;font-size:18px;font-weight:700;color:#1a1a1a;">Nordic Deal Flow</h1>
    <p style="margin:4px 0 0;font-size:11px;color:#aaa;font-family:'Courier New',monospace;">
      {today} · Siste 24 timer · {len(deals)} transaksjoner
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
        <span style="font-size:14px;color:#1a1a1a;">{' '.join(countries)}</span>
      </td>
    </tr></table>
  </td></tr>
  {rows}
  <tr><td style="padding:20px 24px;border-top:1px solid #eee;background:#fafafa;">
    <p style="margin:0;font-size:10px;color:#bbb;font-family:'Courier New',monospace;line-height:1.6;">
      Nordic Deal Flow Agent · Analyse: Claude API<br>
      Kilder: {', '.join(s['name'] for s in RSS_SOURCES + WEB_SOURCES)}
    </p>
  </td></tr>
</table>
</body></html>"""


# ─── STEG 4: SEND ───────────────────────────────────────────────────────────

def send_email(deals: list[AnalyzedDeal]):
    html = build_email(deals)
    today = datetime.now().strftime("%d.%m.%Y")
    filename = f"deal_flow_{datetime.now().strftime('%Y%m%d')}.html"

    # Lagre alltid lokalt som backup
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Lagret {filename}")

    if not RESEND_API_KEY or not EMAIL_TO:
        log.info("RESEND_API_KEY eller EMAIL_TO mangler – e-post ikke sendt")
        log.info(f"Åpne {filename} i nettleseren for å se resultatet")
        return

    try:
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
    log.info("Nordic Deal Flow Agent – starter")
    log.info("=" * 50)

    articles = fetch_all_articles()
    if not articles:
        log.warning("Ingen artikler funnet")
        return

    deals = analyze_all(articles)
    send_email(deals)
    log.info("Ferdig")


if __name__ == "__main__":
    run()
