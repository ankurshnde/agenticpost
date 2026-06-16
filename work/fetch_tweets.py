import os
import sys
import json
import re
import asyncio
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

# Monkey-patch twikit to fix X (Twitter) changes and bugs
from twikit.x_client_transaction.transaction import ClientTransaction
from twikit.user import User

# 1. Patch the "KEY_BYTE indices" error during login/initialization
async def patched_get_indices(self, home_page_response, session, headers):
    html = str(home_page_response)
    
    id_match = re.search(r'(\d+)\s*:\s*["\']ondemand\.s["\']', html)
    if not id_match:
        id_match = re.search(r'["\']ondemand\.s["\']\s*:\s*["\']([\w]*)["\']', html)
        if id_match:
            hash_val = id_match.group(1)
            on_demand_file_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_val}a.js"
        else:
            raise Exception("Couldn't find ondemand.s ID or hash in index page")
    else:
        id_val = id_match.group(1)
        # Find the hash for this ID
        hash_match = re.search(r'"' + id_val + r'"\s*:\s*["\']([\da-f]+)["\']|' + id_val + r'\s*:\s*["\']([\da-f]+)["\']', html)
        if not hash_match:
            raise Exception(f"Couldn't find ondemand.s hash for ID {id_val}")
        hash_val = hash_match.group(1) or hash_match.group(2)
        on_demand_file_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_val}a.js"
        
    print(f"[+] [Patch] Fetching ondemand JS from: {on_demand_file_url}")
    on_demand_file_response = await session.request(method="GET", url=on_demand_file_url, headers=headers)
    js_text = on_demand_file_response.text
    
    # Find all indices using the new pattern W[index], 16
    key_byte_indices = re.findall(r'W\[(\d+)\],\s*16', js_text)
    if not key_byte_indices:
        INDICES_REGEX = re.compile(r"""(\(\w{1}\[(\d{1,2})\],\s*16\))+""", flags=(re.VERBOSE | re.MULTILINE))
        key_byte_indices_match = INDICES_REGEX.finditer(js_text)
        for item in key_byte_indices_match:
            key_byte_indices.append(item.group(2))
            
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices from JS")
        
    key_byte_indices = list(map(int, key_byte_indices))
    print("[+] [Patch] Successfully parsed KEY_BYTE indices:", key_byte_indices)
    return key_byte_indices[0], key_byte_indices[1:]

ClientTransaction.get_indices = patched_get_indices

# 2. Patch User.__init__ to prevent KeyError on missing optional profile fields
def patched_user_init(self, client, data: dict) -> None:
    self._client = client
    legacy = data.get('legacy', {})

    self.id: str = data.get('rest_id')
    self.created_at: str = legacy.get('created_at')
    self.name: str = legacy.get('name')
    self.screen_name: str = legacy.get('screen_name')
    self.profile_image_url: str = legacy.get('profile_image_url_https')
    self.profile_banner_url: str = legacy.get('profile_banner_url')
    self.url: str = legacy.get('url')
    self.location: str = legacy.get('location', '')
    self.description: str = legacy.get('description', '')
    
    entities = legacy.get('entities', {})
    description_entity = entities.get('description', {})
    self.description_urls: list = description_entity.get('urls', [])
    self.urls: list = entities.get('url', {}).get('urls', [])
    
    self.pinned_tweet_ids: list[str] = legacy.get('pinned_tweet_ids_str', [])
    self.is_blue_verified: bool = data.get('is_blue_verified', False)
    self.verified: bool = legacy.get('verified', False)
    self.possibly_sensitive: bool = legacy.get('possibly_sensitive', False)
    self.can_dm: bool = legacy.get('can_dm', False)
    self.can_media_tag: bool = legacy.get('can_media_tag', False)
    self.want_retweets: bool = legacy.get('want_retweets', False)
    self.default_profile: bool = legacy.get('default_profile', False)
    self.default_profile_image: bool = legacy.get('default_profile_image', False)
    self.has_custom_timelines: bool = legacy.get('has_custom_timelines', False)
    self.followers_count: int = legacy.get('followers_count', 0)
    self.fast_followers_count: int = legacy.get('fast_followers_count', 0)
    self.normal_followers_count: int = legacy.get('normal_followers_count', 0)
    self.following_count: int = legacy.get('friends_count', 0)
    self.favourites_count: int = legacy.get('favourites_count', 0)
    self.listed_count: int = legacy.get('listed_count', 0)
    self.media_count = legacy.get('media_count', 0)
    self.statuses_count: int = legacy.get('statuses_count', 0)
    self.is_translator: bool = legacy.get('is_translator', False)
    self.translator_type: str = legacy.get('translator_type', '')
    self.withheld_in_countries: list[str] = legacy.get('withheld_in_countries', [])
    self.protected: bool = legacy.get('protected', False)

User.__init__ = patched_user_init

# Now import Client
from twikit import Client

# Path constants
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'work', 'config.json')
SESSION_PATH = os.path.join(BASE_DIR, 'work', 'session.json')
HTML_PATH = os.path.join(BASE_DIR, 'index.html')

# Keywords dictionary matching the frontend categories
KEYWORDS = {
    'research': ["benchmark", "paper", "arxiv", "evaluation", "dataset", "research", "model", "architecture"],
    'development': ["framework", "mcp", "sdk", "api", "open source", "developer", "orchestration", "tool"],
    'enterprise': ["enterprise", "workflow", "productivity", "deployment", "agentforce", "copilot", "bedrock", "governance"],
    'policy': ["regulation", "guideline", "governance", "risk", "responsible", "compliance", "sec", "sebi", "cftc", "nist", "iosco"],
    'market': ["capital markets", "trading", "asset management", "market structure", "clearing", "surveillance", "liquidity", "risk"]
}

# Env Loader to parse .env file without external dependencies
def load_env():
    env_path = os.path.join(BASE_DIR, 'work', '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

def is_within_7_days(date_str):
    try:
        if not date_str:
            return False
        if 'GMT' in date_str or ',' in date_str:
            # RSS format: "Tue, 16 Jun 2026 13:00:00 GMT"
            dt = datetime.strptime(date_str.split(' GMT')[0].strip(), '%a, %d %b %Y %H:%M:%S')
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            # ISO format: "2026-06-16T13:00:00+00:00"
            if date_str.endswith('Z'):
                date_str = date_str[:-1] + '+00:00'
            dt = datetime.fromisoformat(date_str)
            
        now = datetime.now(timezone.utc)
        delta = now - dt
        return 0 <= delta.days <= 7
    except Exception:
        return True

def extract_feeds_from_html(html_content):
    feeds = []
    feed_blocks = re.findall(r'name:\s*["\'](.*?)["\'],\s*vertical:\s*["\'](.*?)["\'],.*?query:\s*["\'](.*?)["\']', html_content, re.DOTALL)
    for name, vertical, query in feed_blocks:
        feeds.append({
            'name': name,
            'vertical': vertical,
            'query': query,
            'url': f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        })
    return feeds

def score_item(title, summary):
    text = f"{title} {summary}".lower()
    score = 0
    for term_list in KEYWORDS.values():
        for term in term_list:
            if term in text:
                score += 1
    if "capital markets" in text and ("agent" in text or "ai" in text):
        score += 4
    if "policy" in text or "regulation" in text or "guideline" in text:
        score += 2
    if "research" in text or "benchmark" in text or "evaluation" in text:
        score += 2
    return score

def score_tweet(tweet):
    text = (tweet.get('text') or '').lower()
    author = (tweet.get('screen_name') or '').lower()
    score = 0
    
    tier1 = ["sama", "karpathy", "darioamodei", "demishassabis", "drfeifei", "ylecun", "openai", "googledeepmind", "google", "googleai", "sundarpichai", "mntruell"]
    tier2 = ["openaidevs", "deepseek_ai", "_mohansolo", "polynoamial", "officiallogank", "feinbergvlad", "bcherny", "trq212", "antigravity", "milichab"]
    
    if author in tier1:
        score += 60
    elif author in tier2:
        score += 35
    else:
        score += 10
        
    high_signal_keywords = [
        "mcp", "model context protocol", "claude code", "agent", "agents", "agentic", "orchestration", "framework",
        "reasoning", "breakthrough", "agi", "frontier", "gpt-5", "claude 4", "strawberry", "o1", "o3", "gemini 2",
        "pretraining", "released", "launch", "announcing", "announce", "paper", "research", "benchmark", "evaluation",
        "scaling", "gpu", "cluster", "compute", "investment", "acquisitions", "acquire", "partnership"
    ]
    
    for kw in high_signal_keywords:
        if kw in text:
            score += 8
            
    if "breakthrough" in text or "revolution" in text or "next generation" in text:
        score += 15
    if "paper" in text or "arxiv" in text:
        score += 10
    if "open source" in text or "github" in text:
        score += 10
        
    try:
        dt = datetime.fromisoformat(tweet['created_at'].replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        age_hours = (now - dt).total_seconds() / 3600.0
        score -= age_hours * 1.5
    except Exception:
        pass
        
    return score

def classify_tweet_vertical(tweet):
    text = tweet.get('text', '').lower()
    scores = {v: 0 for v in KEYWORDS}
    for vert, terms in KEYWORDS.items():
        for term in terms:
            if term in text:
                scores[vert] += 1
                
    max_vert = max(scores, key=scores.get)
    if scores[max_vert] > 0:
        return max_vert
    
    author = tweet.get('screen_name', '').lower()
    research_authors = ["karpathy", "darioamodei", "sama", "demishassabis", "drfeifei", "ylecun", "openai", "googledeepmind", "polynoamial", "googleai"]
    dev_authors = ["openaidevs", "bcherny", "trq212", "mntruell", "antigravity"]
    policy_authors = ["swiss_un", "IGR_Media", "RBI", "FinMinIndia"]
    
    if author in research_authors:
        return "research"
    elif author in dev_authors:
        return "development"
    elif author in policy_authors:
        return "policy"
    else:
        return "market"

def clean_title(title):
    title = re.split(r'\s+[-|•]\s+', title)[0]
    return title.strip().rstrip('.,;:-')

def fetch_and_parse_feed(feed):
    req = urllib.request.Request(
        feed['url'],
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    )
    items = []
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        for item in root.findall('.//item'):
            title_el = item.find('title')
            link_el = item.find('link')
            date_el = item.find('pubDate')
            desc_el = item.find('description')
            
            title = title_el.text if title_el is not None else ''
            link = link_el.text if link_el is not None else ''
            date_str = date_el.text if date_el is not None else ''
            desc = desc_el.text if desc_el is not None else ''
            
            if is_within_7_days(date_str):
                clean_desc = re.sub('<[^<]+?>', '', desc).replace('&nbsp;', ' ').strip()
                score = score_item(title, clean_desc)
                items.append({
                    'title': title,
                    'link': link,
                    'date': date_str,
                    'summary': clean_desc,
                    'vertical': feed['vertical'],
                    'source': feed['name'],
                    'score': score
                })
    except Exception:
        pass
    return items

def generate_weekly_report_algorithmic(items_by_vertical):
    html = []
    vertical_labels = {
        'research': 'Research',
        'development': 'Development',
        'enterprise': 'Enterprise',
        'policy': 'Policy',
        'market': 'Capital Markets'
    }
    
    for vert, items in items_by_vertical.items():
        if not items:
            continue
        label = vertical_labels.get(vert, vert.capitalize())
        html.append(f'<div class="weekly-vert-block">')
        html.append(f'  <h4>{label}</h4>')
        
        # Algorithmic TL;DR generation
        title1 = clean_title(items[0]['title'])
        if len(items) > 1:
            title2 = clean_title(items[1]['title'])
            tldr_text = f"Major developments in {label} focus on {title1} and {title2}."
        else:
            tldr_text = f"Key development in {label} focuses on {title1}."
            
        if len(tldr_text) > 150:
            tldr_text = tldr_text[:147] + "..."
            
        html.append(f'  <p class="weekly-tldr"><strong>TL;DR</strong> {tldr_text}</p>')
        
        html.append(f'  <ul>')
        for item in items[:2]:
            cleaned = clean_title(item['title'])
            source = item.get('source', '')
            link = item.get('link', '#')
            if source:
                html.append(f'    <li><a href="{link}" target="_blank" rel="noopener noreferrer">{cleaned}</a> ({source})</li>')
            else:
                html.append(f'    <li><a href="{link}" target="_blank" rel="noopener noreferrer">{cleaned}</a></li>')
        html.append(f'  </ul>')
        html.append(f'</div>')
        
    if not html:
        return '<p class="empty-weekly">No major institutional signals recorded this week.</p>'
        
    return '\n'.join(html)

def generate_weekly_report_gemini(api_key, items_by_vertical):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    summary_data = {}
    for vert, items in items_by_vertical.items():
        if items:
            summary_data[vert] = [{
                'title': clean_title(item['title']),
                'source': item['source'],
                'link': item['link']
            } for item in items[:4]]
            
    prompt = f"""You are an institutional research analyst. Synthesize a weekly institutional report based on these AI developments.
Format the output as a clean, brief HTML fragment.
Rules:
1. Use very short, simple English.
2. Group by these five verticals only: Research, Development, Enterprise, Policy, Capital Markets.
3. If a vertical has no important news, DO NOT mention it (no filler gaps!).
4. For each active vertical, structure it precisely as follows:
   <div class="weekly-vert-block">
     <h4>[Vertical Name]</h4>
     <p class="weekly-tldr"><strong>TL;DR</strong> [A concise 1-sentence summary of all developments in this vertical]</p>
     <ul>
       <li><a href="[link]" target="_blank" rel="noopener noreferrer">[Cleaned Title]</a> ([Source])</li>
     </ul>
   </div>
5. Wrap the title of each bullet point in a hyperlink matching the provided link.
6. Do not wrap in ```html or other markdown blocks. Just output raw HTML.

Input data:
{json.dumps(summary_data, indent=2)}
"""
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            html_content = res_data['candidates'][0]['content']['parts'][0]['text']
            return html_content.strip()
    except Exception as e:
        print(f"Error generating report via Gemini: {e}")
        return None

def generate_weekly_report_openai(api_key, items_by_vertical):
    url = "https://api.openai.com/v1/chat/completions"
    summary_data = {}
    for vert, items in items_by_vertical.items():
        if items:
            summary_data[vert] = [{
                'title': clean_title(item['title']),
                'source': item['source'],
                'link': item['link']
            } for item in items[:4]]
            
    prompt = f"""You are an institutional research analyst. Synthesize a weekly institutional report based on these AI developments.
Format the output as a clean, brief HTML fragment.
Rules:
1. Use very short, simple English.
2. Group by these five verticals only: Research, Development, Enterprise, Policy, Capital Markets.
3. If a vertical has no important news, DO NOT mention it (no filler gaps!).
4. For each active vertical, structure it precisely as follows:
   <div class="weekly-vert-block">
     <h4>[Vertical Name]</h4>
     <p class="weekly-tldr"><strong>TL;DR</strong> [A concise 1-sentence summary of all developments in this vertical]</p>
     <ul>
       <li><a href="[link]" target="_blank" rel="noopener noreferrer">[Cleaned Title]</a> ([Source])</li>
     </ul>
   </div>
5. Wrap the title of each bullet point in a hyperlink matching the provided link.
6. Do not wrap in ```html or other markdown blocks. Just output raw HTML.

Input data:
{json.dumps(summary_data, indent=2)}
"""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            html_content = res_data['choices'][0]['message']['content']
            return html_content.strip()
    except Exception as e:
        print(f"Error generating report via OpenAI: {e}")
        return None


TECH_RELEVANT_KEYWORDS = [
    "ai", "agent", "llm", "model", "intelligence", "deep learning", "machine learning",
    "gpu", "compute", "mcp", "software", "developer", "framework", "coding", "code",
    "tech", "technology", "silicon", "chip", "openai", "claude", "gemini", "deepseek",
    "llama", "reasoning", "benchmark", "paper", "research", "arxiv", "observability",
    "cursor"
]

def get_title_tokens(title):
    words = re.findall(r'\b\w+\b', title.lower())
    stopwords = {"and", "the", "for", "with", "from", "that", "this", "about", "latest", "new", "released", "launch", "how", "why"}
    return {w for w in words if w not in stopwords and len(w) > 2}

def calculate_similarity(title1, title2):
    tokens1 = get_title_tokens(title1)
    tokens2 = get_title_tokens(title2)
    if not tokens1 or not tokens2:
        return 0.0
    return len(tokens1.intersection(tokens2)) / min(len(tokens1), len(tokens2))

def deduplicate_items(items):
    deduped = []
    for item in items:
        found_match = False
        for existing in deduped:
            if existing['vertical'] == item['vertical'] and calculate_similarity(existing['title'], item['title']) >= 0.65:
                found_match = True
                if item['score'] > existing['score']:
                    old_secondary = existing.get('secondary_sources', [])
                    new_secondary = old_secondary + [{'source': existing['source'], 'link': existing['link']}]
                    existing.update(item)
                    existing['secondary_sources'] = new_secondary
                else:
                    if 'secondary_sources' not in existing:
                        existing['secondary_sources'] = []
                    existing['secondary_sources'].append({'source': item['source'], 'link': item['link']})
                break
        if not found_match:
            item['secondary_sources'] = []
            deduped.append(item)
    return deduped

def compile_trends_data(all_items):
    trends = {
        'research': [0]*7,
        'development': [0]*7,
        'enterprise': [0]*7,
        'policy': [0]*7,
        'market': [0]*7
    }
    now = datetime.now(timezone.utc)
    for item in all_items:
        vert = item.get('vertical')
        if vert not in trends:
            continue
        date_str = item.get('date')
        try:
            if not date_str:
                continue
            if 'GMT' in date_str or ',' in date_str:
                dt = datetime.strptime(date_str.split(' GMT')[0].strip(), '%a, %d %b %Y %H:%M:%S')
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                if date_str.endswith('Z'):
                    date_str = date_str[:-1] + '+00:00'
                dt = datetime.fromisoformat(date_str)
            
            delta = now - dt
            delta_days = delta.days
            if 0 <= delta_days < 7:
                trends[vert][6 - delta_days] += 1
        except Exception:
            pass
    return trends

def is_tech_relevant(text):
    text_lower = text.lower()
    return any(kw in text_lower for kw in TECH_RELEVANT_KEYWORDS)

def compile_weekly_report(html_content, tweets):
    load_env()
    feeds = extract_feeds_from_html(html_content)
    print(f"[+] Extracted {len(feeds)} RSS feed configurations from HTML.")
    
    print("[+] Fetching RSS feeds in parallel for weekly report...")
    rss_items = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_and_parse_feed, feed) for feed in feeds]
        for f in futures:
            rss_items.extend(f.result())
            
    print(f"[+] Fetched {len(rss_items)} RSS items within 7 days.")
    
    weekly_tweets = []
    for tweet in tweets:
        text = tweet.get('text', '')
        # Filter out retweets and non-tech-relevant tweets to keep weekly signal high
        if text.startswith('RT @') or not is_tech_relevant(text):
            continue
            
        if is_within_7_days(tweet.get('created_at', '')):
            score = score_tweet(tweet)
            if score >= 12:
                vert = classify_tweet_vertical(tweet)
                weekly_tweets.append({
                    'title': tweet['text'],
                    'link': f"https://x.com/{tweet['screen_name']}/status/{tweet['id']}",
                    'date': tweet['created_at'],
                    'summary': tweet['text'],
                    'vertical': vert,
                    'source': f"@{tweet['screen_name']}",
                    'score': score
                })
                
    print(f"[+] Filtered {len(weekly_tweets)} high-signal weekly tweets.")
    
    # De-duplicate all items (RSS + Tweets) before compiling report and trends
    deduped_items = deduplicate_items(rss_items + weekly_tweets)
    
    # Compile trends from de-duplicated items
    trends_data = compile_trends_data(deduped_items)
    
    # Re-group deduped items for report generation
    items_by_vertical = {
        'research': [],
        'development': [],
        'enterprise': [],
        'policy': [],
        'market': []
    }
    
    for item in deduped_items:
        vert = item['vertical']
        if vert in items_by_vertical:
            items_by_vertical[vert].append(item)
            
    for vert in items_by_vertical:
        items_by_vertical[vert].sort(key=lambda x: x['score'], reverse=True)
        
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        print("[+] Generating Weekly Report using Gemini API...")
        html_report = generate_weekly_report_gemini(api_key, items_by_vertical)
        if not html_report:
            print("[+] Gemini failed, falling back to algorithmic report.")
            html_report = generate_weekly_report_algorithmic(items_by_vertical)
    else:
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            print("[+] Generating Weekly Report using OpenAI API...")
            html_report = generate_weekly_report_openai(openai_key, items_by_vertical)
            if not html_report:
                print("[+] OpenAI failed, falling back to algorithmic report.")
                html_report = generate_weekly_report_algorithmic(items_by_vertical)
        else:
            print("[+] No API Key found, generating Weekly Report algorithmically...")
            html_report = generate_weekly_report_algorithmic(items_by_vertical)
            
    return html_report, trends_data

async def main():
    # Parse CLI arguments
    priority_only = "--priority-only" in sys.argv

    print("=" * 60)
    print(f"X (Twitter) Fetcher Started at {datetime.now().isoformat()}")
    print(f"Mode: {'Priority Accounts Only' if priority_only else 'All Accounts'}")
    print("=" * 60)

    # 1. Load config
    if not os.path.exists(CONFIG_PATH):
        print(f"[-] Config file not found at: {CONFIG_PATH}")
        sys.exit(1)
    
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)

    username = config.get("username")
    email = config.get("email")
    password = config.get("password")
    
    high_priority_accounts = config.get("high_priority_accounts", [])
    regular_accounts = config.get("regular_accounts", [])

    if not username or "YOUR_THROWAWAY" in username:
        print("[-] Please configure your X credentials in config.json before running.")
        sys.exit(1)

    # Determine accounts to fetch
    accounts_to_fetch = high_priority_accounts if priority_only else (high_priority_accounts + regular_accounts)

    # 2. Parse existing HTML data if in priority mode
    existing_tweets = []
    if priority_only and os.path.exists(HTML_PATH):
        print("[+] Parsing existing tweets from HTML to preserve standard accounts...")
        try:
            with open(HTML_PATH, 'r', encoding='utf-8') as f:
                html_content = f.read()
            match = re.search(r'/\* TWEET_DATA_START \*/(.*?)/\* TWEET_DATA_END \*/', html_content, re.DOTALL)
            if match:
                existing_tweets = json.loads(match.group(1).strip())
                print(f"[+] Loaded {len(existing_tweets)} existing tweets from HTML.")
        except Exception as e:
            print(f"[-] Warning: Failed to parse existing HTML tweets: {e}")

    # 3. Initialize Client
    proxy = config.get("proxy")
    if proxy:
        print(f"[+] Using configured proxy: {proxy}")
        client = Client('en-US', proxy=proxy)
    else:
        client = Client('en-US')

    # 4. Authenticate
    authenticated = False
    if os.path.exists(SESSION_PATH):
        print("[+] Loading session cookies from session.json...")
        try:
            client.load_cookies(SESSION_PATH)
            await client.get_user_by_screen_name("sama")
            print("[+] Session loaded and verified successfully.")
            authenticated = True
        except Exception as e:
            print(f"[-] Failed to verify session: {e}. Re-authenticating...")
            try:
                os.remove(SESSION_PATH)
            except Exception:
                pass

    if not authenticated:
        print(f"[+] Logging in to X as {username}...")
        try:
            await client.login(
                auth_info_1=username,
                auth_info_2=email,
                password=password
            )
            client.save_cookies(SESSION_PATH)
            print("[+] Login successful. Saved session.json.")
        except Exception as e:
            print(f"[-] Login failed: {e}")
            print("\n[-] Suggestion: If you are seeing a Cloudflare Block (status 403), run the script locally")
            print("[-] on your residential IP, or export cookies from a logged-in browser session to work/session.json")
            sys.exit(1)

    # 5. Fetch Tweets
    new_tweets = []
    print(f"[+] Fetching tweets from {len(accounts_to_fetch)} accounts...")

    for idx, screen_name in enumerate(accounts_to_fetch, 1):
        print(f"    [{idx}/{len(accounts_to_fetch)}] Fetching @{screen_name}...")
        try:
            user = await client.get_user_by_screen_name(screen_name)
            # Fetch latest 10 tweets
            tweets = await user.get_tweets('Tweets', count=10)
            
            for tweet in tweets:
                if not tweet.text:
                    continue
                tweet_data = {
                    "id": tweet.id,
                    "text": tweet.text,
                    "created_at": tweet.created_at_datetime.isoformat(),
                    "screen_name": screen_name,
                    "user_name": user.name,
                    "is_red_list": screen_name.lower() in [r.lower() for r in high_priority_accounts]
                }
                new_tweets.append(tweet_data)
            
            # Randomized jitter delay
            import random
            delay = 2.0 + random.random() * 3.0
            print(f"    [+] Sleeping for {delay:.2f}s (jitter)...")
            await asyncio.sleep(delay)
        except Exception as e:
            print(f"    [-] Error fetching @{screen_name}: {e}")
            continue

    if not new_tweets and not existing_tweets:
        print("[-] No tweets were fetched and no existing database found. Exiting.")
        sys.exit(1)

    # 6. Merge lists
    if priority_only:
        # Keep old regular/standard accounts' tweets; filter out old high priority ones (so they are replaced)
        filtered_existing = [
            t for t in existing_tweets 
            if t.get("screen_name", "").lower() not in [hp.lower() for hp in high_priority_accounts]
        ]
        merged_tweets = new_tweets + filtered_existing
    else:
        merged_tweets = new_tweets

    # Sort merged list by date descending
    merged_tweets.sort(key=lambda x: x["created_at"], reverse=True)
    print(f"[+] Combined total: {len(merged_tweets)} tweets.")

    # 7. Embed in HTML file
    if not os.path.exists(HTML_PATH):
        print(f"[-] HTML file not found at: {HTML_PATH}")
        sys.exit(1)

    print(f"[+] Reading HTML template: {HTML_PATH}")
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # Compile Weekly Report & Trends
    print("[+] Compiling Weekly Institutional Signal Report & Activity Trends...")
    try:
        weekly_report_html, trends_data = compile_weekly_report(html_content, merged_tweets)
        
        # Inject report HTML
        report_pattern = r'(<!-- WEEKLY_REPORT_START -->).*?(<!-- WEEKLY_REPORT_END -->)'
        html_content = re.sub(report_pattern, lambda m: f"{m.group(1)}\n{weekly_report_html}\n{m.group(2)}", html_content, flags=re.DOTALL)
        
        # Inject trends JSON
        trends_pattern = r'(/\* TRENDS_DATA_START \*/).*?(/\* TRENDS_DATA_END \*/)'
        trends_json = json.dumps(trends_data, indent=2)
        html_content = re.sub(trends_pattern, lambda m: f"{m.group(1)} {trends_json} {m.group(2)}", html_content, flags=re.DOTALL)
        
        print("[+] Weekly report and activity trends compiled and injected.")
    except Exception as e:
        print(f"[-] Warning: Failed to compile report/trends: {e}")

    # Regex substitution for tweet data between the markers
    pattern = r'(/\* TWEET_DATA_START \*/).*?(/\* TWEET_DATA_END \*/)'
    json_str = json.dumps(merged_tweets, indent=2)
    new_html = re.sub(pattern, lambda m: f"{m.group(1)} {json_str} {m.group(2)}", html_content, flags=re.DOTALL)

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(new_html)
    print("[+] HTML file updated successfully with both tweets and weekly report.")

    print("[+] Done!")

if __name__ == "__main__":
    asyncio.run(main())
