# Nordic Deal Flow Agent

AI-agent som skraper nordiske eiendomskilder daglig, analyserer med Claude, og sender deg en formatert e-post kl. 09:00 med relevante transaksjoner.

## Kilder

| Land | Kilde | Type |
|------|-------|------|
| 🇳🇴 | Estate Nyheter | Nyheter |
| 🇳🇴 | Finansavisen Eiendom | Nyheter |
| 🇳🇴 | Newsec Norge | Megler |
| 🇸🇪 | Fastighetsvärlden | Nyheter |
| 🇸🇪 | DI Fastigheter | Nyheter |
| 🇸🇪 | Newsec Sverige | Megler |
| 🇩🇰 | Estate Media DK | Nyheter |
| 🇩🇰 | EDC Erhverv | Megler |
| 🇫🇮 | Newsec Finland | Megler |

## Oppsett (5 minutter)

### 1. Fork eller klon dette repoet

```bash
git clone https://github.com/DITT-BRUKERNAVN/nordic-deal-flow.git
cd nordic-deal-flow
```

### 2. Lag API-nøkler

**Anthropic (Claude API):**
1. Gå til https://console.anthropic.com
2. Klikk "API Keys" → "Create Key"
3. Kopier nøkkelen (starter med `sk-ant-...`)

**Resend (e-post):**
1. Gå til https://resend.com og lag konto (gratis)
2. Klikk "API Keys" → "Create API Key"
3. Kopier nøkkelen (starter med `re_...`)

### 3. Legg inn secrets i GitHub

1. Gå til ditt repo på GitHub
2. Settings → Secrets and variables → Actions → New repository secret
3. Legg inn disse fire:

| Secret name | Verdi |
|-------------|-------|
| `ANTHROPIC_API_KEY` | `sk-ant-din-nøkkel` |
| `RESEND_API_KEY` | `re_din-nøkkel` |
| `EMAIL_TO` | `din@epost.no` |
| `EMAIL_FROM` | `onboarding@resend.dev` |

> **Tips:** `onboarding@resend.dev` er Resends test-avsender som fungerer umiddelbart. Når du har verifisert ditt eget domene i Resend kan du bytte til `deals@dittdomene.no`.

### 4. Test med én gang

Gå til Actions-fanen i GitHub → "Nordic Deal Flow" → "Run workflow" → klikk den grønne knappen.

Sjekk innboksen din etter ca. 2-3 minutter.

### 5. Ferdig!

Agenten kjører nå automatisk kl. 09:00 hver morgen. Du trenger ikke gjøre noe mer.

## Teste lokalt

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export EMAIL_TO=din@epost.no
python nordic_deal_flow.py
```

Uten Resend-nøkkel lagres e-posten som en HTML-fil du kan åpne i nettleseren.

## Tilpasning

I `nordic_deal_flow.py` kan du endre:

- `MIN_RELEVANCE` – terskel for hva som inkluderes (default: 60)
- `LOOKBACK_HOURS` – tidsvindu (default: 24 timer)
- `RSS_SOURCES` / `WEB_SOURCES` – legg til eller fjern kilder
- `ANALYSIS_PROMPT` – juster hva Claude ser etter

## Kostnad

- **Claude API:** ~$0.50–1.50/dag avhengig av antall artikler
- **Resend:** Gratis (100 e-poster/dag)
- **GitHub Actions:** Gratis for offentlige repos, 2000 min/mnd for private

## Lisens

MIT
