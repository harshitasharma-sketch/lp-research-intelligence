# LP Research Intelligence — MVP

A local web-app prototype for researching LP firms from a website URL, extracting configured datapoints, preserving source evidence, and exporting results.

## What is included
- Streamlit web interface: Overview, New research, Datapoint library, Research history.
- SQLite persistence (`lp_research.db`) for firms, research runs, datapoints and evidence.
- Firm-site crawler (same-domain pages, configurable page limit).
- Optional external web search through Tavily.
- Optional contextual extraction through OpenAI.
- Evidence fields: value, status, confidence, source type, source URL and supporting excerpt.
- CSV export for each research run.
- Seeded AUM datapoint based on the supplied specification.

## AUM datapoint seeded in the app
Definition: total market value of financial and alternative investments, including private equity, private debt, hedge funds, infrastructure, real estate and natural resources.

Candidate terms: Total Assets; Total investment; Fair value; Asset under management; Market value.

The app does not silently treat every balance-sheet asset as AUM. AI extraction is instructed to distinguish them where possible; verify results before relying on them.

## Run locally
Requires Python 3.10+.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
.venv\\Scripts\\Activate.ps1

pip install -r requirements.txt
cp .env.example .env
```

Set API keys in `.env`, then load them into your shell (or use a dotenv loader of your choice). For example on macOS/Linux:

```bash
export $(grep -v '^#' .env | xargs)
streamlit run app.py
```

On Windows, set `OPENAI_API_KEY` and `TAVILY_API_KEY` as environment variables before launching:

```powershell
streamlit run app.py
```

Open the local URL Streamlit prints (normally http://localhost:8501).

## API keys
- `OPENAI_API_KEY`: enables contextual evidence-based extraction. Without it, the app uses a basic keyword/number heuristic (low confidence).
- `TAVILY_API_KEY`: enables external web search. Without it, only the firm's own site is crawled.

The app can be launched without either key, but research coverage and extraction quality will be limited.

## Current MVP limitations
- This is a local prototype, not a deployed multi-user production service.
- External-source search uses Tavily and depends on a valid API key.
- PDFs, paywalled sources, login-only content and complex JavaScript sites may not be extracted reliably.
- The crawler is intentionally limited to same-domain HTML pages and a page cap.
- Search/extraction results require human review. “Not Found” means no supported match was identified in this run, not proof that the datapoint does not exist.
- No authentication, team permissions, scheduled refreshes or cloud backup yet.
- Review website terms and applicable crawling rules before researching at scale.

## Suggested next build increments
1. Add PDF and annual-report parsing.
2. Add source ranking, date capture, duplicate resolution and explicit firm-vs-third-party comparison.
3. Add editable datapoint definitions and per-datapoint qualification rules in the UI.
4. Deploy with managed PostgreSQL, authentication and secure secret storage.
