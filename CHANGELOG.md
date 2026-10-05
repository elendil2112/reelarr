# Changelog

All notable changes to Reelarr. Versions follow [Semantic Versioning](https://semver.org/);
until 1.0, minor versions may change behaviour, and every upgrade backs up your
databases first.

## 0.1.0 — first public release

### Identify, tag and file
- Reads everything a live recording comes with — etree-style folder names, info
  files, embedded tags, SHNIDs — and checks it against setlist.fm and archive.org,
  keeping the provenance of every value.
- Works out the source: SBD, AUD, MTX, FM, front-of-board; optional crowd-noise
  analysis when nothing says.
- Names and tags every show the same way: `Artist/YYYY-MM-DD Venue, City, ST [SOURCE]`.
- **Places:** a built-in world city list (GeoNames). US states and Canadian
  provinces are two-letter codes; everywhere else the country is written in full
  in place of the state (`Paradiso, Amsterdam, Netherlands`). Codes are read
  against the city — `Berlin, DE` is Germany, `Dover, DE` is Delaware — and a
  missing state is filled when the city is unambiguous. Country spellings are
  yours to change.
- **Early and late shows** of one night are filed as one show: both folders kept
  inside it, the early show as disc 1 and the late as disc 2, and one album whose
  bracket carries both sources (`[SBD 082964 TinyDancer & AUD Miller]`).
- Converts SHN and WAV to FLAC without losing bit depth or sample rate.
- Anything uncertain waits in **Review**; corrections become rules for future shows,
  and your existing library teaches it your artists, venues and song titles.
- Official releases (the word "Official", plus any labels you name in Settings)
  are tagged as such and never replaced by fan recordings.

### An *arr for live music
- **Artists:** follow an artist and Reelarr watches the Live Music Archive for new
  uploads you don't have, in the sources and formats you'd accept — asking you
  first or grabbing automatically. Look back 30 days, a year, or everything;
  every check explains itself; stream-only items are recognised and skipped.
- **Discover:** search the Live Music Archive and grab shows.
- **Queue:** wanted releases, what was passed over and why, and a sortable
  download table (client order, progress, speed, ETA, seeds, ratio…).
- **Download clients:** Deluge, qBittorrent and Transmission, with a path-mapping
  helper; finished downloads come back through the watch folder automatically.

### Safe by design
- Starts in **dry run**: every change is planned and shown before any is made.
- **Nothing is deleted outright** — removed files go to a trash folder for 30 days —
  and every change can be undone from Activity.
- Database migrations with automatic backups; refuses to start on a database
  from a newer version.

### Security
- Login by default (scrypt, HttpOnly SameSite cookies, throttling), API key for
  other apps, secrets masked in the UI and redacted from logs.
- Strict Content-Security-Policy and related headers; the folder browser can't
  see the config folder; hardened `.torrent` parsing; archive extraction with
  size and zip-bomb limits, and programs and links removed from downloads.
- The container drops root and runs as `PUID`/`PGID`.

### Install
- Docker with no `.env` required, multi-arch image (amd64/arm64), an unRAID
  template, or bare metal with Python 3.10+.
- A setup wizard for the login, folders, download client, setlist.fm and start-up
  mode — every setting is in the UI.
- Add-ons ("providers") can bring shows in from other sources; see `docs/providers.md`.
