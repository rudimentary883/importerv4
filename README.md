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

## API Requirements

- Tabbycat URL
- API Token (from Change Password page)
- Tournament Slug

## Deploy to Render

1. Push this repo to GitHub
2. Create Web Service on Render
3. Build: pip install -r requirements.txt
4. Start: gunicorn app:app --workers 1 --threads 2 --timeout 120
