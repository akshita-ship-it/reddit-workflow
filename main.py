"""
F5Bot Gmail Parser for xflowpay.com
-------------------------------------
Reads F5Bot alert emails from Gmail, scores each Reddit thread using Claude
for relevance to xflowpay.com, and writes qualifying threads to Google Sheets.

Run daily via cron or manually:
    python main.py
"""

import os
import re
import json
import base64
from datetime import datetime, timedelta
from email import message_from_bytes

import time
import urllib.request

import anthropic
import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

GOOGLE_CREDENTIALS_FILE = 'credentials.json'   # OAuth client secret from GCP
GOOGLE_TOKEN_FILE       = 'token.json'          # auto-created after first login

SPREADSHEET_ID   = os.environ.get('SPREADSHEET_ID', '1ADQ8aXk2arLJ-URtfnnE3M-7koBxlwIgbma6_KrSrGg')   # Google Sheet ID
SHEET_NAME       = 'F5Bot Leads'
RELEVANCE_THRESHOLD = 6    # 1-10; threads scoring >= this go into the sheet
DAYS_BACK        = int(os.environ.get('DAYS_BACK', 1))  # override via env for manual runs

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
]

XFLOW_CONTEXT = """
xflowpay.com is an Indian cross-border payments fintech with four core products:

1. INWARD REMITTANCES / EXPORT PAYMENTS: Indian freelancers, agencies, and
   exporters receive payments from foreign clients in USD/EUR/GBP with better
   forex rates and lower fees than Wise, Skydo, Payoneer, or traditional banks.

2. STABLECOIN SETTLEMENT: Indian businesses can collect cross-border payments
   in stablecoins (USDT/USDC) for fast, low-cost international settlement
   without traditional banking friction.

3. PAYMENTS INFRASTRUCTURE API: xflowpay offers APIs so other Indian businesses
   and fintechs can embed cross-border payment collection into their own products.
   Relevant for developers/CTOs building payment features, or startups looking for
   a cross-border payment API provider.

4. IMPORT PAYMENTS (Global-to-India): Global businesses and sellers can collect
   payments FROM Indian customers/importers. Relevant for international companies
   wanting to accept INR or serve Indian buyers.

Primary competitors: Wise, Skydo, Payoneer, PayPal, Razorpay, Cashfree,
traditional Indian banks, SWIFT.

Ideal threads to comment on (score 7-10):
- Indian freelancers/agencies/exporters asking how to receive USD/EUR/GBP
- Anyone asking for Wise/Skydo/Payoneer alternatives for receiving money in India
- Businesses asking about inward remittances, FEMA, or RBI compliance
- Anyone complaining about high fees, slow transfers, or rejected international payments
- Developers/startups looking for a cross-border payment API or infrastructure
- Stablecoin/USDT threads about business payment settlement or cross-border trade
- Global businesses asking how to collect payments from Indian customers
- International sellers/SaaS companies asking how to accept payments from India

Borderline relevant (score 5-6):
- General international payment platform comparisons (even if not India-specific)
- Fintech API discussions around cross-border or FX payments
- Crypto payment settlement threads for businesses

Not relevant (score 1-3):
- Purely outbound remittances (Indians sending money abroad for personal use)
- Purely European/US domestic payment discussions with zero India connection
- Job listings, news articles, spam, or promotional posts
- Unrelated keyword matches (e.g. "xflow" in gaming, tech, or other contexts)
"""

# ── Google Auth ───────────────────────────────────────────────────────────────

def get_credentials() -> Credentials:
    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return creds

# ── Gmail ─────────────────────────────────────────────────────────────────────

def fetch_f5bot_emails(service, days_back: int = 1) -> list[dict]:
    """Return raw parsed email objects for F5Bot alerts from the last N days."""
    since = (datetime.now() - timedelta(days=days_back)).strftime('%Y/%m/%d')
    query = f'from:f5bot.com after:{since}'

    result   = service.users().messages().list(userId='me', q=query).execute()
    messages = result.get('messages', [])

    emails = []
    for msg in messages:
        raw   = service.users().messages().get(userId='me', id=msg['id'], format='raw').execute()
        raw_b = base64.urlsafe_b64decode(raw['raw'].encode('ASCII'))
        emails.append(message_from_bytes(raw_b))

    return emails


def get_email_body(email_obj) -> str:
    """Extract plain-text body from an email object."""
    if email_obj.is_multipart():
        for part in email_obj.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                return part.get_payload(decode=True).decode('utf-8', errors='ignore')
        # Fallback: first text/html part
        for part in email_obj.walk():
            if part.get_content_type() == 'text/html':
                return part.get_payload(decode=True).decode('utf-8', errors='ignore')
    return email_obj.get_payload(decode=True).decode('utf-8', errors='ignore')


def parse_threads(email_obj) -> list[dict]:
    """
    Parse all Reddit thread links + context out of one F5Bot email.

    Actual F5Bot format:
        Keyword: "wise alternative"
          Reddit Posts (/r/india/): 'Post title here' by username
            https://f5bot.com/url?u=https%3A%2F%2Fwww.reddit.com%2F...&i=...&h=...
    """
    from urllib.parse import urlparse, parse_qs, unquote

    subject = email_obj.get('Subject', '')
    body    = get_email_body(email_obj)

    threads = []
    lines   = body.splitlines()

    current_keyword = 'unknown'

    for i, line in enumerate(lines):
        line_stripped = line.strip()

        # Track keyword blocks: 'Keyword: "wise alternative"'
        kw_match = re.match(r'Keyword:\s*["\']?(.+?)["\']?\s*$', line_stripped, re.IGNORECASE)
        if kw_match:
            current_keyword = kw_match.group(1).strip().strip('"\'')
            continue

        # Detect F5Bot redirect URLs containing Reddit links
        if 'f5bot.com/url' not in line_stripped and 'reddit.com' not in line_stripped:
            continue

        # Extract the real Reddit URL
        reddit_url = None
        if 'f5bot.com/url' in line_stripped:
            # URL-decode the ?u= parameter
            qs_match = re.search(r'[?&]u=([^&\s]+)', line_stripped)
            if qs_match:
                reddit_url = unquote(qs_match.group(1)).rstrip('/')
        elif 'reddit.com' in line_stripped:
            url_match = re.search(r'https?://[^\s]+reddit\.com[^\s]+', line_stripped)
            if url_match:
                reddit_url = url_match.group(0).rstrip('/')

        if not reddit_url or '/comments/' not in reddit_url:
            continue

        # Extract subreddit from URL
        sub_match = re.search(r'reddit\.com/r/(\w+)', reddit_url)
        subreddit = sub_match.group(1) if sub_match else 'unknown'

        # Title is on the line just above the URL line
        # Format: "  Reddit Posts (/r/sub/): 'Title here' by username"
        title = ''
        if i > 0:
            prev_line = lines[i - 1].strip()
            # Extract title from between quotes
            title_match = re.search(r"['‘’](.+?)['‘’]", prev_line)
            if title_match:
                title = title_match.group(1).strip()
            elif prev_line and not prev_line.lower().startswith('http'):
                title = prev_line

        threads.append({
            'url':       reddit_url,
            'subreddit': subreddit,
            'title':     title or f'Thread in r/{subreddit}',
            'snippet':   '',          # F5Bot emails don't include snippets
            'keyword':   current_keyword,
        })

    return threads

# ── Reddit Content Fetcher ────────────────────────────────────────────────────

def fetch_reddit_content(url: str) -> str:
    """
    Fetch post body + top comments from a Reddit thread using the public JSON API.
    No API key needed — appending .json to any Reddit URL returns full content.
    Returns a plain-text summary capped at 2000 chars.
    """
    try:
        json_url = url.rstrip('/') + '.json?limit=10'
        # Reddit requires a descriptive User-Agent or it returns 429/403
        req = urllib.request.Request(
            json_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; xflowpay-lead-finder/1.0; +https://xflowpay.com)',
                'Accept': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return ''
            data = json.loads(resp.read().decode('utf-8'))

        parts = []

        # Post body (selftext)
        post = data[0]['data']['children'][0]['data']
        selftext = post.get('selftext', '').strip()
        if selftext and selftext != '[deleted]' and selftext != '[removed]':
            parts.append(f"POST BODY:\n{selftext[:1000]}")

        # Top-level comments
        comments = data[1]['data']['children']
        comment_texts = []
        for c in comments:
            if c.get('kind') != 't1':
                continue
            body = c['data'].get('body', '').strip()
            if body and body not in ('[deleted]', '[removed]'):
                comment_texts.append(body[:300])
            if len(comment_texts) >= 5:
                break

        if comment_texts:
            parts.append("TOP COMMENTS:\n" + '\n---\n'.join(comment_texts))

        content = '\n\n'.join(parts)
        return content[:2000] if content else ''

    except Exception:
        return ''


# ── Claude Scoring ────────────────────────────────────────────────────────────

SCORE_PROMPT = """\
You evaluate Reddit threads to decide if xflowpay.com should comment on them.

== About xflowpay ==
{context}

== Thread ==
Subreddit    : r/{subreddit}
Title        : {title}
F5Bot kw     : {keyword}
Full content :
{post_content}

== Task ==
Score this thread 1–10 for how worthwhile it is for xflowpay to comment.
10 = someone explicitly asking for a platform to receive international payments.
7-9 = strong fit: alternative-hunting, fee complaints, freelancer payment issues.
4-6 = indirect fit: could mention xflow but it's a stretch.
1-3 = not relevant.

Reply with ONLY valid JSON, no markdown:
{{
  "score": <integer 1-10>,
  "reason": "<one sentence>",
  "comment_angle": "<one sentence: how xflowpay could be naturally mentioned>"
}}
"""


def score_thread(client: anthropic.Anthropic, thread: dict) -> dict:
    prompt = SCORE_PROMPT.format(
        context      = XFLOW_CONTEXT,
        subreddit    = thread['subreddit'],
        title        = thread['title'],
        keyword      = thread['keyword'],
        post_content = thread.get('post_content', '(no content fetched)'),
    )

    resp = client.messages.create(
        model      = 'claude-opus-4-5',
        max_tokens = 300,
        messages   = [{'role': 'user', 'content': prompt}],
    )

    raw = resp.content[0].text.strip()
    # Strip accidental markdown fences
    raw = re.sub(r'^```(?:json)?|```$', '', raw, flags=re.MULTILINE).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        return {'score': 0, 'reason': 'parse error', 'comment_angle': ''}

# ── Google Sheets ─────────────────────────────────────────────────────────────

HEADERS = [
    'Date Found', 'Score', 'Subreddit', 'Title', 'Reddit URL',
    'F5Bot Keyword', 'Why Relevant', 'Comment Angle', 'Post Content', 'Status',
]


def write_to_sheet(creds: Credentials, rows: list[list]) -> None:
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS, value_input_option='RAW')
        # Basic formatting: freeze header row
        ws.freeze(rows=1)

    ws.append_rows(rows, value_input_option='USER_ENTERED')

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  F5Bot → xflowpay Sheet  |  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*60}\n")

    if not SPREADSHEET_ID:
        raise SystemExit(
            "ERROR: Set the SPREADSHEET_ID environment variable to your "
            "Google Sheet ID (from the sheet URL)."
        )

    creds         = get_credentials()
    gmail_service = build('gmail', 'v1', credentials=creds)
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        raise SystemExit("ERROR: ANTHROPIC_API_KEY is not set.")
    claude = anthropic.Anthropic(api_key=api_key)

    # 1. Fetch emails
    print(f"📬 Fetching F5Bot emails from last {DAYS_BACK} day(s)…")
    emails = fetch_f5bot_emails(gmail_service, days_back=DAYS_BACK)
    print(f"   {len(emails)} email(s) found\n")

    if not emails:
        print("Nothing to process. Exiting.")
        return

    # 2. Parse threads
    all_threads: list[dict] = []
    for email_obj in emails:
        all_threads.extend(parse_threads(email_obj))

    # Deduplicate by post URL (strip comment-level paths like /c/commentid)
    def post_url(url: str) -> str:
        m = re.search(r'(reddit\.com/r/\w+/comments/\w+)', url)
        return m.group(1) if m else url

    seen: set[str] = set()
    unique: list[dict] = []
    for t in all_threads:
        key = post_url(t['url'])
        if key not in seen:
            seen.add(key)
            # Normalise URL to post-level
            t['url'] = 'https://' + key
            unique.append(t)

    print(f"🔗 {len(unique)} unique Reddit thread(s) extracted\n")

    # 3. Fetch full Reddit content for each thread
    print("Fetching Reddit thread content...")
    for i, thread in enumerate(unique, 1):
        content = fetch_reddit_content(thread['url'])
        thread['post_content'] = content
        status = f"{len(content)} chars" if content else "unavailable"
        print(f"  [{i}/{len(unique)}] r/{thread['subreddit']} — {status}")
        time.sleep(0.5)   # be polite to Reddit's servers
    print()

    # 4. Score with Claude
    relevant: list[dict] = []
    for i, thread in enumerate(unique, 1):
        short_title = thread['title'][:65]
        print(f"[{i}/{len(unique)}] Scoring: {short_title!r}")
        result = score_thread(claude, thread)

        thread.update(result)
        score = result.get('score', 0)

        if score >= RELEVANCE_THRESHOLD:
            relevant.append(thread)
            print(f"         ✅  {score}/10 — {result.get('reason', '')}")
        else:
            print(f"         ❌  {score}/10 — below threshold")

    print(f"\n📊 {len(relevant)} relevant thread(s) (score ≥ {RELEVANCE_THRESHOLD})\n")

    # 5. Write to Google Sheets
    if not relevant:
        print("No relevant threads today. Sheet not updated.")
        return

    sheet_rows = [
        [
            datetime.now().strftime('%Y-%m-%d'),
            t.get('score', ''),
            f"r/{t['subreddit']}",
            t['title'],
            t['url'],
            t['keyword'],
            t.get('reason', ''),
            t.get('comment_angle', ''),
            t.get('post_content', '')[:500],
            'New',
        ]
        for t in relevant
    ]

    write_to_sheet(creds, sheet_rows)
    print(f"✅  {len(sheet_rows)} row(s) added to '{SHEET_NAME}' tab in your Google Sheet.")
    print("\nDone!\n")


if __name__ == '__main__':
    main()
