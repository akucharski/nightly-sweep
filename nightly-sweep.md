# Nightly site sweep

Find pages on a public website that have quietly stopped being true, and report
them to a human. Change nothing.

## Claws

| Claw | State | Why |
| --- | --- | --- |
| Read | open | Public pages only, over HTTP, as any visitor |
| Write | **closed** | This agent cannot alter a single character of the site |
| Browse | open | Fetching and parsing pages is the whole job |
| Shell | **closed** | Nothing here needs it |
| Schedule | open | Runs nightly at 03:00, unattended |

Running with write access denied is not caution. It is the reason this is
approvable without a procurement, a security review, or a meeting.

## Steps

1. Read `/robots.txt` and honour it. Identify the crawler by name with a
   working contact address in the User-Agent.
2. Read `/sitemap.xml`, following sitemap indexes. Keep each URL's `lastmod` —
   it is the cheapest staleness signal available and most sites publish it.
3. Crawl same-origin HTML pages from the homepage, for anything the sitemap
   missed. Skip binaries. Pause between requests.
4. For each page record: HTTP status, title, last-modified, internal and
   external links, `mailto:` addresses, forms and their actions, and any
   schema.org Event with an `endDate`.
5. Check every distinct link target. HEAD first, GET on failure.
6. Flag a page if any of these is true: not modified in 18+ months, contains a
   broken link, publishes an event whose end date has passed, or carries a form
   on a stale page.
7. Send **only the flagged pages** to a model and ask one question: is this page
   still doing its job, or does it describe something that has ended? Require a
   verbatim quote as evidence. Accept "unclear" as an answer.
8. Email the digest. Lead with what needs a human decision, then the mechanical
   fixes. State plainly at the bottom that nothing was changed.

## What the model is and is not for

The crawler knows a page has not been edited since 2019. It does not know
whether that matters.

- The city charter has not changed since 2019, and should not have. **Live.**
- A grant page reading "applications close March 15, 2021" for a programme
  funded through 2021. **Dead.**

Both are 2019 pages with forms on them. Only one is a problem, and no date
comparison will ever tell you which. Stale is arithmetic. Dead is judgement.
That is the only reason a model is in this pipeline at all, and it is why it
sees a few percent of the site rather than all of it.

## Rules

- Never guess that something is dead because it is old. Age is not evidence.
- Quote the page verbatim as evidence. A reason without a quote is a guess.
- "Unclear" is a successful outcome. A wrong "dead" costs the web team more
  than an honest "I can't tell".
- Deleting a public page is a records decision. This agent never proposes a
  deletion, only a review.

## Schedule

```cron
0 3 * * *  cd /opt/nightly-sweep && ./run.sh >> /var/log/nightly-sweep.log 2>&1
```
