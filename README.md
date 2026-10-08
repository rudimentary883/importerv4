# Tabbycat API Importer

Web service that imports tournament data into Tabbycat via REST API or generates CSVs.

## Render Free Tier

- 512MB RAM safe
- Single worker, 2 threads
- 120s timeout

## Two Modes

1. Download CSVs - for manual import
2. Direct API Import - connects to Tabbycat automatically

## Supported Formats

| Format | Value | Speakers imported per team |
|--------|-------|----------------------------|
| British Parliamentary (BP) | `bp` | first 2 |
| 3v3 (Australs, UADC, etc.) | `3v3` | first 3 |
| World Schools (WSDC) | `wsdc` | first 5 (warns if a team has fewer than 3) |

Extra speakers beyond the limit are skipped and reported in the results page.
List main speakers first and reserves after them in the speakers file.


## Importing in batches (v4.0)

In **Direct API Import** mode the importer first reads what is already on the tab site, then imports only what is new.

- Existing institutions (matched by code or name, ignoring case and extra spaces), teams (institution + reference),
  judges (name + institution, or same email) and speakers (name on the same team) are **skipped**, never duplicated.
- Every file is optional. Teams and judges can use institutions that are already on the site without uploading the institutions file.
- You can re-upload the **same, updated .csv files** for the 2nd, 3rd, 4th... batch, or upload only the new rows.
- Speakers can be added to teams that already exist on the site; the per-format speaker limit counts the speakers already there.
- Tick **Preview only** to see what would be imported without importing anything.
- If the existing entries cannot be read (for example the token has no admin access), nothing is imported.
- The results page lists what was new, what already existed, and what failed (with the reason).


## Teams + Speakers in one file (v5.0)

Upload **one row per team with its speakers side by side** instead of separate Teams and Speakers files.
Use it alone or together with the separate files; everything is imported through the same batch-aware logic
(existing entries are skipped, only new ones are imported).

- **Standard layout** (download the template from the importer): `institution, reference, short_reference, code_name,
  use_institution_prefix, emoji, team_name (human)`, then `speaker_N_name, speaker_N_gender, speaker_N_email,
  speaker_N_phone, speaker_N_anonymous, speaker_N_categories` for N = 1..5.
- **Google Form / registration sheets** are read as they are: headers such as `Name of Speaker 1`,
  `Email Address of Speaker 1`, `Phone Number (WhatsApp) of Speaker 1`, `Gender of Speaker 1`,
  `Does Speaker 1 qualify as Novice?`, `Team Name`, `Full Name of Institution`, `Abbreviation for Institution`,
  `Institution of Speaker 1` are detected automatically. The results page shows how every column was understood.
- **Blank speaker columns are fine**: a WSDC team with only speakers 1-3 filled in is read as a 3-speaker team.
  Format limits still apply (BP 2, 3v3 3, WSDC 5; WSDC warns below 3).
- Rows that are not teams (e.g. adjudicator registrations) are skipped and counted.
- Institutions: `Full Name` + `Abbreviation` columns create institutions (matched to existing ones by name/code, ignoring
  case and punctuation). A team whose speakers all share one institution gets that institution; mixed institutions
  become an independent (composite) team. Options decide what happens to an institution that is in none of your files.
- Gender answers such as `Cis Male / Cis Female / Non-Binary / Agender / Prefer Not Say` become M / F / O / blank;
  `Novice? Yes` can become the speaker category `Novice`; invalid emails are dropped with a warning (the speaker is still imported).
- Needs `openpyxl` in `requirements.txt` to read `.xlsx` files.

## API Requirements

- Tabbycat URL
- API Token (from Change Password page)
- Tournament Slug

## Deploy to Render

1. Push this repo to GitHub
2. Create Web Service on Render
3. Build: pip install -r requirements.txt
4. Start: gunicorn app:app --workers 1 --threads 2 --timeout 120
