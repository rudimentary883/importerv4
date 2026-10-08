"""
combined_import.py  --  "Teams + Speakers in ONE file" reader for the Tabbycat Importer (v5.0)

One row = one team, with its speakers laid out horizontally, e.g.

  institution | reference | ... | speaker_1_name | speaker_1_email | ... | speaker_2_name | ... | speaker_5_name

The reader does NOT need exact column names. It recognises the importer's own
"standard" headers and also typical Google-Form / registration-sheet headers such as

  "Name of Speaker 1", "Email Address of Speaker 1", "Phone Number (WhatsApp) of Speaker 1",
  "Gender of Speaker 1", "Does Speaker 1 qualify as Novice?", "Team Name", "Team Name 2",
  "Full Name of Institution", "Abbreviation for Institution", "Institution of Speaker 1", ...

and turns them into the same team / speaker / institution rows the importer already uses
(so the existing batch logic - skip what already exists, import only what is new - applies unchanged).

Blank speaker columns are simply ignored, so a WSDC team with only 3 speakers filled in out of
5 columns is read as a 3-speaker team (no error).
"""

import csv
import io
import re
import unicodedata
from collections import Counter, OrderedDict

MAX_SPEAKER_SLOTS = 5

# ----------------------------------------------------------------------------
# text helpers
# ----------------------------------------------------------------------------

def clean_text(value):
    """Cell value -> single-line, trimmed text ('' for None / NaN)."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        if value != value:          # NaN
            return ''
        if value.is_integer():
            value = int(value)
    text = str(value).replace('\u00a0', ' ').replace('\u200b', '')
    text = re.sub(r'\s+', ' ', text).strip()
    return '' if text.lower() in ('none', 'nan', 'null') else text


def norm(value):
    return re.sub(r'\s+', ' ', clean_text(value)).casefold()


def inst_key(text, strip_brackets=False):
    """
    Forgiving key for matching institution names / codes:
    ignores case, accents, punctuation, '&' vs 'and' and a leading 'the'.
      "St. Stephen's College" == "St Stephens College"
      "Delhi college  of  arts  and  commerce" == "Delhi College of Arts & Commerce"
    A bracketed part is kept ("Vellore Institute of Technology ( Vellore )" == "... Technology, Vellore")
    unless strip_brackets=True (used as a second attempt: "College of Vocational Studies (CVS)" == "... Studies").
    """
    t = clean_text(text)
    t = unicodedata.normalize('NFKD', t)
    t = ''.join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace('&', ' and ')
    t = re.sub(r'\([^)]*\)', ' ', t) if strip_brackets else t.replace('(', ' ').replace(')', ' ')
    t = re.sub(r"[\u2019'`]", '', t)
    t = re.sub(r'[^\w\s]', ' ', t.casefold())
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'^the ', '', t)
    return t


def bracket_codes(text):
    return [clean_text(m) for m in re.findall(r'\(([^)]{1,20})\)', clean_text(text)) if clean_text(m)]


def parse_yes(value):
    return norm(value) in ('yes', 'y', 'true', '1', 't', 'yeah', 'yep')


def parse_bool_or_none(value):
    t = norm(value)
    if t in ('true', '1', 'yes', 'y', 't'):
        return True
    if t in ('false', '0', 'no', 'n', 'f'):
        return False
    return None


def normalize_gender(value):
    """'Cis Female' -> F, 'Cis Male' -> M, 'Non-Binary' / 'Agender' / 'Other' -> O, 'Prefer Not Say' -> ''."""
    t = norm(value)
    if not t:
        return ''
    if re.search(r'non[\s-]?binary|agender|gender ?queer|gender ?fluid|\bother\b|\bnb\b|enby|^o$', t):
        return 'O'
    if re.search(r'prefer not|rather not|not say|not specified|unspecified|^n/?a$', t):
        return ''
    if re.search(r'\b(female|woman|girl|f)\b', t):
        return 'F'
    if re.search(r'\b(male|man|boy|m)\b', t):
        return 'M'
    return ''


EMAIL_RE = re.compile(r'^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$')

_STOP_WORDS = {'of', 'the', 'for', 'and', 'in', 'at', 'de', 'la', 'a', 'an'}


def make_code(name, taken):
    """Short unique code for an auto-created institution ('Zakir Husain Delhi College' -> 'ZHDC')."""
    taken = {norm(t) for t in taken}
    brackets = bracket_codes(name)
    base = ''
    if brackets and re.fullmatch(r'[A-Za-z0-9\-& ]{2,12}', brackets[0]):
        base = brackets[0]
    if not base:
        words = [w for w in re.findall(r'\w+', re.sub(r'\([^)]*\)', ' ', clean_text(name)))
                 if w.lower() not in _STOP_WORDS]
        if len(words) > 1:
            base = ''.join(w[0] for w in words).upper()[:12]
        elif words:
            base = words[0][:12]
    base = base or 'INST'
    code, i = base, 2
    while norm(code) in taken:
        code = f'{base}{i}'
        i += 1
    return code


# ----------------------------------------------------------------------------
# column detection
# ----------------------------------------------------------------------------

_WORD_NUM = {'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
             'first': '1', 'second': '2', 'third': '3', 'fourth': '4', 'fifth': '5',
             '1st': '1', '2nd': '2', '3rd': '3', '4th': '4', '5th': '5'}


def clean_header(header):
    """Lower-case the FIRST line of a header and strip punctuation / bracketed notes."""
    text = '' if header is None else str(header)
    first = ''
    for line in text.splitlines():
        if line.strip():
            first = line.strip()
            break
    t = first.lower()
    t = re.sub(r'\(\s*human\s*\)', ' human ', t)
    t = re.sub(r'\(([^)]*speaker[^)]*)\)', r' \1 ', t)      # keep "(Speaker 1)"
    t = re.sub(r'\([^)]*\)', ' ', t)                        # drop "(WhatsApp)" etc.
    t = t.replace('&', ' and ')
    t = re.sub(r'[_\-/\\.:,;?!*#]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


_SPK = r'(?:speaker|spk|debater|member)'


def _speaker_index(t):
    t = re.sub(r'\b(%s)\s+(one|two|three|four|five)\b' % _SPK,
               lambda m: f'{m.group(1)} {_WORD_NUM[m.group(2)]}', t)
    t = re.sub(r'\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(%s)\b' % _SPK,
               lambda m: f'{m.group(2)} {_WORD_NUM[m.group(1)]}', t)
    m = re.search(r'\b(?:%s|s)\s*(\d)\b' % _SPK, t)
    if not m:
        return None, t
    n = int(m.group(1))
    rest = re.sub(r'\b(?:%s|s)\s*\d\b' % _SPK, ' ', t)
    return n, re.sub(r'\s+', ' ', rest).strip()


def _speaker_attr(rest):
    if re.search(r'institution|university|college|school|affiliation|organi[sz]ation|society|club', rest):
        return 'institution'
    if re.search(r'e ?mail', rest):
        return 'email'
    if re.search(r'phone|whats ?app|mobile|contact|telephone|cell', rest):
        return 'phone'
    if re.search(r'gender|\bsex\b', rest):
        return 'gender'
    if re.search(r'anonym|redact', rest):
        return 'anonymous'
    if re.search(r'categor', rest):
        return 'categories'
    if re.search(r'novice', rest):
        return 'novice'
    if re.search(r'minor|under 18|underage', rest):
        return 'minor'
    if re.search(r'name', rest) or rest == '':
        return 'name'
    return None


_TEAM_PATTERNS = [
    ('registration_type', r'registration type'),
    ('code_name', r'(team )?code ?name|team code'),
    ('team_name_human', r'team name human|human team name'),
    ('use_prefix', r'use institution prefix|institution prefix|use prefix'),
    ('short_reference', r'short reference|short team name'),
    ('reference', r'(team )?reference'),
    ('team_name', r'(name of )?team( name)?( \d+)?'),
    ('emoji', r'emoji'),
    ('institution_code', r'(institution |inst |university |college |school )?(abbreviation|abbrev|acronym|code)'
                         r'( for (the )?(institution|university|college|school))?'),
    ('institution', r'(full name of )?(the )?(institution|university|college|school|affiliation)( name)?'),
]

FIELD_LABELS = OrderedDict([
    ('institution', 'Team institution'),
    ('institution_code', 'Institution abbreviation / code'),
    ('reference', 'Team reference'),
    ('team_name', 'Team name'),
    ('team_name_human', 'Team name (human)'),
    ('short_reference', 'Short reference'),
    ('code_name', 'Team code name'),
    ('use_prefix', 'Use institution prefix'),
    ('emoji', 'Emoji'),
    ('registration_type', 'Registration type (used to skip adjudicator rows)'),
])
SPEAKER_LABELS = OrderedDict([
    ('name', 'Name'), ('email', 'Email'), ('phone', 'Phone'), ('gender', 'Gender'),
    ('anonymous', 'Anonymous'), ('categories', 'Categories'), ('novice', 'Novice? (-> "Novice" category)'),
    ('institution', 'Speaker institution'), ('minor', 'Minor? (not imported)'),
])


def detect_columns(headers):
    """
    headers -> {'team': {field: [header,...]}, 'speaker': {n: {attr: [header,...]}}, 'ignored': [header,...]}
    Several columns can feed one field (e.g. "Team Name" and "Team Name 2"); the first non-empty one wins per row.
    """
    team, speaker, ignored = {}, {}, []
    for h in headers:
        if h is None or clean_text(h) == '':
            continue
        t = clean_header(h)
        n, rest = _speaker_index(t)
        if n is not None and 1 <= n <= MAX_SPEAKER_SLOTS:
            attr = _speaker_attr(rest)
            if attr:
                speaker.setdefault(n, {}).setdefault(attr, []).append(h)
                continue
        elif n is None:
            for field, pattern in _TEAM_PATTERNS:
                if re.fullmatch(pattern, t):
                    team.setdefault(field, []).append(h)
                    break
            else:
                ignored.append(h)
            continue
        ignored.append(h)
    return {'team': team, 'speaker': dict(sorted(speaker.items())), 'ignored': ignored}


def mapping_report(mapping):
    """Human-readable [(label, 'Column A + Column B'), ...] for the results page."""
    def fmt(headers):
        return ' + '.join(clean_text(str(h).splitlines()[0] if str(h).strip() else h) for h in headers)
    out = []
    for field, label in FIELD_LABELS.items():
        if field in mapping['team']:
            out.append((label, fmt(mapping['team'][field])))
    for n, attrs in mapping['speaker'].items():
        for attr, label in SPEAKER_LABELS.items():
            if attr in attrs:
                out.append((f'Speaker {n} - {label}', fmt(attrs[attr])))
    return out


# ----------------------------------------------------------------------------
# institution registry (institutions found in / derived from the combined file)
# ----------------------------------------------------------------------------

class InstitutionRegistry:
    def __init__(self, known):
        self.known_codes = set()
        self.entries = []              # entries OWNED by this file (derived / auto-created)
        self._by_key = {}              # inst_key(name or code) -> entry (known + owned)
        self._by_code = {}             # norm(code) -> entry
        for inst in known:
            self._register({'name': inst['name'], 'code': inst['code'], 'known': True})
            self.known_codes.add(norm(inst['code']))

    def _register(self, entry):
        for key in (inst_key(entry['name']), inst_key(entry['code']),
                    inst_key(entry['name'], True), inst_key(entry['code'], True)):
            if key:
                self._by_key.setdefault(key, entry)
        self._by_code.setdefault(norm(entry['code']), entry)

    def taken_codes(self):
        return set(self._by_code)

    def find(self, text):
        text = clean_text(text)
        if not text:
            return None
        if norm(text) in self._by_code:
            return self._by_code[norm(text)]
        for key in (inst_key(text), inst_key(text, True)):
            if key and key in self._by_key:
                return self._by_key[key]
        for code in bracket_codes(text):
            if norm(code) in self._by_code:
                return self._by_code[norm(code)]
        return None

    def add_owned(self, name, code, auto_code=False):
        code = code if code else make_code(name, self.taken_codes())
        if norm(code) in self._by_code:                      # code clash with a different institution
            code = make_code(code, self.taken_codes())
        entry = {'name': name, 'code': code, 'region': '', 'known': False, 'auto_code': auto_code}
        self.entries.append(entry)
        self._register(entry)
        return entry


# ----------------------------------------------------------------------------
# main parser
# ----------------------------------------------------------------------------

def _first(row, headers):
    for h in headers or []:
        v = clean_text(row.get(h))
        if v:
            return v
    return ''


def _looks_like_code(text):
    return bool(re.fullmatch(r'[\w\-.&]{1,12}', clean_text(text)))


def parse_combined(rows, options=None, known_institutions=(), max_speakers=3,
                   debate_format='3v3', min_speakers=None):
    """
    rows -> {institutions, teams, speakers, warnings, mapping, mapping_rows, stats, clean_csv, ...}

    options:
      institution_policy : 'create' (default) | 'independent' | 'skip'   what to do with a team's institution
                           when it is not found in the file(s) (the tab site is checked later, at import time)
      prefix             : 'auto' (default) | 'always' | 'never'         use_institution_prefix
      novice_category    : True (default)  -> "Does Speaker N qualify as Novice? Yes" becomes category "Novice"
    """
    options = options or {}
    policy = options.get('institution_policy', 'create')
    prefix_opt = options.get('prefix', 'auto')
    novice_cat = options.get('novice_category', True)
    warnings = []

    headers = []
    for r in rows:
        for k in r.keys():
            if k is not None and k not in headers:
                headers.append(k)
    mapping = detect_columns(headers)
    tm, sm = mapping['team'], mapping['speaker']

    if not sm or not any('name' in attrs for attrs in sm.values()):
        raise ValueError(
            "No speaker name columns were found. Expected headers such as 'speaker_1_name' or "
            "'Name of Speaker 1' (see the template for the standard layout).")
    if 'team_name' not in tm and 'reference' not in tm and 'team_name_human' not in tm:
        warnings.append("No team name column was found (looked for 'Team Name', 'reference', ...). "
                        "Team names will be made up from the speakers' names.")

    slots = sorted(sm)
    standard_layout = 'reference' in tm
    stats = {'rows_total': len(rows), 'rows_used': 0, 'skipped': Counter(), 'teams': 0, 'speakers': 0,
             'dup_teams': 0, 'dup_speakers': 0, 'over_limit': 0, 'bad_emails': 0}

    # ---------------- pass 1: read every row -------------------------------------------------
    parsed = []
    pair_names, pair_codes = {}, {}          # inst_key -> Counter of spellings / abbreviations
    for idx, row in enumerate(rows, start=2):
        reg = _first(row, tm.get('registration_type'))
        spk = []
        for n in slots:
            attrs = sm[n]
            name = _first(row, attrs.get('name'))
            if not name:
                continue
            spk.append({
                'slot': n, 'name': name,
                'email': _first(row, attrs.get('email')),
                'phone': _first(row, attrs.get('phone')),
                'gender': _first(row, attrs.get('gender')),
                'anonymous': _first(row, attrs.get('anonymous')),
                'categories': _first(row, attrs.get('categories')),
                'novice': _first(row, attrs.get('novice')),
                'institution': _first(row, attrs.get('institution')),
            })
        team_text = _first(row, tm.get('team_name'))
        ref_text = _first(row, tm.get('reference'))
        human_text = _first(row, tm.get('team_name_human'))

        if re.search(r'adjudicat|judge', reg, re.I):
            stats['skipped']['adjudicator registration rows (use the Adjudicators file)'] += 1
            continue
        if not spk and not (team_text or ref_text or human_text):
            stats['skipped']['blank rows'] += 1
            continue

        inst_text = _first(row, tm.get('institution'))
        inst_code_text = _first(row, tm.get('institution_code'))
        if inst_text and inst_code_text:
            key = inst_key(inst_text)
            pair_names.setdefault(key, Counter())[inst_text] += 1
            pair_codes.setdefault(key, Counter())[inst_code_text] += 1

        parsed.append({'idx': idx, 'speakers': spk, 'team_text': team_text, 'ref_text': ref_text,
                       'human_text': human_text, 'inst_text': inst_text, 'inst_code_text': inst_code_text,
                       'short_ref': _first(row, tm.get('short_reference')),
                       'code_name': _first(row, tm.get('code_name')),
                       'emoji': _first(row, tm.get('emoji')),
                       'prefix_cell': _first(row, tm.get('use_prefix'))})

    # ---------------- institutions that the file itself defines (name + abbreviation) -----------
    registry = InstitutionRegistry(known_institutions)
    known_only = InstitutionRegistry(known_institutions)      # only the Institutions file, for the duplicate check below
    for key, names in pair_names.items():
        name = names.most_common(1)[0][0]
        code = pair_codes[key].most_common(1)[0][0]
        if len(pair_codes[key]) > 1:
            warnings.append(f"'{name}' was given several abbreviations ({', '.join(sorted(pair_codes[key]))}); "
                            f"using '{code}'.")
        existing = known_only.find(name) or known_only.find(code)
        if existing is None:
            registry.add_owned(name, code)

    # possible duplicates such as "Vellore Institute of Technology" vs "... , Vellore"
    owned_keys = sorted({inst_key(e['name']): e for e in registry.entries}.items())
    for i, (ka, ea) in enumerate(owned_keys):
        for kb, eb in owned_keys[i + 1:]:
            if kb.startswith(ka + ' ') or ka.startswith(kb + ' '):
                warnings.append(f"Possibly the same institution: '{ea['name']}' ({ea['code']}) and "
                                f"'{eb['name']}' ({eb['code']}) are imported as two institutions. "
                                f"Make the spelling identical in the sheet if they are the same.")

    # ---------------- pass 2: teams + speakers ------------------------------------------------
    teams, speakers, clean_rows = [], [], []
    seen_team_keys, used_displays = {}, set()
    cap = max_speakers if max_speakers and max_speakers > 0 else MAX_SPEAKER_SLOTS
    unresolved_notes = Counter()

    for p in parsed:
        # ---- the team's institution
        inst_code, soft = '', False
        speaker_insts = [s['institution'] for s in p['speakers']]
        if p['inst_text'] and p['inst_code_text']:
            entry = registry.find(p['inst_text']) or registry.find(p['inst_code_text'])
            inst_code = entry['code'] if entry else ''
        else:
            candidate = p['inst_text']
            if not candidate and speaker_insts:
                vals = [v for v in speaker_insts]
                keys = {inst_key(v) for v in vals}
                uniform = len(keys) == 1 and all(vals) and next(iter(keys)) not in ('', 'independent', 'none', 'na', 'n a', 'nil')
                candidate = vals[0] if uniform else ''          # mixed institutions -> composite -> no institution
            if candidate and inst_key(candidate) not in ('independent', 'none', 'na', 'n a', 'nil'):
                entry = registry.find(candidate)
                if entry:
                    inst_code = entry['code']
                elif policy == 'create' and not _looks_like_code(candidate):
                    inst_code = registry.add_owned(clean_text(candidate), '', auto_code=True)['code']
                elif policy == 'independent':
                    inst_code, soft = clean_text(candidate), True
                else:                                           # 'skip' (or an unnamed abbreviation we cannot create)
                    inst_code = clean_text(candidate)
        # ---- names
        reference = p['ref_text'] or p['team_text'] or p['human_text']
        if not reference:
            parts = [re.sub(r'[^\w]', '', s['name'].split()[-1]) for s in p['speakers'][:2] if s['name'].split()]
            reference = ' & '.join(x for x in parts if x) or f"Team row {p['idx']}"
            warnings.append(f"Row {p['idx']}: no team name, using '{reference}'.")
        reference = reference[:150]

        if prefix_opt == 'always':
            use_prefix = True
        elif prefix_opt == 'never':
            use_prefix = False
        else:
            cell = parse_bool_or_none(p['prefix_cell'])
            use_prefix = cell if cell is not None else standard_layout
        if not inst_code:
            use_prefix = False          # nothing to prefix a team without an institution with

        display = p['human_text'] or (f'{inst_code} {reference}' if (use_prefix and inst_code) else reference)

        # ---- duplicates
        key = (inst_key(inst_code), norm(reference))
        if key in seen_team_keys:
            stats['dup_teams'] += 1
            warnings.append(f"Row {p['idx']}: team '{reference}' appears more than once "
                            f"(first seen in row {seen_team_keys[key]}); only the first one is imported.")
            continue
        seen_team_keys[key] = p['idx']
        if norm(display) in used_displays:                      # same name, different institution
            display = f"{display} ({inst_code or 'independent'})"
            use_prefix = bool(inst_code) or use_prefix
            warnings.append(f"Row {p['idx']}: another team is already called '{reference}' - "
                            f"this one is kept as '{display}'.")
        used_displays.add(norm(display))

        short_ref = (p['short_ref'] or reference)[:35].strip()
        team = {'institution': inst_code, 'reference': reference, 'short_reference': short_ref,
                'code_name': p['code_name'], 'use_institution_prefix': bool(use_prefix),
                'emoji': p['emoji'], 'team_name_human': display, 'inst_soft': soft}
        if soft:
            unresolved_notes[clean_text(inst_code)] += 1

        # ---- speakers
        seen_names, team_speakers, dup_counts = set(), [], Counter()
        for s in p['speakers']:
            if norm(s['name']) in seen_names:
                stats['dup_speakers'] += 1
                dup_counts[s['name']] += 1
                continue
            if len(team_speakers) >= cap:
                stats['over_limit'] += 1
                warnings.append(f"Team '{reference}': speaker '{s['name']}' skipped "
                                f"(max {cap} speakers for the {debate_format.upper()} format).")
                continue
            seen_names.add(norm(s['name']))
            email = s['email']
            if email and not EMAIL_RE.match(email):
                stats['bad_emails'] += 1
                warnings.append(f"Team '{reference}': email '{email}' for {s['name']} is not valid - "
                                f"imported without an email.")
                email = ''
            cats = [c.strip() for c in re.split(r'[;,]', s['categories']) if c.strip()]
            if novice_cat and parse_yes(s['novice']) and 'novice' not in [c.lower() for c in cats]:
                cats.append('Novice')
            team_speakers.append({
                'name': s['name'], 'gender': normalize_gender(s['gender']), 'email': email,
                'phone': s['phone'], 'anonymous': bool(parse_bool_or_none(s['anonymous'])),
                'team': display, 'categories': ', '.join(cats), 'initials_match': ''})
        for dup_name, extra in dup_counts.items():
            warnings.append(f"Row {p['idx']}: '{dup_name}' is listed {extra + 1} times in team '{reference}' - kept once.")
        if not team_speakers:
            warnings.append(f"Team '{reference}' (row {p['idx']}) has no speakers listed - team imported without speakers.")
        elif min_speakers and len(team_speakers) < min_speakers:
            warnings.append(f"Team '{reference}': only {len(team_speakers)} speaker(s) found; "
                            f"{debate_format.upper()} expects at least {min_speakers}.")

        teams.append(team)
        speakers.extend(team_speakers)
        clean_rows.append((team, team_speakers))
        stats['rows_used'] += 1

    for text, n in unresolved_notes.items():
        warnings.append(f"Institution '{text}' ({n} team(s)) is not in your files: it will be matched against the "
                        f"tab site and the team imported as independent if it is not there.")
    stats['teams'], stats['speakers'] = len(teams), len(speakers)
    stats['skipped'] = dict(stats['skipped'])

    return {
        'institutions': [{'name': e['name'], 'code': e['code'], 'region': '', 'auto_code': e['auto_code']}
                         for e in registry.entries],
        'teams': teams, 'speakers': speakers, 'warnings': warnings,
        'mapping': mapping, 'mapping_rows': mapping_report(mapping),
        'ignored_columns': [clean_text(str(h).splitlines()[0]) for h in mapping['ignored']],
        'stats': stats, 'slots': slots,
        'clean_csv': standard_csv(clean_rows, max(slots + [3])),
    }


# ----------------------------------------------------------------------------
# standard layout (also used for the downloadable template)
# ----------------------------------------------------------------------------

TEAM_COLUMNS = ['institution', 'reference', 'short_reference', 'code_name',
                'use_institution_prefix', 'emoji', 'team_name (human)']
SPEAKER_FIELDS = ['name', 'gender', 'email', 'phone', 'anonymous', 'categories']


def standard_headers(slots=MAX_SPEAKER_SLOTS):
    cols = list(TEAM_COLUMNS)
    for n in range(1, slots + 1):
        cols += [f'speaker_{n}_{f}' for f in SPEAKER_FIELDS]
    return cols


def standard_csv(team_speaker_pairs, slots=MAX_SPEAKER_SLOTS):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(standard_headers(slots))
    for team, spks in team_speaker_pairs:
        row = [team['institution'], team['reference'], team['short_reference'], team['code_name'],
               'TRUE' if team['use_institution_prefix'] else 'FALSE', team['emoji'], team['team_name_human']]
        for n in range(slots):
            if n < len(spks):
                s = spks[n]
                row += [s['name'], s['gender'], s['email'], s['phone'],
                        'TRUE' if s['anonymous'] else 'FALSE', s['categories']]
            else:
                row += [''] * len(SPEAKER_FIELDS)
        w.writerow(row)
    return out.getvalue()


def template_csv():
    """A ready-to-fill template: one 2-speaker (BP), one 3-speaker and one 5-speaker (WSDC) example."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(standard_headers(MAX_SPEAKER_SLOTS))

    def spk(name, gender='', email='', phone='', anon='FALSE', cats=''):
        return [name, gender, email, phone, anon, cats]
    blank = [''] * len(SPEAKER_FIELDS)
    w.writerow(['Andhra', 'A', 'A', 'Talreja & Aditya', 'TRUE', '', 'Andhra A']
               + spk('First Speaker', 'F', 'first@example.com', '+91 9000000001') + spk('Second Speaker', 'M')
               + blank * 3)
    w.writerow(['COEP', 'B', 'B', '', 'TRUE', '', 'COEP B']
               + spk('Third Speaker', 'M', cats='Novice') + spk('Fourth Speaker', 'F') + spk('Fifth Speaker')
               + blank * 2)
    w.writerow(['', 'Free Agents', 'Free Agents', '', 'FALSE', '', 'Free Agents']
               + spk('Speaker A') + spk('Speaker B') + spk('Speaker C') + spk('Speaker D') + spk('Speaker E'))
    return out.getvalue()
