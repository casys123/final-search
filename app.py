# app.py
import re
import time
import base64
import sqlite3
import json
import os
from urllib.parse import urlparse, urljoin, parse_qs

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st
from urllib.robotparser import RobotFileParser

# Optional: MX lookup (soft dependency)
try:
    import dns.resolver as dnsresolver  # requires "dnspython"
except Exception:
    dnsresolver = None

# ----------------------------
# Streamlit page setup
# ----------------------------
st.set_page_config(page_title="Miami Master Flooring Lead Finder", layout="wide")
st.title("🚀 Miami Master Flooring — Lead Finder & Email Drafts")

DEFAULT_SENDER = "info@miamimasterflooring.com"

# Session state
if "leads_df" not in st.session_state:
    st.session_state.leads_df = pd.DataFrame(
        columns=["Company", "Email", "Website", "Phone", "Source", "EmailVerified", "MXDomain"]
    )

# ----------------------------
# Helpers
# ----------------------------
UA_STR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
UA = {"User-Agent": UA_STR, "Accept-Language": "en-US,en;q=0.9"}

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_REGEX = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")

COMPETITOR_WORDS = {"floor", "tile", "carpet"}  # used if you want to exclude flooring companies
CACHE_DB = "cache.db"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# ----------------------------
# SQLite cache
# ----------------------------
def init_db():
    con = sqlite3.connect(CACHE_DB)
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS cache_pages (url TEXT PRIMARY KEY, html TEXT, fetched_at REAL)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS cache_robots (host TEXT PRIMARY KEY, content TEXT, fetched_at REAL)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS cache_emails (url TEXT PRIMARY KEY, emails TEXT, phone TEXT, fetched_at REAL)"
    )
    con.commit()
    con.close()

def cache_get_page(url: str):
    try:
        con = sqlite3.connect(CACHE_DB)
        cur = con.cursor()
        cur.execute("SELECT html, fetched_at FROM cache_pages WHERE url = ?", (url,))
        row = cur.fetchone()
        con.close()
        if not row:
            return None, None
        html, ts = row
        return html, ts
    except Exception:
        return None, None

def cache_put_page(url: str, html: str, ts: float):
    try:
        con = sqlite3.connect(CACHE_DB)
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO cache_pages (url, html, fetched_at) VALUES (?, ?, ?)",
            (url, html, ts),
        )
        con.commit()
        con.close()
    except Exception:
        pass

def cache_get_robots(host: str):
    try:
        con = sqlite3.connect(CACHE_DB)
        cur = con.cursor()
        cur.execute("SELECT content, fetched_at FROM cache_robots WHERE host = ?", (host,))
        row = cur.fetchone()
        con.close()
        if not row:
            return None, None
        content, ts = row
        return content, ts
    except Exception:
        return None, None

def cache_put_robots(host: str, content: str, ts: float):
    try:
        con = sqlite3.connect(CACHE_DB)
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO cache_robots (host, content, fetched_at) VALUES (?, ?, ?)",
            (host, content, ts),
        )
        con.commit()
        con.close()
    except Exception:
        pass

def cache_get_emails(url: str):
    try:
        con = sqlite3.connect(CACHE_DB)
        cur = con.cursor()
        cur.execute("SELECT emails, phone, fetched_at FROM cache_emails WHERE url = ?", (url,))
        row = cur.fetchone()
        con.close()
        if not row:
            return None, None, None
        emails_json, phone, ts = row
        emails = json.loads(emails_json) if emails_json else []
        return emails, phone, ts
    except Exception:
        return None, None, None

def cache_put_emails(url: str, emails: list, phone: str, ts: float):
    try:
        con = sqlite3.connect(CACHE_DB)
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO cache_emails (url, emails, phone, fetched_at) VALUES (?, ?, ?, ?)",
            (url, json.dumps(emails), phone or "", ts),
        )
        con.commit()
        con.close()
    except Exception:
        pass

init_db()

# ----------------------------
# Network and parsing utils
# ----------------------------
def domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def normalize_url(u: str) -> str:
    try:
        parsed = urlparse(u)
        if not parsed.scheme:
            u = "http://" + u
        return u
    except Exception:
        return u

def safe_get(url: str, timeout: int = 12) -> requests.Response | None:
    # Try cache first
    html, ts = cache_get_page(url)
    now = time.time()
    if html and ts and (now - ts) < CACHE_TTL_SECONDS:
        # Build a fake Response-like object
        class R:
            def __init__(self, text):
                self.text = text
                self.ok = True
        return R(html)

    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        if r and r.ok and r.text:
            cache_put_page(url, r.text, time.time())
        return r
    except Exception:
        return None

def extract_company_name(html: str, url: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for selector in [
            'meta[property="og:site_name"]',
            'meta[property="og:title"]',
            "h1",
            "title",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get("content") or el.get_text()
                text = (text or "").strip()
                if text:
                    return text[:120]
    except Exception:
        pass
    d = domain(url)
    return d.replace("www.", "") if d else ""

def extract_phone(html: str) -> str:
    m = PHONE_REGEX.search(html or "")
    return m.group(0) if m else ""

def find_likely_contact_pages(base_url: str, html: str) -> list[str]:
    pages = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            hlow = href.lower()
            if any(key in hlow for key in ["contact", "contact-us", "about", "team"]):
                pages.add(urljoin(base_url, href))
    except Exception:
        pass
    for suffix in ("/contact", "/contact-us", "/about"):
        pages.add(urljoin(base_url, suffix))
    return list(pages)[:6]

def extract_emails_from_text(text: str) -> list[str]:
    emails = {
        e for e in EMAIL_REGEX.findall(text or "")
        if not e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
    }
    return sorted(emails)

# ----------------------------
# robots.txt handling (cached)
# ----------------------------
def fetch_robots_for_host(host: str, delay_s: float):
    content, ts = cache_get_robots(host)
    now = time.time()
    if content and ts and (now - ts) < CACHE_TTL_SECONDS:
        return content
    try:
        robots_url = f"https://{host}/robots.txt"
        time.sleep(delay_s)
        r = requests.get(robots_url, headers=UA, timeout=12)
        text = r.text if (r and r.ok and r.text) else ""
        cache_put_robots(host, text, time.time())
        return text
    except Exception:
        cache_put_robots(host, "", time.time())
        return ""

def is_allowed_by_robots(url: str, delay_s: float) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc
        content = fetch_robots_for_host(host, delay_s)
        rp = RobotFileParser()
        if content:
            rp.parse(content.splitlines())
        else:
            # No robots found or failed; default allow
            return True
        return rp.can_fetch(UA_STR, url)
    except Exception:
        return True

# ----------------------------
# Email verification
# ----------------------------
def is_valid_email_syntax(email: str) -> bool:
    return EMAIL_REGEX.fullmatch(email or "") is not None

def mx_lookup(domain_name: str) -> bool:
    if dnsresolver is None:
        return False
    try:
        answers = dnsresolver.resolve(domain_name, "MX")
        return len(list(answers)) > 0
    except Exception:
        return False

def verify_email(email: str, do_mx: bool) -> tuple[bool, str]:
    if not is_valid_email_syntax(email):
        return False, ""
    mx_dom = ""
    if do_mx:
        try:
            mx_dom = email.split("@", 1)[1].lower()
            ok = mx_lookup(mx_dom)
            return ok, mx_dom if ok else mx_dom
        except Exception:
            return False, mx_dom
    return True, ""

# ----------------------------
# Crawl with robots + cache + email verify
# ----------------------------
def crawl_for_emails(start_url: str, delay_s: float = 0.7, do_mx: bool = False):
    # Check cache of extracted emails
    emails_cached, phone_cached, ts = cache_get_emails(start_url)
    now = time.time()
    if emails_cached is not None and phone_cached is not None and ts and (now - ts) < CACHE_TTL_SECONDS:
        # Do verification pass here since verification can be toggled dynamically
        verified_emails = []
        mx_domain = ""
        for e in emails_cached:
            ok, mx_dom = verify_email(e, do_mx=do_mx)
            if ok:
                verified_emails.append(e)
                if mx_dom:
                    mx_domain = mx_dom
        return sorted(verified_emails if verified_emails else emails_cached), phone_cached, (len(verified_emails) > 0), mx_domain

    emails = set()
    phone = ""

    # robots check for homepage
    if not is_allowed_by_robots(start_url, delay_s):
        return [], "", False, ""

    r = safe_get(start_url)
    if r and getattr(r, "ok", True):
        html = r.text
        emails |= set(extract_emails_from_text(html))
        phone = extract_phone(html) or phone

        # crawl likely contact pages with robots checks
        for p in find_likely_contact_pages(start_url, html):
            if not is_allowed_by_robots(p, delay_s):
                continue
            time.sleep(delay_s)
            rp = safe_get(p)
            if rp and getattr(rp, "ok", True):
                emails |= set(extract_emails_from_text(rp.text))
                phone = extract_phone(rp.text) or phone

    emails_list = sorted(emails)
    cache_put_emails(start_url, emails_list, phone, time.time())

    # Verify
    verified_emails = []
    mx_domain = ""
    for e in emails_list:
        ok, mx_dom = verify_email(e, do_mx=do_mx)
        if ok:
            verified_emails.append(e)
            if mx_dom:
                mx_domain = mx_dom

    if verified_emails:
        return sorted(verified_emails), phone, True, mx_domain
    return emails_list, phone, False, mx_domain

def looks_like_competitor(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in COMPETITOR_WORDS)

# ----------------------------
# Search engines
# ----------------------------
def ddg_search(q: str, max_results: int = 30, delay_s: float = 0.8) -> list[dict]:
    results = []
    url = "https://html.duckduckgo.com/html/"
    try:
        time.sleep(delay_s)
        r = requests.post(url, data={"q": q}, headers=UA, timeout=15)
        if not r or not r.ok:
            return results
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.result__a"):
            href = a.get("href")
            title = a.get_text(strip=True)
            if href and title:
                results.append({"title": title, "link": href, "engine": "duckduckgo"})
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results

def bing_search(q: str, max_results: int = 30, delay_s: float = 0.8) -> list[dict]:
    results = []
    base = "https://www.bing.com/search"
    params = {"q": q, "count": max(10, min(max_results, 50))}
    try:
        time.sleep(delay_s)
        r = requests.get(base, params=params, headers=UA, timeout=15)
        if not r or not r.ok:
            return results
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("li.b_algo h2 a"):
            href = a.get("href")
            title = a.get_text(strip=True)
            if href and title:
                results.append({"title": title, "link": href, "engine": "bing"})
            if len(results) >= max_results:
                break
        if len(results) < max_results:
            for a in soup.select("a"):
                href = a.get("href")
                title = a.get_text(strip=True)
                if href and title and href.startswith(("http://", "https://")):
                    results.append({"title": title, "link": href, "engine": "bing"})
                if len(results) >= max_results:
                    break
    except Exception:
        pass
    return results

def google_search(q: str, max_results: int = 30, delay_s: float = 0.8) -> list[dict]:
    results = []
    base = "https://www.google.com/search"
    params = {"q": q, "num": max(10, min(max_results, 50)), "hl": "en"}
    try:
        time.sleep(delay_s)
        r = requests.get(base, params=params, headers=UA, timeout=15)
        if not r or not r.ok:
            return results

        if "consent.google.com" in r.url or "consent" in (r.text[:2000].lower()):
            soup_c = BeautifulSoup(r.text, "html.parser")
            form = soup_c.find("form")
            if form and form.get("action"):
                action = urljoin(r.url, form["action"])
                payload = {}
                for inp in form.find_all("input"):
                    n = inp.get("name")
                    v = inp.get("value", "")
                    if n:
                        payload[n] = v
                time.sleep(delay_s)
                r = requests.post(action, data=payload, headers=UA, timeout=15)

        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a"):
            href = a.get("href", "")
            if href.startswith("/url?"):
                qs = parse_qs(urlparse(href).query)
                target = qs.get("q", [""])[0]
                title = a.get_text(strip=True)
                if target.startswith(("http://", "https://")) and title:
                    results.append({"title": title, "link": target, "engine": "google"})
                    if len(results) >= max_results:
                        break
        if len(results) < max_results:
            for a in soup.select("a"):
                href = a.get("href", "")
                title = a.get_text(strip=True)
                if href.startswith(("http://", "https://")) and title:
                    results.append({"title": title, "link": href, "engine": "google"})
                    if len(results) >= max_results:
                        break
    except Exception:
        pass
    return results

def merge_and_dedupe(hits: list[dict], prefer_order=("duckduckgo", "bing", "google")) -> list[dict]:
    seen_key = {}
    ordered = []
    for h in hits:
        try:
            p = urlparse(normalize_url(h["link"]))
            key = (p.scheme, p.netloc.lower(), p.path)
        except Exception:
            continue
        if key not in seen_key:
            seen_key[key] = h
            ordered.append(h)
        else:
            prev = seen_key[key]
            if h["engine"] in prefer_order and prev["engine"] in prefer_order:
                if prefer_order.index(h["engine"]) < prefer_order.index(prev["engine"]):
                    seen_key[key] = h
                    for i, it in enumerate(ordered):
                        pp = urlparse(normalize_url(it["link"]))
                        kk = (pp.scheme, pp.netloc.lower(), pp.path)
                        if kk == key:
                            ordered[i] = h
                            break
    return ordered

# ----------------------------
# Sidebar
# ----------------------------
with st.sidebar:
    st.header("Search Settings")
    default_queries = [
        "General Contractors Miami Dade",
        "Builders Miami Dade",
        "Architects Miami Dade",
        "Construction Companies Broward",
        "Flooring contractors Miami commercial",
        "Renovation contractors Broward",
    ]
    queries = st.text_area(
        "Queries (one per line)",
        value="\n".join(default_queries),
        height=160,
    ).strip().splitlines()

    max_results = st.slider("Max results per engine per query", 5, 50, 25, 5)
    include_flooring_companies = st.checkbox("Include flooring companies in leads", value=True)
    request_delay = st.slider("Polite delay between requests (seconds)", 0.3, 2.0, 0.8, 0.1)
    enable_mx = st.checkbox("Verify emails with MX lookup (needs dnspython)", value=False)

    st.markdown("**Allow domains** (only crawl if matches; optional)")
    allow_domains_text = st.text_area(
        "One per line (e.g. example.com)", value="", height=80
    ).strip()
    st.markdown("**Deny domains** (never crawl if matches)")
    deny_domains_text = st.text_area(
        "One per line (e.g. facebook.com, linkedin.com)", value="facebook.com\nlinkedin.com\nyoutube.com\ntwitter.com\nx.com\ninstagram.com", height=100
    ).strip()

    engines = st.multiselect(
        "Search engines to use",
        options=["Google", "Bing", "DuckDuckGo"],
        default=["Google", "Bing", "DuckDuckGo"],
    )

    st.divider()
    st.header("Email Drafts")
    sender_email = st.text_input("Sender email", value=DEFAULT_SENDER)
    sender_name = st.text_input("Sender name", value="Luis Gonzalez")
    sender_title = st.text_input("Sender title", value="Business Development")
    phone_display = st.text_input("Phone (shown in email)", value="(305) 555-7890")
    website_display = st.text_input("Website (shown in email)", value="https://miamimasterflooring.com")

def parse_domain_rules(text: str) -> list[str]:
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        items.append(line.lower())
    return items

allow_domains = parse_domain_rules(allow_domains_text)
deny_domains = parse_domain_rules(deny_domains_text)

def is_allowed_by_lists(url: str) -> bool:
    host = domain(url)
    if not host:
        return False
    for d in deny_domains:
        if host.endswith(d):
            return False
    if allow_domains:
        return any(host.endswith(d) for d in allow_domains)
    return True

# ----------------------------
# Tabs
# ----------------------------
tab1, tab2 = st.tabs(["🔎 Find Leads", "✉️ Build Email Drafts"])

with tab1:
    st.subheader("Search & Scrape")
    st.write("Searches Google, Bing, and DuckDuckGo, respects robots.txt, applies allow/deny lists, caches pages, and verifies emails (syntax + optional MX).")
    colA, colB = st.columns([1, 1])
    with colA:
        run = st.button("Run Search", type="primary")
    with colB:
        clear = st.button("Clear Leads")

    if clear:
        st.session_state.leads_df = st.session_state.leads_df.iloc[0:0]
        st.success("Leads cleared.")

    if run:
        all_hits = []
        progress = st.progress(0.0)
        total_q = max(1, len(queries))
        for i, q in enumerate(queries, 1):
            local_hits = []
            if "DuckDuckGo" in engines:
                local_hits.extend(ddg_search(q, max_results=max_results, delay_s=request_delay))
            if "Bing" in engines:
                local_hits.extend(bing_search(q, max_results=max_results, delay_s=request_delay))
            if "Google" in engines:
                local_hits.extend(google_search(q, max_results=max_results, delay_s=request_delay))

            merged_local = merge_and_dedupe(local_hits)
            all_hits.extend(merged_local)

            progress.progress(i / total_q)
            time.sleep(0.2)

        # Global dedupe
        all_hits = merge_and_dedupe(all_hits)

        # Apply allow/deny lists
        filtered_hits = [h for h in all_hits if is_allowed_by_lists(h["link"])]

        st.info(f"Found {len(filtered_hits)} unique result links after filters. Crawling sites for emails...")
        crawl_prog = st.progress(0.0)
        new_rows = []
        total_hits = max(1, len(filtered_hits))
        for idx, hit in enumerate(filtered_hits, 1):
            url = normalize_url(hit["link"])
            if not url.startswith(("http://", "https://")):
                crawl_prog.progress(idx / total_hits)
                continue

            # Quick robots check before any fetch
            if not is_allowed_by_robots(url, request_delay):
                crawl_prog.progress(idx / total_hits)
                continue

            r = safe_get(url)
            if not (r and getattr(r, "text", None)):
                crawl_prog.progress(idx / total_hits)
                continue

            name = extract_company_name(r.text, url)
            if (not include_flooring_companies) and looks_like_competitor(name):
                crawl_prog.progress(idx / total_hits)
                continue

            emails, phone, any_verified, mx_domain = crawl_for_emails(url, delay_s=request_delay, do_mx=enable_mx)
            if not emails:
                crawl_prog.progress(idx / total_hits)
                continue

            # Prefer first verified email if we have any
            selected_email = emails[0]

            new_rows.append(
                {
                    "Company": name,
                    "Email": selected_email,
                    "Website": url,
                    "Phone": phone,
                    "Source": hit["engine"],
                    "EmailVerified": "Yes" if any_verified else ("SyntaxOnly" if emails else "No"),
                    "MXDomain": mx_domain,
                }
            )
            crawl_prog.progress(idx / total_hits)

        if new_rows:
            df_new = pd.DataFrame(new_rows)
            merged = pd.concat([st.session_state.leads_df, df_new], ignore_index=True)
            merged.drop_duplicates(subset=["Email", "Website"], keep="first", inplace=True)
            st.session_state.leads_df = merged.reset_index(drop=True)
            st.success(f"Added {len(df_new)} new leads. Total leads: {len(st.session_state.leads_df)}")
        else:
            st.warning("No emails found. Try adjusting queries or filters, or reduce request delay if safe.")

    if not st.session_state.leads_df.empty:
        st.subheader("Leads")
        st.dataframe(st.session_state.leads_df, use_container_width=True, height=380)

        # Download CSV
        csv_bytes = st.session_state.leads_df.to_csv(index=False).encode()
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name="miami_master_flooring_leads.csv",
            mime="text/csv",
        )

with tab2:
    st.subheader("Bulk Email Draft Generator (CSV)")
    st.write("Creates a CSV of ready-to-send draft emails (no provider lock-in).")

    template_default = (
        "Subject: Premium Flooring Support for Your Projects\n\n"
        "Dear {company_or_contact},\n\n"
        "We came across your work while researching active projects in South Florida. "
        "At Miami Master Flooring, we provide fast, reliable flooring installation for multifamily, commercial, and renovation projects in Miami-Dade and Broward.\n\n"
        "Highlights:\n"
        "- SPC and LVP installations\n"
        "- Carpet tile for turns\n"
        "- Tile and baseboard\n"
        "- Fast turnaround and clean job sites\n\n"
        "If you have upcoming units or projects that need flooring, I would be glad to help with pricing and scheduling.\n\n"
        "Best regards,\n"
        "{sender_name}\n"
        "{sender_title}\n"
        "Miami Master Flooring\n"
        "{phone_display}\n"
        "{website_display}\n"
        "From: {sender_email}\n"
        "To: {recipient_email}\n"
        "Unsubscribe: reply STOP\n"
    )

    template_text = st.text_area("Email template (use the placeholders below)", value=template_default, height=280)
    st.caption("Placeholders: {company_or_contact} {sender_name} {sender_title} {phone_display} {website_display} {sender_email} {recipient_email}")

    if st.session_state.leads_df.empty:
        st.warning("No leads yet. Go to the Find Leads tab first.")
    else:
        sel = st.multiselect(
            "Choose companies",
            options=st.session_state.leads_df.index,
            format_func=lambda i: f"{st.session_state.leads_df.loc[i,'Company']} — {st.session_state.leads_df.loc[i,'Email']}",
            default=st.session_state.leads_df.index.tolist(),
        )

        if st.button("Create Draft Emails CSV", type="primary"):
            rows = []
            for i in sel:
                row = st.session_state.leads_df.loc[i]
                filled = template_text.format(
                    company_or_contact=(row["Company"] or "Team"),
                    sender_name=sender_name,
                    sender_title=sender_title,
                    phone_display=phone_display,
                    website_display=website_display,
                    sender_email=sender_email,
                    recipient_email=row["Email"],
                )
                subject = ""
                body = filled
                if filled.lower().startswith("subject:"):
                    first_newline = filled.find("\n")
                    subject = filled[len("Subject:"):first_newline].strip()
                    body = filled[first_newline+1:].lstrip()

                rows.append(
                    {
                        "To": row["Email"],
                        "Company": row["Company"],
                        "Website": row["Website"],
                        "Phone": row["Phone"],
                        "From": sender_email,
                        "Subject": subject,
                        "Body": body,
                    }
                )
            out_df = pd.DataFrame(rows)
            csv_out = out_df.to_csv(index=False).encode()
            st.success(f"Created {len(out_df)} email drafts.")
            st.download_button(
                "Download Draft Emails CSV",
                data=csv_out,
                file_name="mmf_email_drafts.csv",
                mime="text/csv",
            )

st.divider()
note_lines = [
    "Tip: Demo uses polite delays, robots.txt, and a local SQLite cache.",
    "MX check is optional and requires dnspython; install with: pip install dnspython",
    "Respect website terms. For scale and reliability, prefer official search APIs and dedicated enrichment/verification services.",
]
st.caption(" | ".join(note_lines))
