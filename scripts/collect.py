#!/usr/bin/env python3
"""Chakri Finder job collector.

Runs for free on GitHub Actions. Reads scripts/sources.json, fetches every
source, and writes data/jobs.json (jobs + notices + per-source status).
Only the Python standard library is used.
"""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "jobs.json")
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
MAX_AGE_DAYS = 45
UA = "Mozilla/5.0 (compatible; ChakriFinder/1.0; personal job alert)"

GOV_WORDS = [
    "govt", "government", "ministry", "teletalk", "bpsc", "bcs", "directorate",
    "department", "authority", "board", "commission", "corporation", "railway",
    "police", "army", "navy", "air force", "bangladesh bank", "sonali", "janata",
    "agrani", "rupali", "palli bidyut", "pdb", "pgcb", "desco", "dpdc", "nesco",
    "egcb", "npcbl", "wasa", "titas", "petrobangla", "university", "dc office",
    "সরকারি", "মন্ত্রণালয়", "অধিদপ্তর", "কর্তৃপক্ষ", "বোর্ড", "কমিশন", "পুলিশ",
    "সেনাবাহিনী", "নৌবাহিনী", "বিমান বাহিনী", "ব্যাংক", "রেলওয়ে", "বিশ্ববিদ্যালয়",
    "বিসিএস", "পিএসসি", "ডিপিডিসি", "ডেসকো", "পিজিসিবি", "নেসকো", "পিডিবি", "ওয়াসা", "কার্যালয়", "পরিষদ", "বিদ্যুৎ",
]
NOTICE_WORDS = [
    "admit", "exam date", "exam schedule", "result", "viva", "seat plan",
    "written exam", "mcq exam", "প্রবেশপত্র", "পরীক্ষার তারিখ", "পরীক্ষার সময়সূচি",
    "ফলাফল", "ভাইভা", "মৌখিক", "আসন বিন্যাস", "পরীক্ষা স্থগিত",
]
JOB_WORDS = [
    "job", "circular", "recruit", "vacanc", "career", "hiring", "নিয়োগ",
    "বিজ্ঞপ্তি", "চাকরি", "পদে", "নেবে", "জনবল",
]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
BN_MONTHS = {"জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "মার্চ": 3, "এপ্রিল": 4, "মে": 5,
             "জুন": 6, "জুলাই": 7, "আগস্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10,
             "নভেম্বর": 11, "ডিসেম্বর": 12}
BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(text, limit=260):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def iso(d):
    return d.isoformat() if d else ""


def parse_date(value):
    """Return a date from RFC822, ISO or unix values; None when unknown."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).date()
    value = str(value).strip()
    try:
        return email.utils.parsedate_to_datetime(value).date()
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None


def find_deadline(text):
    """Best-effort deadline finder for English and Bangla circular text."""
    t = (text or "").translate(BN_DIGITS)
    low = t.lower()
    pats = [
        r"(?:deadline|last date|closing date|apply before|end date)[^0-9]{0,45}(\d{1,2})(?:st|nd|rd|th)?[\s\-/,]+([a-z]{3,9})[\s\-/,]+(\d{4})",
        r"(?:deadline|last date|closing date|apply before|end date)[^0-9]{0,45}(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{4})",
        r"(?:শেষ তারিখ|শেষ সময়|আবেদনের শেষ)[^0-9]{0,45}(\d{1,2})\s*([^\s\d,]+)\s*,?\s*(\d{4})",
        r"(?:শেষ তারিখ|শেষ সময়|আবেদনের শেষ)[^0-9]{0,45}(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{4})",
    ]
    for p in pats:
        m = re.search(p, low)
        if not m:
            continue
        d, mo, y = m.group(1), m.group(2), m.group(3)
        month = int(mo) if mo.isdigit() else MONTHS.get(mo) or BN_MONTHS.get(mo)
        try:
            return dt.date(int(y), int(month), int(d))
        except Exception:
            continue
    return None


def has_any(text, words):
    low = (text or "").lower()
    return any(w in low for w in words)


def make(title, url, src, cat, org="", loc="", posted=None, deadline=None, desc="", tags=None):
    title = clean(title, 160)
    if not title or not url:
        return None
    return {
        "id": hashlib.md5((url or title).encode("utf-8")).hexdigest()[:12],
        "title": title, "org": clean(org, 80), "cat": cat, "loc": clean(loc, 60),
        "posted": iso(posted), "deadline": iso(deadline), "url": url, "src": src,
        "tags": [clean(str(x), 24) for x in (tags or [])][:8], "desc": clean(desc),
    }


def rss_items(raw):
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        def g(tag):
            el = it.find(tag)
            return el.text if el is not None and el.text else ""
        src_el = it.find("source")
        yield {
            "title": g("title"), "link": g("link").strip(), "date": g("pubDate"),
            "desc": g("description"), "cats": [c.text for c in it.findall("category") if c.text],
            "publisher": src_el.text if src_el is not None and src_el.text else "",
        }


def from_rss(cfg):
    jobs, notices = [], []
    for it in rss_items(fetch(cfg["url"])):
        title, text = it["title"], it["title"] + " " + it["desc"]
        posted = parse_date(it["date"])
        cat = cfg.get("cat", "auto")
        if cat == "auto":
            cat = "gov" if has_any(text + " " + " ".join(it["cats"]), GOV_WORDS) else "private"
        src = cfg["name"] + (" · " + it["publisher"] if it["publisher"] else "")
        org = it["publisher"] if cat == "remote" else ""
        if cat == "remote" and ":" in title:           # "Company: Role" style feeds
            org, title = [x.strip() for x in title.split(":", 1)]
        rec = make(title, it["link"], src, cat, org=org, posted=posted,
                   deadline=find_deadline(text), desc=it["desc"], tags=it["cats"])
        if not rec:
            continue
        if cat != "remote" and has_any(title, NOTICE_WORDS):
            notices.append(rec)
        elif cat == "remote" or has_any(text, JOB_WORDS):
            jobs.append(rec)
    return jobs, notices


def from_google_news(cfg):
    q = urllib.parse.quote(cfg["query"] + " when:14d")
    hl = cfg.get("hl", "bn")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl=BD&ceid=BD:{hl}"
    return from_rss({"name": cfg["name"], "url": url, "cat": cfg.get("cat", "auto")})


def remotive():
    data = json.loads(fetch("https://remotive.com/api/remote-jobs"))
    for j in data.get("jobs", [])[:150]:
        yield make(j.get("title"), j.get("url"), "Remotive", "remote", org=j.get("company_name"),
                   loc=j.get("candidate_required_location"), posted=parse_date(j.get("publication_date")),
                   desc=j.get("description"), tags=[j.get("category"), j.get("job_type")] + (j.get("tags") or [])[:5])


def arbeitnow():
    data = json.loads(fetch("https://www.arbeitnow.com/api/job-board-api"))
    for j in data.get("data", []):
        if not j.get("remote"):
            continue
        yield make(j.get("title"), j.get("url"), "Arbeitnow", "remote", org=j.get("company_name"),
                   loc=j.get("location"), posted=parse_date(j.get("created_at")),
                   desc=j.get("description"), tags=j.get("tags") or [])


def jobicy():
    data = json.loads(fetch("https://jobicy.com/api/v2/remote-jobs?count=50"))
    for j in data.get("jobs", []):
        ind = j.get("jobIndustry") or []
        yield make(j.get("jobTitle"), j.get("url"), "Jobicy", "remote", org=j.get("companyName"),
                   loc=j.get("jobGeo"), posted=parse_date(j.get("pubDate")),
                   desc=j.get("jobExcerpt"), tags=ind if isinstance(ind, list) else [ind])


def remoteok():
    data = json.loads(fetch("https://remoteok.com/api"))
    for j in data[1:120]:
        if not isinstance(j, dict):
            continue
        yield make(j.get("position"), j.get("url"), "Remote OK", "remote", org=j.get("company"),
                   loc=j.get("location") or "Worldwide", posted=parse_date(j.get("date")),
                   desc=j.get("description"), tags=j.get("tags") or [])


def himalayas():
    data = json.loads(fetch("https://himalayas.app/jobs/api?limit=50"))
    for j in data.get("jobs", []):
        loc = j.get("locationRestrictions") or []
        yield make(j.get("title"), j.get("applicationLink") or j.get("guid"), "Himalayas", "remote",
                   org=j.get("companyName"), loc=", ".join(loc[:3]) if loc else "Worldwide",
                   posted=parse_date(j.get("pubDate")), desc=j.get("excerpt"),
                   tags=(j.get("categories") or [])[:5])


REMOTE = {"remotive": remotive, "arbeitnow": arbeitnow, "jobicy": jobicy,
          "remoteok": remoteok, "himalayas": himalayas}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def keep(rec):
    """Only keep things that can still be applied for."""
    if rec["deadline"] and rec["deadline"] < iso(TODAY):
        return False
    if rec["posted"]:
        age = (TODAY - dt.date.fromisoformat(rec["posted"])).days
        if age > MAX_AGE_DAYS and not rec["deadline"]:
            return False
    return True


def main():
    with open(os.path.join(ROOT, "scripts", "sources.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    jobs, notices, status = [], [], []

    def run(name, fn):
        try:
            j, n = fn()
            j, n = [x for x in j if x], [x for x in n if x]
            jobs.extend(j)
            notices.extend(n)
            status.append({"source": name, "ok": True, "jobs": len(j), "notices": len(n)})
        except Exception as e:  # one broken source must never stop the rest
            status.append({"source": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
        print(status[-1], file=sys.stderr)

    for c in cfg.get("rss", []):
        run(c["name"], lambda c=c: from_rss(c))
    for c in cfg.get("google_news", []):
        run(c["name"], lambda c=c: from_google_news(c))
    for name in cfg.get("remote_apis", []):
        if name in REMOTE:
            run(name, lambda name=name: (list(REMOTE[name]()), []))

    # keep previous results for sources that failed this time
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}
    failed = {s["source"] for s in status if not s["ok"]}
    if failed:
        for key, bucket in (("jobs", jobs), ("notices", notices)):
            for rec in old.get(key, []):
                if any(norm(rec.get("src", "")).startswith(norm(f)) for f in failed) and not rec.get("demo"):
                    bucket.append(rec)

    def dedupe(items):
        seen, out = set(), []
        for r in items:
            key = re.sub(r"\W+", "", r["title"].lower())[:70]
            if r["id"] in seen or key in seen:
                continue
            seen.update([r["id"], key])
            out.append(r)
        return out

    jobs = [r for r in dedupe(jobs) if keep(r)]
    notices = [r for r in dedupe(notices) if keep(r)]
    jobs.sort(key=lambda r: r["posted"], reverse=True)
    notices.sort(key=lambda r: r["posted"], reverse=True)

    if not jobs and old.get("jobs"):
        print("Nothing collected; keeping the previous file.", file=sys.stderr)
        old["status"] = status
        old["checked"] = NOW.isoformat(timespec="seconds")
        out = old
    else:
        out = {"updated": NOW.isoformat(timespec="seconds"), "checked": NOW.isoformat(timespec="seconds"),
               "jobs": jobs[:900], "notices": notices[:200], "status": status}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Saved {len(out.get('jobs', []))} jobs, {len(out.get('notices', []))} notices", file=sys.stderr)


if __name__ == "__main__":
    main()
