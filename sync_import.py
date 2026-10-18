"""
Sync step for the Tabbycat API Importer (opt-in; used by run_batch_import in app.py).

When "Update teams and speakers already on the tab" is ticked, teams that are in your file AND already on the
tab are brought in line with the file:
  * team renamed in the file but with the same speakers  -> the existing team is renamed (not deleted + recreated)
  * emoji / code name that differ (file value not blank)  -> updated
  * speaker lineup changed                                -> the existing speaker record is RENAMED in place
                                                             (typo fixes and replacements); email / gender /
                                                             categories are updated when the file has them
  * extra speakers on the tab that are not in the file    -> left alone and reported
When "Also delete teams that are not in the file" is ticked (opt-in), teams on the tab that match no row of the file
are deleted - with safety stops (see delete_missing).

Nothing here changes TabbycatAPI; PATCH / DELETE are sent through api.session.
"""

import difflib
import re
import time

import requests


def _call(api, method, url, payload=None, retries=3):
    """PATCH / DELETE with the same retry + 0.4 s pause behaviour as TabbycatAPI._request. -> (ok, error)."""
    short = url.replace(api.base_url, '')
    for attempt in range(retries):
        try:
            time.sleep(0.4)
            headers = {}
            if getattr(api, 'auth_method', None) == 'session':       # session login needs a CSRF header for writes
                csrf = api.session.cookies.get('csrftoken')
                if csrf:
                    headers['X-CSRFToken'] = csrf
                    headers['Referer'] = api.base_url + '/'
            resp = api.session.request(method, url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201, 204):
                api.stats['success'] += 1
                return True, ''
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            error = f"HTTP {resp.status_code} on {method} {short}: {resp.text[:300]}"
            api.stats['errors'].append(error)
            api.stats['failed'] += 1
            return False, error
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                error = f"Request failed on {method} {short}: {str(e)}"
                api.stats['errors'].append(error)
                api.stats['failed'] += 1
                return False, error
            time.sleep(1)
    return False, 'rate limited (HTTP 429)'


def _plain_emoji(text):
    return re.sub('[\ufe0e\ufe0f]', '', str(text or '')).strip()


class Sync:
    def __init__(self, api, existing, norm, url_id, dry_run, allow_delete, report):
        self.api, self.norm, self.url_id = api, norm, url_id
        self.dry_run, self.allow_delete = dry_run, allow_delete
        self.log = report['sync'] = {'teams_updated': [], 'speakers_updated': [], 'removed': [],
                                     'left_alone': [], 'failed': []}
        base, slug = api.base_url, api.slug
        self.base, self.slug = base, slug

        self.team_obj, self.team_by_id = {}, {}          # team url -> API object ; team id -> team url
        for t in existing['teams']:
            if not isinstance(t, dict):
                continue
            t_id = t.get('id') if t.get('id') is not None else url_id(t.get('url'), 'teams')
            url = (t.get('url') or '').rstrip('/') or f"{base}/api/v1/tournaments/{slug}/teams/{t_id}"
            self.team_obj[url] = t
            if t_id is not None:
                self.team_by_id[t_id] = url

        self.site_speakers = {}                           # team url -> [{'name','url','email','gender','categories'}]
        for sp in existing['speakers']:
            if isinstance(sp, dict):
                t_url = self.team_by_id.get(url_id(sp.get('team'), 'teams'))
                if t_url:
                    self._add_speaker(sp, t_url)
        for t_url, t in self.team_obj.items():             # speakers nested in teams (when /speakers is unreadable)
            for sp in (t.get('speakers') or []):
                self._add_speaker(sp, t_url)

    # ------------------------------------------------------------------ helpers
    def _urls(self, values):
        out = []
        for x in values or []:
            if isinstance(x, dict):
                x = x.get('url') or x.get('id')
            if isinstance(x, int) or (isinstance(x, str) and x.isdigit()):
                x = f"{self.base}/api/v1/tournaments/{self.slug}/speaker-categories/{x}"
            if isinstance(x, str) and x:
                out.append(x.rstrip('/'))
        return out

    def _add_speaker(self, sp, team_url):
        if not isinstance(sp, dict) or not sp.get('name'):
            return
        sid = sp.get('id') if sp.get('id') is not None else self.url_id(sp.get('url'), 'speakers')
        surl = (sp.get('url') or '').rstrip('/') or (
            f"{self.base}/api/v1/tournaments/{self.slug}/speakers/{sid}" if sid is not None else '')
        lst = self.site_speakers.setdefault(team_url, [])
        if surl and any(r['url'] == surl for r in lst):
            return
        if not surl and any(self.norm(r['name']) == self.norm(sp['name']) for r in lst):
            return
        lst.append({'name': sp['name'], 'url': surl, 'email': sp.get('email') or '',
                    'gender': sp.get('gender') or '', 'categories': self._urls(sp.get('categories'))})

    def team_label(self, url):
        t = self.team_obj.get(url, {})
        return t.get('short_name') or t.get('long_name') or t.get('reference') or url

    def _categories(self, category_map):
        by_name = {k.casefold(): v for k, v in (category_map or {}).items() if v}
        if not by_name:
            data = self.api.get_speaker_categories()
            if isinstance(data, list):
                for cat in data:
                    if isinstance(cat, dict) and cat.get('name'):
                        url = (cat.get('url') or '').rstrip('/') or (
                            f"{self.base}/api/v1/tournaments/{self.slug}/speaker-categories/{cat['id']}"
                            if 'id' in cat else '')
                        if url:
                            by_name[cat['name'].casefold()] = url
        return by_name

    # ------------------------------------------------------------------ 1. renamed teams
    def detect_renames(self, teams, speakers, team_index, resolve_institution):
        """
        A team in the file that is not on the tab, whose institution + FULL speaker lineup equals that of exactly one
        tab team that matches nothing in the file, is the same team under a new name.  -> {key: site team url}
        """
        norm = self.norm
        names_by_team = {}
        for sp in speakers:
            names_by_team.setdefault(norm(sp['team']), set()).add(norm(sp['name']))
        matched_urls, unmatched = set(), []
        for team in teams:
            inst_id, _, status = resolve_institution(team['institution'])
            if status == 'missing':
                continue
            inst_id = inst_id if status == 'ok' else None
            key = (inst_id, norm(team['reference']))
            if key in team_index:
                matched_urls.add(team_index[key])
                continue
            default_name = f"{team['institution'] or 'Independent'} {team['reference']}"
            names = names_by_team.get(norm(team['team_name_human'])) or names_by_team.get(norm(default_name)) or set()
            unmatched.append((key, inst_id, names))
        free = []
        for url, t in self.team_obj.items():
            if url in matched_urls:
                continue
            names = {norm(r['name']) for r in self.site_speakers.get(url, [])}
            free.append((url, self.url_id(t.get('institution'), 'institutions'), names))
        renames, claimed = {}, set()
        for key, inst_id, names in unmatched:
            if not names:
                continue
            hits = [u for u, i, n in free if i == inst_id and n == names and u not in claimed]
            if len(hits) == 1:
                renames[key] = hits[0]
                claimed.add(hits[0])
        return renames

    # ------------------------------------------------------------------ 2. update existing teams / speakers
    def sync_existing(self, matched, speakers, team_url_of, category_map, speaker_names):
        cats = self._categories(category_map)
        per_team = {}
        for sp in speakers:
            url = team_url_of(sp['team'])
            if url:
                per_team.setdefault(url, []).append(sp)
        for team, url, renamed in matched:
            if url not in self.team_obj:
                continue
            self._sync_team(team, url, renamed)
            self._sync_speakers(url, per_team.get(url, []), cats, speaker_names)

    def _sync_team(self, team, url, renamed):
        norm, obj = self.norm, self.team_obj[url]
        payload, changes = {}, []
        label = self.team_label(url)
        if renamed and norm(obj.get('reference')) != norm(team['reference']):
            payload['reference'] = team['reference']
            changes.append(f"renamed '{obj.get('reference')}' \u2192 '{team['reference']}'")
            if team.get('short_reference') and (obj.get('short_reference') or '') != team['short_reference']:
                payload['short_reference'] = team['short_reference']
        emoji = (team.get('emoji') or '').strip()
        if emoji and _plain_emoji(obj.get('emoji')) != _plain_emoji(emoji):
            payload['emoji'] = emoji
            changes.append(f"emoji {emoji}")
        code = (team.get('code_name') or '').strip()
        if code and (obj.get('code_name') or '').strip() != code:
            payload['code_name'] = code
            changes.append(f"code name '{code}'")
        if not payload:
            return
        if not self.dry_run:
            ok, err = _call(self.api, 'PATCH', url, payload)
            if not ok:
                self.log['failed'].append((f"Team {label}", err))
                return
            obj.update(payload)
        self.log['teams_updated'].append(f"{label}: " + ', '.join(changes))

    def _pair(self, f_only, s_only):
        norm = self.norm
        scored = sorted(((difflib.SequenceMatcher(None, norm(a['name']), norm(b['name'])).ratio(), i, j)
                         for i, a in enumerate(f_only) for j, b in enumerate(s_only)), reverse=True)
        used_f, used_s, pairs = set(), set(), []
        for _, i, j in scored:
            if i not in used_f and j not in used_s:
                used_f.add(i)
                used_s.add(j)
                pairs.append((f_only[i], s_only[j]))
        return (pairs, [x for i, x in enumerate(f_only) if i not in used_f],
                [x for j, x in enumerate(s_only) if j not in used_s])

    def _sync_speakers(self, url, file_sps, cats, speaker_names):
        norm = self.norm
        site = self.site_speakers.get(url, [])
        if not site or not file_sps:
            return
        label = self.team_label(url)
        site_by_name = {norm(s['name']): s for s in site}
        kept, f_only, used = [], [], set()
        for f in file_sps:
            s = site_by_name.get(norm(f['name']))
            if s and norm(f['name']) not in used:
                used.add(norm(f['name']))
                kept.append((f, s, False))
            else:
                f_only.append(f)
        s_only = [s for s in site if norm(s['name']) not in used]
        pairs, _, left = self._pair(f_only, s_only)
        todo = kept + [(f, s, True) for f, s in pairs]
        for s in left:
            self.log['left_alone'].append(f"{label}: speaker '{s['name']}' is on the tab but not in your file (not removed)")

        for f, s, rename in todo:
            payload, changes = {}, []
            if rename:
                payload['name'] = f['name']
                changes.append(f"renamed '{s['name']}' \u2192 '{f['name']}'")
            if f.get('email') and norm(f['email']) != norm(s['email']):
                payload['email'] = f['email']
                changes.append('email')
            if f.get('gender') and f['gender'] != s['gender']:
                payload['gender'] = f['gender']
                changes.append('gender')
            add_urls, new_cats, bad = [], [], []
            for c in [c.strip() for c in re.split(r'[;,]', f.get('categories') or '') if c.strip()]:
                cat_url = cats.get(c.casefold())
                if cat_url:
                    if cat_url not in s['categories'] and cat_url not in add_urls:
                        add_urls.append(cat_url)
                        changes.append(f"category {c}")
                elif self.dry_run:
                    new_cats.append(c)
                    changes.append(f"category {c} (new)")
                else:
                    bad.append(c)
            if add_urls:
                payload['categories'] = list(s['categories']) + add_urls
            for c in bad:
                self.log['failed'].append((f"{label}: {f['name']}", f"category '{c}' does not exist on the tab"))
            if not payload and not new_cats:
                continue
            who = f"{label}: {s['name']}" if not rename else f"{label}"
            if not self.dry_run and payload:
                if not s['url']:
                    self.log['failed'].append((who, 'the tab does not report an id/url for this speaker, so it '
                                                    'cannot be updated'))
                    continue
                ok, err = _call(self.api, 'PATCH', s['url'], payload)
                if not ok:
                    self.log['failed'].append((who, err))
                    continue
                if 'categories' in payload:
                    s['categories'] = payload['categories']
            if rename:
                names = speaker_names.setdefault(url, set())
                names.discard(norm(s['name']))
                names.add(norm(f['name']))
                s['name'] = f['name']
            for key in ('email', 'gender'):
                if key in payload:
                    s[key] = payload[key]
            self.log['speakers_updated'].append(f"{who}: " + ', '.join(changes))

    # ------------------------------------------------------------------ 3. delete teams (opt-in)
    def delete_missing(self, matched_urls, had_team_rows, failed_rows):
        if not self.allow_delete:
            return
        if not had_team_rows:
            self.log['left_alone'].append('Delete teams: skipped, because no teams were uploaded.')
            return
        gone = [u for u in self.team_obj if u not in matched_urls]
        if not gone:
            return
        names = [self.team_label(u) for u in gone]
        if failed_rows:
            self.log['left_alone'].append(
                f"Delete teams: skipped, because {failed_rows} team row(s) of your file failed to import "
                f"(fix them first, otherwise their teams could be deleted by mistake). Not deleted: " + ', '.join(names))
            return
        if len(gone) >= 4 and len(gone) > len(self.team_obj) / 2:
            self.log['left_alone'].append(
                f"Delete teams: skipped, because {len(gone)} of the {len(self.team_obj)} teams on the tab are not in "
                f"your file (looks like a partial file). Not deleted: " + ', '.join(names))
            return
        for url, name in zip(gone, names):
            if self.dry_run:
                self.log['removed'].append(name)
                continue
            ok, err = _call(self.api, 'DELETE', url)
            if ok:
                self.log['removed'].append(name)
            else:
                self.log['failed'].append((f"Delete team {name}", err))
