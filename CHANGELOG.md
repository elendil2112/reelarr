# Changelog

All notable changes to Reelarr. Versions follow [Semantic Versioning](https://semver.org/);
until 1.0, minor versions may change behaviour, and every upgrade backs up your
databases first.

## Unreleased

### Track titles
- **Fixed: titles one track off when a show starts with a tuning track.**
  setlists don't list tuning, crowd or break tracks, so a show with one more
  file than songs had every title shifted. Reelarr now sets those files aside
  before pairing titles with files: a file whose own title or name says so
  ("Tuning", "Crowd", "Set Break"…), or a short file (under 90 s) at the start,
  the end, or between discs. They're titled by where they sit — Tuning, Set
  Break, Encore Break, Crowd — or keep the label they already had.
- When it can't tell which file isn't a song, the show waits in Review
  instead of being filed with shifted titles. This now applies to setlist.fm
  setlists too, which used to skip the check.
- **Review → Track titles** lists every file with its length and the title
  it would get, with a *not a song* tick box; File it writes exactly what's shown.
- archive.org's per-file titles (which include tuning tracks) are used when
  they cover every file and the setlist doesn't.
- Fixed: when counts didn't match, track numbers were read from any digits in
  a file name — the `1` of `d1t05` gave the last file the first song's title.
- Tuning, Crowd and the like are no longer learned as songs.

### Notes
- A new **Notes** page for your own notes — tapes to chase, trades in
  progress, which source you liked. Saves as you type, when you switch notes
  and when you leave the page. Pin notes to the top, search them, and undo a
  delete. If the same note is edited in two windows, neither silently
  overwrites the other: you choose which version to keep.
- Notes live in `reelarr.db`, so they're in your backups. Deleted notes are
  kept for 30 days, then cleared.

### Queue
- Every Queue table — Wanted, Downloading, Coming in, Recently grabbed, Passed
  over — now sorts by any column: click a heading, click again to reverse.
  Your choice is remembered per table.
- A search box filters all five tables at once. Every word has to match
  somewhere in the row (artist, show, date, source, status, reason…); Esc clears it.
- Recently grabbed shows each show's status, progress and ETA from the
  download client, and keeps up to 100 grabs instead of 15.

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
