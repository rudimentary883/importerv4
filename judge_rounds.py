"""
Judges who judged at least N preliminary rounds  ->  .csv  (name, number_of_rounds)

Two ways to use it (same code):
  1. Command line (any computer with Python + `pip install requests`):
         python judge_rounds.py https://yoursite.calicotab.com  YOUR_ADMIN_TOKEN  [--slug SLUG] [--min 3]
     writes judges_3plus_rounds.csv
  2. Web page /judge-rounds on the importer (needs the 2 lines at the bottom of app.py):
         from judge_rounds import register_judge_rounds
         register_judge_rounds(app)

Counting rules (change with the options):
  * preliminary rounds = rounds whose stage is "P" (Preliminary)
  * only rounds whose draw is Confirmed or Released are counted (a draft draw is not "judged") - option: include drafts
  * chairs, panellists AND trainees count (any seat in a round) - option: leave trainees out
  * a judge is counted once per round, however many debates they sat in
"""
import argparse
import csv
import io
import sys
import time
from urllib.parse import urlsplit

import requests


def _get(session, url, retries=3):
    for attempt in range(retries):
        try:
            time.sleep(0.2)
            resp = session.get(url, timeout=30)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"Could not reach {url}: {e}")
            time.sleep(1)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise RuntimeError(f"HTTP {resp.status_code} on {url}: the token was refused - it must be an ADMIN "
                               f"account's token (Change Password page).")
        raise RuntimeError(f"HTTP {resp.status_code} on {url}")
    raise RuntimeError(f"Rate limited on {url}")


def _list(session, url):
    data = _get(session, url)
    if isinstance(data, dict) and isinstance(data.get('results'), list):      # paginated, just in case
        items = list(data['results'])
        while data.get('next'):
            data = _get(session, data['next'])
            items += data.get('results', [])
        return items
    return data if isinstance(data, list) else []


def _norm_url(u):
    return (u.get('url') if isinstance(u, dict) else str(u or '')).rstrip('/')


def guess_slug(url):
    parts = [p for p in urlsplit(url.strip()).path.split('/') if p]
    return parts[0] if parts and parts[0] not in ('_', 'api', 'admin') else ''


def compute(url, token, slug='', min_rounds=3, include_trainees=True, include_drafts=False, session=None):
    sp = urlsplit(url.strip())
    origin = f"{sp.scheme or 'https'}://{sp.netloc or sp.path.split('/')[0]}"
    s = session or requests.Session()
    s.headers['Authorization'] = f'Token {token.strip()}'
    slug = (slug or guess_slug(url)).strip()
    if not slug:
        slug = ''
    tournaments = _list(s, f"{origin}/api/v1/tournaments")
    slugs = [t.get('slug') for t in tournaments if isinstance(t, dict)]
    if slug not in slugs:
        if len(slugs) == 1:
            slug = slugs[0]
        else:
            raise ValueError("Which tournament? Add its slug. Found: " + ', '.join(map(str, slugs)))
    base = f"{origin}/api/v1/tournaments/{slug}"

    rounds = _list(s, base + '/rounds')
    prelim = [r for r in rounds if (r.get('stage') == 'P' if 'stage' in r else not r.get('break_category'))]
    prelim.sort(key=lambda r: r.get('seq') or 0)
    names = {}
    for a in _list(s, base + '/adjudicators'):
        names[_norm_url(a.get('url') or '')] = a.get('name') or ''
    counted, skipped = [], []
    per_judge = {}
    for r in prelim:
        label = r.get('abbreviation') or r.get('name') or str(r.get('seq'))
        if r.get('draw_status') not in ('C', 'R') and not include_drafts:
            skipped.append(label)
            continue
        counted.append(label)
        pairing_url = ((r.get('_links') or {}).get('pairing')) or (_norm_url(r) + '/pairings')
        in_round = set()
        for debate in _list(s, pairing_url):
            adj = debate.get('adjudicators') or {}
            if isinstance(adj, list):
                urls = adj
            else:
                urls = ([adj.get('chair')] + list(adj.get('panellists') or [])
                        + (list(adj.get('trainees') or []) if include_trainees else []))
            in_round.update(_norm_url(u) for u in urls if u)
        for u in in_round:
            per_judge[u] = per_judge.get(u, 0) + 1
    rows = sorted(((names.get(u) or u, n) for u, n in per_judge.items() if n >= min_rounds),
                  key=lambda x: (-x[1], x[0].casefold()))
    return {'rows': rows, 'prelim_total': len(prelim), 'counted': counted, 'skipped': skipped, 'slug': slug,
            'judges_seen': len(per_judge)}


def to_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['name', 'number_of_rounds'])
    w.writerows(rows)
    return out.getvalue()


def register_judge_rounds(app):
    from flask import Response, request
    from flask import render_template_string
    page = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Judges by rounds judged</title><style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:20px auto;padding:0 16px;background:#f5f5f5}
.card{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.1)}label{display:block;font-weight:600;margin:14px 0 4px}
input[type=text],input[type=url],input[type=number]{width:100%;padding:9px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
button{background:#28a745;color:#fff;border:0;padding:12px 24px;border-radius:6px;font-size:15px;cursor:pointer;margin-top:16px}
.err{background:#f8d7da;color:#721c24;padding:10px;border-radius:6px;margin-bottom:12px}.hint{font-size:12px;color:#888}</style></head><body>
<p><a href="/">&larr; importer</a></p><div class="card"><h2>Judges who judged at least N preliminary rounds</h2>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post"><label>Tab site URL</label><input type="url" name="url" value="{{ f.url }}" placeholder="https://yoursite.calicotab.com">
<label>Tournament slug (optional)</label><input type="text" name="slug" value="{{ f.slug }}"><p class="hint">Leave blank if the site has only one tournament.</p>
<label>Admin API token</label><input type="text" name="token" autocomplete="off">
<label>Minimum rounds</label><input type="number" name="min" value="{{ f.min or 3 }}" min="1">
<label><input type="checkbox" name="trainees" checked> count trainee seats too</label><label><input type="checkbox" name="drafts"> count draft (unconfirmed) draws too</label>
<button type="submit">Download .csv</button></form></div></body></html>"""

    @app.route('/judge-rounds', methods=['GET', 'POST'])
    def judge_rounds_page():
        f = {'url': request.form.get('url', ''), 'slug': request.form.get('slug', ''), 'min': request.form.get('min', '3')}
        if request.method == 'GET':
            return render_template_string(page, f=f, error=None)
        try:
            res = compute(f['url'], request.form.get('token', ''), f['slug'], int(f['min'] or 3),
                          bool(request.form.get('trainees')), bool(request.form.get('drafts')))
        except Exception as e:
            return render_template_string(page, f=f, error=str(e))
        return Response(to_csv(res['rows']), mimetype='text/csv', headers={
            'Content-Disposition': f"attachment; filename=judges_{f['min']}plus_rounds.csv",
            'X-Rounds-Counted': ','.join(res['counted'])})


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('url')
    ap.add_argument('token')
    ap.add_argument('--slug', default='')
    ap.add_argument('--min', type=int, default=3)
    ap.add_argument('--no-trainees', action='store_true', help='leave trainee seats out of the count')
    ap.add_argument('--drafts', action='store_true')
    a = ap.parse_args()
    try:
        res = compute(a.url, a.token, a.slug, a.min, not a.no_trainees, a.drafts)
    except Exception as e:
        sys.exit(f"Error: {e}")
    out = f"judges_{a.min}plus_rounds.csv"
    with open(out, 'w', encoding='utf-8-sig', newline='') as fh:
        fh.write(to_csv(res['rows']))
    print(f"{len(res['rows'])} judges with at least {a.min} rounds -> {out}")
    print(f"Preliminary rounds: {res['prelim_total']} | counted: {', '.join(res['counted'])} | "
          f"skipped (draw not confirmed/released): {', '.join(res['skipped']) or 'none'}")
