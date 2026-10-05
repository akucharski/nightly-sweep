"""
Second-pass analysis on crawled pages: outdated wording and contradictions.

Outdated wording is deterministic. A page can have a fresh timestamp and still
say "applications close March 15, 2023" or "join us on June 4, 2022". Those are
past dates written as if they were still ahead, and a regex finds them for free.

Contradictions need a model. Comparing every page with every other is neither
affordable nor useful, so pages are first grouped by what they talk about
(TF-IDF similarity, no dependencies) and each small group is compared side by
side. Every quote the model returns is checked against the page text, and any
claim that cannot be found verbatim is discarded.
"""

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# ---------- sections ----------


def section_of(url):
    """The first path segment, which is how most sites are organised."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return "(home)"
    if len(parts) == 1 and "." in parts[0]:
        return "(top level)"
    return parts[0].lower()


# ---------- outdated wording ----------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
DATE_RES = [
    (re.compile(MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + MONTH_RE + r",?\s+(\d{4})\b", re.I), "dmy"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "us"),
]
# Words that present a date as still ahead of the reader.
FORWARD_RE = re.compile(
    r"\b(upcoming|will be held|will take place|will be|will open|will close|deadline|"
    r"due (?:by|on)|register|registration|apply (?:by|before|now|today)|"
    r"applications? (?:close|closes|are due|due|open|opens)|closes?|join us|save the date|"
    r"coming soon|starts?|starting|begins?|until|no later than|submit (?:by|before)|rsvp)\b",
    re.I)


def _to_date(kind, g):
    try:
        if kind == "mdy":
            return datetime(int(g[2]), MONTHS[g[0][:3].lower()], int(g[1]), tzinfo=timezone.utc)
        if kind == "dmy":
            return datetime(int(g[2]), MONTHS[g[1][:3].lower()], int(g[0]), tzinfo=timezone.utc)
        if kind == "iso":
            return datetime(int(g[0]), int(g[1]), int(g[2]), tzinfo=timezone.utc)
        return datetime(int(g[2]), int(g[0]), int(g[1]), tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None


def outdated_mentions(text, today, limit=5):
    """Past dates that the surrounding words still present as upcoming."""
    out, seen = [], set()
    for rx, kind in DATE_RES:
        for m in rx.finditer(text):
            when = _to_date(kind, m.groups())
            if not when or when.year < 1995 or when >= today - timedelta(days=1):
                continue
            before = text[max(0, m.start() - 90):m.start()]
            after = text[m.end():m.end() + 30]
            cue = FORWARD_RE.search(before) or FORWARD_RE.search(after)
            if not cue or m.group(0) in seen:
                continue
            seen.add(m.group(0))
            start, end = max(0, m.start() - 90), min(len(text), m.end() + 30)
            if start:                                   # don't open or close mid-word
                start = text.find(" ", start, m.start()) + 1 or start
            if end < len(text):
                end = text.rfind(" ", m.end(), end) if text.rfind(" ", m.end(), end) > 0 else end
            quote = text[start:end].strip()
            out.append({
                "date": when.date().isoformat(),
                "match": m.group(0),
                "cue": cue.group(0).lower(),
                "quote": ("…" if start else "") + quote + ("…" if end < len(text) else ""),
            })
            if len(out) >= limit:
                return out
    return out


# ---------- grouping related pages ----------

STOP = set("""
the and for are with this that from your you our will have has not but all any can was were
been their they them its also more may per each who what when where which into than then there
these those about after before other such only over under some very just most must should would
could upon here how out new one two get use used using see page site please click home contact
""".split())


def _tokens(text):
    return [w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", text.lower()) if w not in STOP]


def related_groups(pages, max_groups=12, group_size=4, min_sim=0.18):
    """Group pages that talk about the same things, most similar first.

    pages: [{url, title, text, ...}]. Returns [{topic, pages}] with at most
    max_groups groups of 2..group_size pages each. A page joins one group only.
    """
    docs = [p for p in pages if len(p.get("text", "").split()) >= 40]
    n = len(docs)
    if n < 2:
        return []
    counts = [Counter(_tokens(((p.get("title") or "") + " ") * 2 + p["text"])) for p in docs]
    df = Counter()
    for c in counts:
        df.update(c.keys())

    # Terms on half the site are boilerplate; terms on one page can't link two.
    vecs = []
    for c in counts:
        v = {t: (1 + math.log(f)) * math.log(n / df[t])
             for t, f in c.items() if 1 < df[t] <= max(2, n * 0.5)}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})

    top = [sorted(v, key=v.get, reverse=True)[:20] for v in vecs]
    index = defaultdict(list)
    for i, terms in enumerate(top):
        for t in terms:
            index[t].append(i)
    candidates = set()
    for ids in index.values():
        if len(ids) > 60:
            continue
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                candidates.add((ids[a], ids[b]))

    pairs = []
    for i, j in candidates:
        a, b = vecs[i], vecs[j]
        if len(a) > len(b):
            a, b = b, a
        sim = sum(x * b.get(t, 0.0) for t, x in a.items())
        if sim >= min_sim:
            pairs.append((sim, i, j))
    pairs.sort(reverse=True)

    group_of, groups = {}, []
    for _, i, j in pairs:
        gi, gj = group_of.get(i), group_of.get(j)
        if gi is None and gj is None:
            if len(groups) < max_groups:
                groups.append([i, j])
                group_of[i] = group_of[j] = len(groups) - 1
        elif gj is None and len(groups[gi]) < group_size:
            groups[gi].append(j)
            group_of[j] = gi
        elif gi is None and len(groups[gj]) < group_size:
            groups[gj].append(i)
            group_of[i] = gj

    out = []
    for g in groups:
        weight = Counter()
        for k in g:
            for t in top[k]:
                weight[t] += vecs[k][t]
        shared = [t for t, _ in weight.most_common() if sum(t in vecs[k] for k in g) > 1][:3]
        out.append({"topic": ", ".join(shared) or "related pages", "pages": [docs[k] for k in g]})
    return out


# ---------- contradictions ----------

CONTRADICTION_SYSTEM = """You check pages from one website for statements that contradict each other.

You get a small group of related pages. Find places where the site states two incompatible things \
about the same subject: a different fee, deadline, date, time, phone number, address, eligibility \
rule, requirement, or named official for the same thing. Check across the pages and within each page.

Return only a JSON array, one object per contradiction, or [] if there are none:

  {"topic": "<what the conflict is about, at most 8 words>",
   "severity": "high" | "medium" | "low",
   "page_a": "<url>", "quote_a": "<exact text from page_a>",
   "page_b": "<url>", "quote_b": "<exact text from page_b>",
   "explanation": "<one sentence, at most 30 words>"}

How to judge:
- Copy both quotes verbatim from the supplied text, short enough to point at the claim. A quote that \
is not in the text will be thrown away.
- page_a and page_b may be the same url when a page contradicts itself.
- It is a contradiction only if both statements cannot be true at once for the same thing. Different \
programmes, years, locations or audiences are not contradictions, and an old date on its own is not one.
- Severity: high when a resident acting on the wrong statement could pay the wrong amount, miss a \
deadline, go to the wrong place or call the wrong number; medium when it is confusing but low \
consequence; low when it is cosmetic.
- A false alarm costs a web team an hour. Leave out anything you are unsure of."""


def contradiction_prompt(group, today):
    lines = [f"Today is {today}. These {len(group['pages'])} pages look related "
             f"(shared terms: {group['topic']}).\n"]
    for i, p in enumerate(group["pages"], 1):
        lines.append(f"--- PAGE {i} ---")
        lines.append(f"url: {p['url']}")
        lines.append(f"title: {p.get('title') or '(none)'}")
        lines.append(f"last modified: {p.get('last_modified') or 'unknown'}")
        lines.append(f"text: {p['text']}\n")
    return "\n".join(lines)


def _norm(s):
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower().strip("…. ")


def verify_contradictions(raw, group):
    """Keep only claims whose pages are in the group and whose quotes are really there."""
    by_url = {p["url"]: p for p in group["pages"]}
    texts = {u: _norm(p["text"]) for u, p in by_url.items()}
    kept, dropped = [], 0
    for c in raw:
        if not isinstance(c, dict):
            continue
        a, b = c.get("page_a"), c.get("page_b")
        qa, qb = _norm(c.get("quote_a")), _norm(c.get("quote_b"))
        if (a not in texts or b not in texts or len(qa) < 4 or len(qb) < 4
                or qa not in texts[a] or qb not in texts[b] or (a == b and qa == qb)):
            dropped += 1
            continue
        sev = c.get("severity") if c.get("severity") in ("high", "medium", "low") else "medium"
        kept.append({
            "topic": str(c.get("topic") or "")[:80],
            "severity": sev,
            "page_a": a, "title_a": by_url[a].get("title") or "", "quote_a": str(c["quote_a"]).strip(),
            "page_b": b, "title_b": by_url[b].get("title") or "", "quote_b": str(c["quote_b"]).strip(),
            "explanation": str(c.get("explanation") or "")[:300],
            "group": group["topic"],
        })
    return kept, dropped
