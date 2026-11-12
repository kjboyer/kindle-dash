-- Edit this file with any text editor (Kindle mounted as a USB drive).
return {
    -- Full URL of the folder that contains manifest.json (no trailing slash).
    base_url = "https://YOUR-USER.github.io/YOUR-REPO/YOUR-SECRET-PATH",
    -- Sync on wake only if the last successful sync is older than this.
    max_age_hours = 20,
    -- Where the pages are saved on the Kindle.
    dest = "/mnt/us/dashboards",
    -- After a failed attempt, wait this long before trying again on wake.
    retry_minutes = 45,
    -- Turn Wi-Fi back off after syncing if the plugin had to turn it on.
    wifi_off_after = true,
}
