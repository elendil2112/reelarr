# Security

## Reporting a problem

Please report security problems **privately**, not as a public issue:
on GitHub, open the **Security** tab of this repository and choose
**Report a vulnerability**. You'll get a reply within a week. Fixes are
released as soon as they're ready, and credited to you unless you'd rather not.

## Supported versions

Only the latest release gets security fixes. Reelarr is pre-1.0; upgrading is
`docker compose pull && docker compose up -d` (or rebuild), and every upgrade
backs up your databases first.

## How Reelarr protects your setup

- **Login required** by default (scrypt-hashed passwords, HttpOnly SameSite
  session cookies, throttled login attempts). Turning it off is an explicit
  choice in setup, meant for use behind your own SSO.
- **API key** for other apps, sent as `X-Api-Key`.
- **Secrets stay put:** passwords and keys are masked in the UI and API,
  redacted from logs, and `settings.json` is written readable only by its
  owner. Backups contain settings — keep them private.
- **Strict browser headers:** a Content-Security-Policy with no third-party
  scripts, no framing, no MIME sniffing.
- **The folder browser** sees only the folders you mounted, never the config folder.
- **Downloads are handled defensively:** archives that would unpack past a size
  limit, look like zip bombs, or reach outside their folder are refused;
  programs and links found inside are removed; malformed `.torrent` files fail
  cleanly.
- **Nothing is deleted outright:** removed files go to a trash folder for 30
  days, and every change can be undone.
- **The container doesn't run as root:** the entrypoint drops to `PUID`/`PGID`.

If you expose Reelarr beyond your home network, put it behind a reverse proxy
with HTTPS.
