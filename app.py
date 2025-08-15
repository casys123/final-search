# app.py
import re
import time
import base64
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# ----------------------------
# Streamlit page setup
# ----------------------------
st.set_page_config(page_title="Miami Master Flooring Lead Finder", layout="wide")
st.title("🚀 Miami Master Flooring — Lead Finder & Email Drafts")

DEFAULT_SENDER = "info@miamimasterflooring.com"

# Session state
if "leads_df" not in st.session_state:
    st.session_state.leads_df = pd.DataFrame(
        columns=["Company", "Email", "Website", "Phone", "Source"]
    )

# ----------------------------
# Helpers
# ----------------------------
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
PHONE_REGEX = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)

COMPETITOR_WORDS = {"floor", "tile", "carpet"}  # used if you want to exclude flooring companies

def domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def safe_get(url: str, timeout: int = 12) -> requests.Response | None:
    try:
        return requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
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
    # fallback to domain
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
            if any(key in href.lower() for key in ["contact", "contact-us", "about", "team"]):
                pages.add(urljoin(base_url, href))
    except Exception:
        pass
    # common fallbacks
    for suffix in ("/contact", "/contact-us", "/about"):
        pages.add(urljoin(base_url, suffix))
    return list(pages)[:6]

def extract_emails_from_text(text: str) -> list[str]:
    # filter out obvious image "emails"
    emails = {e for e in EMAIL_REGEX.findall(text or "") if not e.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))}
    return sorted(emails)

def crawl_for_emails(start_url: str) -> tuple[list[str], str]:
    """Fetch homepage + likely contact pages and return emails + phone."""
    emails = set()
    phone = ""
    seen = set()

    # homepage
    r = safe_get(start_url)
    if r and r.ok:
        html = r.text
        emails |= set(extract_emails_from_text(html))
        phone = extract_phone(html) or phone
        for p in find_likely_contact_pages(start_url, html):
            if p in seen:
                continue
            seen.add(p)
            time.sleep(0.7)  # be gentle
            rp = safe_get(p)
            if rp and rp.ok:
                emails |= set(extract_emails_from_text(rp.text))
                phone = extract_phone(rp.text) or phone
    return sorted(emails), phone

def duckduckgo_search(q: str, max_results: int = 30) -> list[dict]:
    """
    Simple HTML search via DuckDuckGo results (non-JS). Educational/demo only.
    """
    results = []
    url = "https://html.duckduckgo.com/html/"
    try:
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

def normalize_url(u: str) -> str:
    try:
        parsed = urlparse(u)
        if not parsed.scheme:
            u = "http://" + u
        return u
    except Exception:
        return u

def looks_like_competitor(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in COMPETITOR_WORDS)

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

    max_results = st.slider("Max results per query", 5, 50, 25, 5)
    include_flooring_companies = st.checkbox("Include flooring companies in leads", value=True)

    st.divider()
    st.header("Email Drafts")
    sender_email = st.text_input("Sender email", value=DEFAULT_SENDER)
    sender_name = st.text_input("Sender name", value="Luis Gonzalez")
    sender_title = st.text_input("Sender title", value="Business Development")
    phone_display = st.text_input("Phone (shown in email)", value="(305) 555-7890")
    website_display = st.text_input("Website (shown in email)", value="https://miamimasterflooring.com")

# ----------------------------
# Tabs
# ----------------------------
tab1, tab2 = st.tabs(["🔎 Find Leads", "✉️ Build Email Drafts"])

with tab1:
    st.subheader("Search & Scrape")
    st.write("This finds company websites from DuckDuckGo and crawls for emails (homepage + likely contact pages).")
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
            hits = duckduckgo_search(q, max_results=max_results)
            all_hits.extend(hits)
            progress.progress(i / total_q)
            time.sleep(0.4)

        st.info(f"Found {len(all_hits)} result links. Crawling sites for emails…")
        crawl_prog = st.progress(0.0)
        new_rows = []
        for idx, hit in enumerate(all_hits, 1):
            url = normalize_url(hit["link"])
            # fetch the page quickly to get name; skip non-http(s)
            if not url.startswith(("http://", "https://")):
                crawl_prog.progress(idx / max(1, len(all_hits)))
                continue

            r = safe_get(url)
            if not (r and r.ok and r.text):
                crawl_prog.progress(idx / max(1, len(all_hits)))
                continue

            name = extract_company_name(r.text, url)
            if (not include_flooring_companies) and looks_like_competitor(name):
                crawl_prog.progress(idx / max(1, len(all_hits)))
                continue

            emails, phone = crawl_for_emails(url)
            if not emails:
                crawl_prog.progress(idx / max(1, len(all_hits)))
                continue

            new_rows.append(
                {
                    "Company": name,
                    "Email": emails[0],
                    "Website": url,
                    "Phone": phone,
                    "Source": hit["engine"],
                }
            )
            crawl_prog.progress(idx / max(1, len(all_hits)))

        if new_rows:
            df_new = pd.DataFrame(new_rows)
            # Deduplicate by (Email, Website)
            merged = pd.concat([st.session_state.leads_df, df_new], ignore_index=True)
            merged.drop_duplicates(subset=["Email", "Website"], keep="first", inplace=True)
            st.session_state.leads_df = merged.reset_index(drop=True)
            st.success(f"Added {len(df_new)} new leads. Total leads: {len(st.session_state.leads_df)}")
        else:
            st.warning("No emails found. Try adjusting queries.")

    if not st.session_state.leads_df.empty:
        st.subheader("Leads")
        st.dataframe(st.session_state.leads_df, use_container_width=True, height=360)

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

    # Email template (plain text to avoid unicode surprises)
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
                # split subject from body for convenience
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
st.caption(
    "Tip: This demo uses DuckDuckGo HTML results and polite crawling. "
    "Use responsibly and follow website terms. For production, consider search APIs and email verification."
)
