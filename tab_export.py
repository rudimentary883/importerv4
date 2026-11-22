"""
Export what is ON THE TAB as import-ready CSV files (page /export-tab)

The importer's sync reads a file and updates the tab.  This page does the opposite: it reads the tab through the API and writes the
same files the importer reads, so the next import / correction starts from the latest version that is on the tab:

    institutions.csv     id, name, code, region
    adjudicators.csv     id, name, institution (code), email, gender, base_score, independent, adj_core, notes
    teams.csv            id, institution (code), reference, short_reference, code_name, use_institution_prefix, emoji, team_name (human), break_categories
    speakers.csv         id, team ("<institution code> <reference>"), name, gender, email, phone, anonymous, categories      (separate speakers file)
    teams_speakers.csv   the combined file: the team columns + speaker_1_name ... speaker_N_categories  (N = most speakers in one team, at least 3)

* the `id` column is the Tabbycat id (the importer ignores it); categories are written by name, several separated by "; "
* one file chosen -> that .csv, several -> one .zip;  files are UTF-8 without BOM (what Google Sheets exports; the importer reads utf-8)
* judges: notes are only exported when the API returns them;  nothing is ever written to the tab

Needs the 2 lines at the bottom of app.py:
    from tab_export import register_tab_export
    register_tab_export(app)
"""
import csv
import io
import re
import time
import zipfile
from urllib.parse import urlsplit

import requests

from combined_import import SPEAKER_FIELDS, TEAM_COLUMNS

UA = "Mozilla/5.0 (compatible; TabbycatTabExport/1.0; Render)"


# ------------------------------------------------------------------ API helpers
def _get_json(session, url, retries=3):
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=40)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"Could not reach {url}: {e}")
            time.sleep(1)
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                raise RuntimeError(f"{url} did not return JSON")
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise RuntimeError(f"HTTP {resp.status_code} on {url}: the token was refused - it must be an ADMIN account's token.")
        raise RuntimeError(f"HTTP {resp.status_code} on {url}")
    raise RuntimeError(f"Rate limited on {url}")


def _list(session, url):
    data = _get_json(session, url)
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items = list(data["results"])
        while data.get("next"):
            data = _get_json(session, data["next"])
            items += data.get("results", [])
        return items
    return data if isinstance(data, list) else []


def _key(ref):
    return str(ref.get("url") if isinstance(ref, dict) else ref or "").rstrip("/")


def _id_of(ref):
    return _key(ref).rsplit("/", 1)[-1]


def guess_slug(url):
    parts = [p for p in urlsplit(url.strip()).path.split("/") if p]
    return parts[0] if parts and parts[0] not in ("_", "api", "admin") else ""


def _bool(v):
    return "TRUE" if v else "FALSE"


def _num(v):
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else ("%.4f" % f).rstrip("0").rstrip(".")


def _csv(rows):
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(rows)
    return out.getvalue()


def _natural(text):
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", str(text))]


# ------------------------------------------------------------------ read the tab
def read_tab(url, token, slug, session=None, need=("institutions", "adjudicators", "teams", "speakers")):
    s = session or requests.Session()
    s.headers.update({"Authorization": f"Token {token.strip()}", "Accept": "application/json", "User-Agent": UA})
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
    tab = {"slug": slug}
    tab["institutions"] = _list(s, f"{origin}/api/v1/institutions")
    if "adjudicators" in need:
        tab["adjudicators"] = _list(s, base + "/adjudicators")
    if "teams" in need or "speakers" in need:
        tab["teams"] = _list(s, base + "/teams")
        tab["speakers"] = _list(s, base + "/speakers")
        tab["break_categories"] = {_key(c): c.get("name", "") for c in _list(s, base + "/break-categories")}
        tab["speaker_categories"] = {_key(c): c.get("name", "") for c in _list(s, base + "/speaker-categories")}
    return tab


# ------------------------------------------------------------------ build the CSV files
def build_files(tab, want):
    inst_by = {_key(i): i for i in tab["institutions"]}
    code_of = lambda ref: (inst_by.get(_key(ref)) or {}).get("code", "") if ref else ""
    files = {}
    if "institutions" in want:
        rows = [["id", "name", "code", "region"]]
        for i in sorted(tab["institutions"], key=lambda i: (str(i.get("name", "")).casefold())):
            rows.append([i.get("id", ""), i.get("name", ""), i.get("code", ""), i.get("region") or ""])
        files["institutions.csv"] = _csv(rows)
    if "adjudicators" in want:
        rows = [["id", "name", "institution", "email", "gender", "base_score", "independent", "adj_core", "notes"]]
        for a in sorted(tab["adjudicators"], key=lambda a: str(a.get("name", "")).casefold()):
            rows.append([a.get("id", ""), a.get("name", ""), code_of(a.get("institution")), a.get("email") or "", a.get("gender") or "",
                         _num(a.get("base_score")), _bool(a.get("independent")), _bool(a.get("adj_core")), a.get("notes") or ""])
        files["adjudicators.csv"] = _csv(rows)
    if want & {"teams", "speakers", "teams_speakers"}:
        bcats, scats = tab["break_categories"], tab["speaker_categories"]
        by_team = {}
        for sp in tab["speakers"]:
            by_team.setdefault(_key(sp.get("team")), []).append(sp)
        for lst in by_team.values():
            lst.sort(key=lambda x: x.get("id", 0))
        def team_row(t):
            code = code_of(t.get("institution"))
            ref = t.get("reference") or ""
            return {"id": t.get("id", ""), "code": code, "ref": ref, "default": f"{code or 'Independent'} {ref}",
                    "cols": [code, ref, t.get("short_reference") or ref, t.get("code_name") or "", _bool(t.get("use_institution_prefix", True)),
                             t.get("emoji") or "", f"{code or 'Independent'} {ref}",
                             "; ".join(bcats.get(_key(u), "") for u in t.get("break_categories", []) if bcats.get(_key(u)))]}
        teams = sorted((team_row(t) | {"_t": t} for t in tab["teams"]), key=lambda r: (_natural(r["code"]), _natural(r["ref"])))
        spk_cols = lambda sp: [sp.get("name", ""), sp.get("gender") or "", sp.get("email") or "", sp.get("phone") or "",
                               _bool(sp.get("anonymous")), "; ".join(scats.get(_key(u), "") for u in sp.get("categories", []) if scats.get(_key(u)))]
        if "teams" in want:
            rows = [["id"] + TEAM_COLUMNS]
            rows += [[r["id"]] + r["cols"] for r in teams]
            files["teams.csv"] = _csv(rows)
        if "speakers" in want:
            rows = [["id", "team", "name", "gender", "email", "phone", "anonymous", "categories"]]
            for r in teams:
                for sp in by_team.get(_key(r["_t"]), []):
                    c = spk_cols(sp)
                    rows.append([sp.get("id", ""), r["default"], c[0], c[1], c[2], c[3], c[4], c[5]])
            files["speakers.csv"] = _csv(rows)
        if "teams_speakers" in want:
            slots = max([3] + [len(v) for v in by_team.values()])
            head = ["id"] + TEAM_COLUMNS + [f"speaker_{n}_{f}" for n in range(1, slots + 1) for f in SPEAKER_FIELDS]
            rows = [head]
            for r in teams:
                row = [r["id"]] + r["cols"]
                spks = by_team.get(_key(r["_t"]), [])
                for n in range(slots):
                    row += spk_cols(spks[n]) if n < len(spks) else [""] * len(SPEAKER_FIELDS)
                rows.append(row)
            files["teams_speakers.csv"] = _csv(rows)
    return files


# ------------------------------------------------------------------ web page
CHOICES = [("institutions", "Institutions"), ("adjudicators", "Judges (adjudicators)"), ("teams_speakers", "Teams + speakers, combined file"),
           ("teams", "Teams only"), ("speakers", "Speakers only (separate file)")]

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Export the tab as import files</title><style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:20px auto;padding:0 16px;background:#f5f5f5}
.card{background:#fff;border-radius:8px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.1)}label{display:block;font-weight:600;margin:14px 0 4px}label.i{font-weight:400;margin:6px 0}
input[type=text],input[type=url]{width:100%;padding:9px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
button{background:#28a745;color:#fff;border:0;padding:12px 24px;border-radius:6px;font-size:15px;cursor:pointer;margin-top:16px}
.err{background:#f8d7da;color:#721c24;padding:10px;border-radius:6px;margin-bottom:12px}.hint{font-size:12px;color:#888;margin:4px 0}</style></head><body>
<p><a href="/">&larr; importer</a></p><div class="card"><h2>Export the tab as import-ready CSV files</h2>
<p class="hint">The opposite of sync: reads what is on the tab and writes the same files the importer reads, so new teams and corrections start from the latest version. Nothing is written to the tab.</p>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post"><label>Tab site URL</label><input type="url" name="url" value="{{ f.url }}" placeholder="https://yoursite.calicotab.com">
<label>Tournament slug (optional)</label><input type="text" name="slug" value="{{ f.slug }}">
<label>Admin API token</label><input type="text" name="token" autocomplete="off">
<label>Files</label>
{% for key, text in choices %}<label class="i"><input type="checkbox" name="what" value="{{ key }}" {{ 'checked' if key in f.what else '' }}> {{ text }}</label>{% endfor %}
<p class="hint">One file is downloaded as .csv, several as one .zip. Use either the combined file or the separate teams + speakers files for an import, not both.</p>
<button type="submit">Download</button></form></div></body></html>"""


def register_tab_export(app):
    from flask import Response, render_template_string, request

    @app.route("/export-tab", methods=["GET", "POST"])
    def export_tab_page():
        f = {"url": "", "slug": "", "what": ["institutions", "adjudicators", "teams_speakers"]}
        if request.method == "GET":
            return render_template_string(PAGE, f=f, choices=CHOICES, error=None)
        form = request.form
        f = {"url": form.get("url", ""), "slug": form.get("slug", ""), "what": [w for w in form.getlist("what") if w in dict(CHOICES)]}
        try:
            if not f["url"].strip():
                raise ValueError("The tab site URL is needed.")
            if not form.get("token", "").strip():
                raise ValueError("The admin API token is needed.")
            if not f["what"]:
                raise ValueError("Tick at least one file.")
            want = set(f["what"])
            need = set()
            if "adjudicators" in want:
                need.add("adjudicators")
            if want & {"teams", "speakers", "teams_speakers"}:
                need |= {"teams", "speakers"}
            tab = read_tab(f["url"], form.get("token", ""), f["slug"], need=tuple(need) or ("institutions",))
            files = build_files(tab, want)
        except (ValueError, RuntimeError) as e:
            return render_template_string(PAGE, f=f, choices=CHOICES, error=str(e))
        slug = tab["slug"]
        if len(files) == 1:
            name, text = next(iter(files.items()))
            return Response(text.encode("utf-8"), mimetype="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f"attachment; filename={slug}_{name}"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, text in files.items():
                z.writestr(name, text.encode("utf-8"))
        return Response(buf.getvalue(), mimetype="application/zip",
                        headers={"Content-Disposition": f"attachment; filename={slug}_tab_export.zip"})
