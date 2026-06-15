# F5Bot → xflowpay Google Sheet — Setup Guide

## What this does
Every day, this script:
1. Reads your Gmail for F5Bot alert emails (last 24 hrs)
2. Extracts every Reddit thread link from those emails
3. Asks Claude to score each thread 1–10 for xflowpay relevance
4. Writes threads scoring ≥ 7 into a Google Sheet with a suggested comment angle

---

## One-time setup

### 1. Install dependencies
```bash
cd ~/f5bot-parser
pip install -r requirements.txt
```

### 2. Get a Google OAuth credential

1. Go to https://console.cloud.google.com/
2. Create a new project (or use an existing one)
3. Enable these two APIs:
   - **Gmail API**
   - **Google Sheets API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
5. Application type: **Desktop app**
6. Download the JSON → save it as `credentials.json` inside `~/f5bot-parser/`

### 3. Set environment variables

Add these to your shell profile (`~/.zshrc` or `~/.bash_profile`):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."          # your Anthropic API key
export SPREADSHEET_ID="1BxiM..."               # Google Sheet ID (from its URL)
```

Then reload: `source ~/.zshrc`

**Finding the Spreadsheet ID:**  
Open your Google Sheet → look at the URL:  
`https://docs.google.com/spreadsheets/d/`**`1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`**`/edit`  
The bold part is the ID.

> The script will auto-create a tab called **"F5Bot Leads"** with headers on first run.

### 4. First run (authorize Gmail access)

```bash
cd ~/f5bot-parser
python main.py
```

A browser window will open asking you to authorize Gmail + Sheets access.  
After that, a `token.json` file is saved — you won't need to log in again.

---

## Run it daily (cron)

```bash
crontab -e
```

Add this line to run every morning at 8 AM:

```
0 8 * * * cd /Users/xflow/f5bot-parser && /usr/bin/python3 main.py >> /tmp/f5bot.log 2>&1
```

Or if you use a virtualenv:
```
0 8 * * * cd /Users/xflow/f5bot-parser && /path/to/venv/bin/python main.py >> /tmp/f5bot.log 2>&1
```

---

## Output columns in the Sheet

| Column | Description |
|---|---|
| Date Found | Date the email was processed |
| Score | Claude's relevance score (1–10) |
| Subreddit | e.g. `r/india` |
| Title | Reddit post title |
| Reddit URL | Direct link to the thread |
| F5Bot Keyword | Which keyword triggered the alert |
| Why Relevant | One-line reason from Claude |
| Comment Angle | Suggested way to mention xflowpay naturally |
| Snippet | Post text excerpt |
| Status | Starts as "New" — change to "Commented", "Skip", etc. |

---

## Tuning

- **Raise/lower the threshold** — change `RELEVANCE_THRESHOLD = 7` in `main.py`
- **Look back further** — change `DAYS_BACK = 1` to `2` or `3`
- **Update the scoring criteria** — edit the `XFLOW_CONTEXT` block in `main.py`
