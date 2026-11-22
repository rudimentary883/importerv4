"""
Judge availability per round  <-  the judges' availability form (Google Forms .xlsx / .csv)      (page /availability)

The form asks every judge about all rounds in one question list ("Round-by-Round Availability [Round 1]" ... "[Round 5]").
Tabbycat keeps availability per round, so this page reads the sheet and marks the judges available / unavailable in each
round through the API:   /api/v1/tournaments/<slug>/rounds/<seq>/availabilities
    GET  ?adjudicators=true   the judges who are available now   (without a filter the list comes back empty)
    PUT  [urls]               mark these objects as available
    POST [urls]               mark these objects as unavailable
Only JUDGE urls are ever sent, so teams and rooms are never touched.  The preview (default) only reads.

Matching a form row to a judge on the tab:  1. same email   2. same name (full name or name preference)   3. near-identical name
(e.g. the tab has "Rafael Clet", the form "Rafael Carlos Clet").  A person who answered twice: the LATEST answer counts.

Needs the 2 lines at the bottom of app.py:
    from availability_import import register_availability
    register_availability(app)
"""
import csv
import difflib
import io
import re
import time
import unicodedata
from urllib.parse import urlsplit

import requests

UA = "Mozilla/5.0 (compatible; TabbycatAvailabilityImporter/1.0; Render)"


# ------------------------------------------------------------------ small helpers
def _n(text):
    """NFKC (fancy unicode letters -> plain), lower, single spaces."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip().casefold()


def _loose(text):
    text = re.sub(r"\([^)]*\)", "", _n(text))
    return re.sub(r"[^0-9a-z\u00c0-\u024f]+", "", text)


def _id_of(ref):
    text = (ref.get("url") if isinstance(ref, dict) else str(ref or "")).rstrip("/")
    return text.rsplit("/", 1)[-1]


def _request(session, method, url, body=None, retries=3):
    for attempt in range(retries):
        try:
            resp = session.request(method, url, json=body, timeout=40)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"Could not reach {url}: {e}")
            time.sleep(1)
            continue
        if resp.status_code in (200, 201, 204):
            try:
                return resp.json() if resp.text else []
            except ValueError:
                return []
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise RuntimeError(f"HTTP {resp.status_code} on {method} {url}: the token was refused - it must be an ADMIN account's token.")
        raise RuntimeError(f"HTTP {resp.status_code} on {method} {url}: {resp.text[:300]}")
    raise RuntimeError(f"Rate limited on {url}")


def _list(session, url):
    data = _request(session, "GET", url)
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items = list(data["results"])
        while data.get("next"):
            data = _request(session, "GET", data["next"])
            items += data.get("results", [])
        return items
    return data if isinstance(data, list) else []


def guess_slug(url):
    parts = [p for p in urlsplit(url.strip()).path.split("/") if p]
    return parts[0] if parts and parts[0] not in ("_", "api", "admin") else ""


# ------------------------------------------------------------------ the sheet
def read_table(filename, raw):
    """-> list of rows (lists of cell values) from .xlsx or .csv"""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True).active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    text = raw.decode("utf-8-sig", errors="ignore")
    return [r for r in csv.reader(io.StringIO(text))]


def classify(value):
    """'yes' | 'no' | 'maybe' | None (blank / unknown)"""
    t = _n(value)
    if not t:
        return None
    if t.startswith(("maybe", "subject", "tentative", "not sure", "unsure")):
        return "maybe"
    if t.startswith(("not ", "no", "unavailable", "cannot", "can't", "cant")):
        return "no"
    if t.startswith(("available", "yes", "y", "free", "can ")):
        return "yes"
    return None


def parse_sheet(filename, raw):
    rows = read_table(filename, raw)
    if not rows:
        raise ValueError("The file is empty.")
    head = [_n(h) for h in rows[0]]
    def find(*words, exclude=()):
        for i, h in enumerate(head):
            if all(w in h for w in words) and not any(x in h for x in exclude):
                return i
        return None
    c_email = find("email")
    c_full = find("full name")
    c_pref = find("name preference") if find("name preference") is not None else find("preferred")
    c_time = find("timestamp")
    c_inst = find("institution")
    rounds = {}
    for i, h in enumerate(head):
        m = re.search(r"round\s*(\d+)", h)
        if m and ("availab" in h or "round-by-round" in h):
            rounds[int(m.group(1))] = i
    if not rounds:
        raise ValueError("No round columns were found. The column headers must look like 'Round-by-Round Availability [Round 1]'.")
    if c_email is None and c_full is None and c_pref is None:
        raise ValueError("No email / name columns were found (expected 'Email Address', 'Full Name', 'Name Preference').")
    people, dup = {}, []
    for r in rows[1:]:
        cell = lambda c: (r[c] if c is not None and c < len(r) and r[c] is not None else "")
        full, pref, email = str(cell(c_full)).strip(), str(cell(c_pref)).strip(), _n(cell(c_email))
        if not (full or pref or email):
            continue
        answers = {rd: classify(cell(c)) for rd, c in rounds.items()}
        stamp = str(cell(c_time))
        key = email or _n(full or pref)
        person = {"full": full, "pref": pref, "email": email, "inst": str(cell(c_inst)).strip(), "answers": answers, "stamp": stamp}
        if key in people:
            dup.append(full or pref or email)
            if stamp >= people[key]["stamp"]:
                people[key] = person
        else:
            people[key] = person
    return list(people.values()), sorted(rounds), dup


# ------------------------------------------------------------------ matching
def match(people, judges):
    """-> (pairs [(person, judge, how)], unmatched people, judges with no form answer)"""
    used_j, pairs, left = set(), [], []
    by_email = {}
    for j in judges:
        if j["email"]:
            by_email.setdefault(j["email"], j)
    rest = []
    for p in people:                                                  # 1. email
        j = by_email.get(p["email"]) if p["email"] else None
        if j and j["id"] not in used_j:
            used_j.add(j["id"]); pairs.append((p, j, "email"))
        else:
            rest.append(p)
    rest2 = []
    for p in rest:                                                    # 2. exact name
        names = {_n(p["full"]), _n(p["pref"])} - {""}
        j = next((j for j in judges if j["id"] not in used_j and _n(j["name"]) in names), None)
        if j:
            used_j.add(j["id"]); pairs.append((p, j, "name"))
        else:
            rest2.append(p)
    scored = []                                                       # 3. near-identical name
    for pi, p in enumerate(rest2):
        for j in judges:
            if j["id"] in used_j:
                continue
            best = 0.0
            for nm in (p["full"], p["pref"]):
                if not nm:
                    continue
                a, b = _loose(nm), _loose(j["name"])
                if a and b:
                    best = max(best, difflib.SequenceMatcher(None, a, b).ratio())
                ta, tb = set(_n(nm).split()), set(_n(j["name"]).split())
                if len(tb) >= 2 and tb <= ta:
                    best = max(best, 0.9)
            if best >= 0.85:
                scored.append((best, pi, j["id"]))
    taken_p = set()
    for score, pi, jid in sorted(scored, reverse=True):
        if pi in taken_p or jid in used_j:
            continue
        taken_p.add(pi); used_j.add(jid)
        pairs.append((rest2[pi], next(j for j in judges if j["id"] == jid), f"similar name ({score:.0%})"))
    left = [p for pi, p in enumerate(rest2) if pi not in taken_p]
    unanswered = [j for j in judges if j["id"] not in used_j]
    return pairs, left, unanswered


# ------------------------------------------------------------------ the plan and the writes
def build_plan(url, token, slug, people, sheet_rounds, maybe="no", apply=False, session=None):
    s = session or requests.Session()
    s.headers.update({"Authorization": f"Token {token.strip()}", "Accept": "application/json",
                      "Content-Type": "application/json", "User-Agent": UA})
    sp = urlsplit(url.strip())
    origin = f"{sp.scheme or 'https'}://{sp.netloc or sp.path.split('/')[0]}"
    slug = (slug or guess_slug(url)).strip()
    slugs = [t.get("slug") for t in _list(s, f"{origin}/api/v1/tournaments") if isinstance(t, dict)]
    if slug not in slugs:
        if len(slugs) == 1:
            slug = slugs[0]
        else:
            raise ValueError("Which tournament? Add its slug. Found: " + ", ".join(map(str, slugs)))
    base = f"{origin}/api/v1/tournaments/{slug}"
    judges = []
    for a in _list(s, base + "/adjudicators"):
        jid = _id_of(a)
        judges.append({"id": jid, "url": (a.get("url") or f"{base}/adjudicators/{jid}").rstrip("/"),
                       "name": (a.get("name") or "").strip(), "email": _n(a.get("email"))})
    seqs = {r.get("seq") for r in _list(s, base + "/rounds") if isinstance(r, dict)}
    pairs, unmatched, unanswered = match(people, judges)
    plan = {"slug": slug, "pairs": pairs, "unmatched": unmatched, "unanswered": unanswered, "rounds": [], "skipped_rounds": [],
            "n_judges": len(judges)}
    name_of = {j["url"]: j["name"] for j in judges}
    for seq in sheet_rounds:
        if seqs and seq not in seqs:
            plan["skipped_rounds"].append(seq)
            continue
        want_yes, want_no, undecided = [], [], []
        for p, j, how in pairs:
            ans = p["answers"].get(seq)
            if ans == "maybe":
                ans = {"no": "no", "yes": "yes"}.get(maybe)
            if ans == "yes":
                want_yes.append(j["url"])
            elif ans == "no":
                want_no.append(j["url"])
            else:
                undecided.append(j["url"])
        cur_raw = _list(s, f"{base}/rounds/{seq}/availabilities?adjudicators=true")
        current = {str(u.get("url") if isinstance(u, dict) else u).rstrip("/") for u in cur_raw}
        to_add = [u for u in want_yes if u not in current]
        to_remove = [u for u in want_no if u in current]
        final = sorted((current | set(want_yes)) - set(want_no))
        row = {"seq": seq, "available_now": len(current), "to_add": to_add, "to_remove": to_remove, "already": len(want_yes) - len(to_add),
               "undecided": len(undecided), "final": len(final), "name_of": name_of, "result": "preview", "problem": ""}
        if apply and (to_add or to_remove):
            if final:                                              # PUT = mark as available (the full final list is sent, so it is safe
                _request(s, "PUT", f"{base}/rounds/{seq}/availabilities", final)   # whether PUT adds or replaces the judges' list)
            if to_remove:
                _request(s, "POST", f"{base}/rounds/{seq}/availabilities", to_remove)       # POST = mark as unavailable
            after = {str(u.get("url") if isinstance(u, dict) else u).rstrip("/")
                     for u in _list(s, f"{base}/rounds/{seq}/availabilities?adjudicators=true")}
            row["result"] = "done" if after == set(final) else "CHECK"
            if after != set(final):
                row["problem"] = (f"After writing, the tab lists {len(after)} available judges but {len(final)} were expected. "
                                  f"Missing: {', '.join(name_of.get(u, u) for u in sorted(set(final) - after)) or '-'}; "
                                  f"unexpected: {', '.join(name_of.get(u, u) for u in sorted(after - set(final))) or '-'}.")
        elif apply:
            row["result"] = "nothing to change"
        plan["rounds"].append(row)
    return plan


# ------------------------------------------------------------------ web page
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Judge availability per round</title><style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:860px;margin:20px auto;padding:0 16px;background:#f5f5f5}
.card{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:16px}label{display:block;font-weight:600;margin:14px 0 4px}
input[type=text],input[type=url],select,input[type=file]{width:100%;padding:9px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
button{background:#28a745;color:#fff;border:0;padding:12px 24px;border-radius:6px;font-size:15px;cursor:pointer;margin-top:16px}
.err{background:#f8d7da;color:#721c24;padding:10px;border-radius:6px;margin-bottom:12px}.hint{font-size:12px;color:#888;margin:4px 0}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
.ok{color:#155724}.warn{color:#856404}.bad{color:#721c24}details{margin:8px 0}summary{cursor:pointer;font-weight:600}</style></head><body>
<p><a href="/">&larr; importer</a></p>
<div class="card"><h2>Judge availability per round &larr; availability form</h2>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post" enctype="multipart/form-data">
<label>Tab site URL</label><input type="url" name="url" value="{{ f.url }}" placeholder="https://yoursite.calicotab.com">
<label>Tournament slug (optional)</label><input type="text" name="slug" value="{{ f.slug }}">
<label>Admin API token</label><input type="text" name="token" autocomplete="off">
<label>Availability form answers (.xlsx or .csv)</label><input type="file" name="sheet" accept=".xlsx,.xlsm,.csv">
<p class="hint">Columns used: Email Address, Full Name, Name Preference, Timestamp and "Round-by-Round Availability [Round N]".</p>
<label>"Maybe / Subject to change" counts as</label>
<select name="maybe"><option value="no" {{ 'selected' if f.maybe=='no' else '' }}>not available (default)</option><option value="yes" {{ 'selected' if f.maybe=='yes' else '' }}>available</option>
<option value="leave" {{ 'selected' if f.maybe=='leave' else '' }}>leave the judge's availability unchanged</option></select>
<label>Only these rounds (optional)</label><input type="text" name="rounds" value="{{ f.rounds }}" placeholder="e.g. 2  or  1,2  (empty = every round in the sheet)">
<p class="hint">Handy for a first real try on one round.</p>
<label style="font-weight:400;margin-top:14px"><input type="checkbox" name="dry_run" {{ 'checked' if f.dry_run else '' }}> <strong>Preview only</strong> &mdash; read the tab and show what would change, write nothing</label>
<button type="submit">Run</button></form></div>
{% if plan %}
<div class="card"><h3>{{ 'Preview (nothing was written)' if dry_run else 'Result' }} &mdash; {{ plan.slug }}</h3>
<p>{{ plan.pairs|length }} of {{ n_people }} form answers matched a judge on the tab ({{ plan.n_judges }} judges on the tab).
{% if dups %}Answered twice (latest answer used): {{ dups|join(', ') }}.{% endif %}</p>
<table><tr><th>Round</th><th>Available now</th><th>To mark available</th><th>To mark unavailable</th><th>Already right</th><th>No answer</th>{% if not dry_run %}<th>Result</th>{% endif %}</tr>
{% for r in plan.rounds %}<tr><td>R{{ r.seq }}</td><td>{{ r.available_now }}</td><td>{{ r.to_add|length }}</td><td>{{ r.to_remove|length }}</td><td>{{ r.already }}</td><td>{{ r.undecided }}</td>
{% if not dry_run %}<td class="{{ 'ok' if r.result in ('done','nothing to change') else 'bad' }}">{{ r.result }}</td>{% endif %}</tr>
{% if r.problem %}<tr><td colspan="7" class="bad">{{ r.problem }}</td></tr>{% endif %}{% endfor %}</table>
{% if plan.skipped_rounds %}<p class="warn">Rounds {{ plan.skipped_rounds|join(', ') }} are in the sheet but do not exist on the tab: skipped.</p>{% endif %}
{% for r in plan.rounds %}{% if r.to_add or r.to_remove %}<details><summary>R{{ r.seq }}: names</summary>
{% if r.to_add %}<p><strong>Marked available:</strong> {{ r.to_add|map('extract', r.name_of)|join(', ') }}</p>{% endif %}
{% if r.to_remove %}<p><strong>Marked unavailable:</strong> {{ r.to_remove|map('extract', r.name_of)|join(', ') }}</p>{% endif %}</details>{% endif %}{% endfor %}
<details><summary>How the form answers were matched ({{ plan.pairs|length }})</summary><table>
{% for p, j, how in plan.pairs %}<tr><td>{{ p.full or p.pref }}</td><td>&rarr; {{ j.name }}</td><td class="{{ 'ok' if how in ('email','name') else 'warn' }}">{{ how }}</td></tr>{% endfor %}</table></details>
{% if plan.unmatched %}<p class="bad"><strong>Not found on the tab ({{ plan.unmatched|length }}) &mdash; nothing was done for them:</strong>
{% for p in plan.unmatched %}{{ p.full or p.pref }} ({{ p.email }}){{ ', ' if not loop.last }}{% endfor %}</p>{% endif %}
<details><summary>Judges on the tab without a form answer ({{ plan.unanswered|length }}) &mdash; left untouched</summary><p>{{ plan.unanswered|map(attribute='name')|join(', ') }}</p></details>
</div>{% endif %}</body></html>"""


def register_availability(app):
    from flask import render_template_string, request
    app.jinja_env.filters.setdefault("extract", lambda key, mapping: mapping.get(key, key))

    @app.route("/availability", methods=["GET", "POST"])
    def availability_page():
        if request.method == "GET":
            return render_template_string(PAGE, f={"url": "", "slug": "", "maybe": "no", "dry_run": True, "rounds": ""}, plan=None, error=None)
        form = request.form
        f = {"url": form.get("url", ""), "slug": form.get("slug", ""), "maybe": form.get("maybe", "no"), "dry_run": bool(form.get("dry_run")),
             "rounds": form.get("rounds", "")}
        try:
            up = request.files.get("sheet")
            if not f["url"].strip():
                raise ValueError("The tab site URL is needed.")
            if not form.get("token", "").strip():
                raise ValueError("The admin API token is needed.")
            if not up or not up.filename:
                raise ValueError("Upload the availability form answers (.xlsx or .csv).")
            people, rounds, dups = parse_sheet(up.filename, up.read())
            only = {int(x) for x in re.findall(r"\d+", f["rounds"])}
            if only:
                rounds = [r for r in rounds if r in only]
                if not rounds:
                    raise ValueError("None of the rounds you typed is in the sheet.")
            plan = build_plan(f["url"], form.get("token", ""), f["slug"], people, rounds, f["maybe"], apply=not f["dry_run"])
        except (ValueError, RuntimeError) as e:
            return render_template_string(PAGE, f=f, plan=None, error=str(e))
        return render_template_string(PAGE, f=f, plan=plan, error=None, dry_run=f["dry_run"], n_people=len(people), dups=dups)
