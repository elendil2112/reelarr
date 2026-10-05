"""Run Reelarr:  python -m app

Listens on REELARR_HOST:REELARR_PORT.
  host default: 127.0.0.1 on bare metal (only this computer can connect),
                0.0.0.0 inside the Docker image (Docker's port mapping decides
                who can reach it)
  port default: 8189

To reach a bare-metal install from other machines, set REELARR_HOST=0.0.0.0
deliberately — and keep the login on.
"""
import ipaddress
import os
import sys

from . import paths


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _RedactAccessLog:
    """uvicorn's access log prints the full URL, including ?apikey=…
    Scrub it the same way Reelarr's own log is scrubbed."""
    def filter(self, record):
        try:
            from .database import redact
            if record.args:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            record.msg = redact(str(record.msg))
        except Exception:
            pass
        return True


def main():
    import logging
    import uvicorn
    logging.getLogger("uvicorn.access").addFilter(_RedactAccessLog())
    host = os.environ.get("REELARR_HOST") or ("0.0.0.0" if paths.in_container() else "127.0.0.1")
    try:
        port = int(os.environ.get("REELARR_PORT") or 8189)
    except ValueError:
        sys.exit("REELARR_PORT must be a number")
    if not _is_loopback(host) and not paths.in_container():
        print(f"[reelarr] listening on {host}:{port} — reachable from other machines. "
              f"Make sure the login is on (Settings → Security).", file=sys.stderr, flush=True)
    uvicorn.run("app.main:app", host=host, port=port, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("REELARR_TRUSTED_PROXIES", "127.0.0.1"))


if __name__ == "__main__":
    main()
