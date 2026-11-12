# Kindle daily dashboard

A GitHub Action builds four e-ink pages every morning (Today, Week, Calendar, News) and
publishes them to GitHub Pages under a secret path. The KOReader plugin in `kindle/` downloads
them when the Kindle wakes up and the last sync is older than ~20 hours.

Data: NWS forecast, NDBC buoy 46225 (surf), NOAA tides 9410230, Hacker News, your Google Calendar (private iCal link).

## One-time setup (about 10 minutes)

1. **Create a public GitHub repo** (Pages is free only for public repos), e.g. `kindle-dash`, and upload
   everything in this folder (drag and drop into the web UI works; keep the `.github` folder).
   The page contents are public, but live under a long random URL nobody can guess.
2. **Enable Pages**: repo > Settings > Pages > Source = **GitHub Actions**.
3. **Make a random secret path** in PowerShell and copy the output:
   `-join ((48..57)+(97..122) | Get-Random -Count 32 | % {[char]$_})`
4. *(Optional — skip for now)* **Get your calendar link**: Google Calendar (web) > Settings > your calendar > Integrate calendar >
   **Secret address in iCal format**. Copy it.
5. **Add the secret(s)**: repo > Settings > Secrets and variables > Actions > New repository secret:
   - `SECRET_PATH` = the random string from step 3
   - `CAL_ICS_URL` = the iCal link from step 4 (optional; without it the Calendar page is simply left out. Add it later and re-run the workflow.)
6. **Run it once**: Actions tab > "Build Kindle dashboard" > Run workflow. Takes ~1 minute.
7. Your pages live at `https://<your-github-user>.github.io/<repo>/<SECRET_PATH>/manifest.json`.
   Open it in a browser to confirm.
8. Edit `kindle/dashsync.koplugin/config.lua` with that folder URL (without `manifest.json`)
   and copy the `dashsync.koplugin` folder to `koreader/plugins/` on the Kindle.

After that it is hands-off: the Action refreshes daily, and the Kindle pulls on wake.
Menu > Tools > "Sync dashboard now" forces an update.

Treat the secret iCal address like a password; if it leaks, reset it in Google Calendar.
