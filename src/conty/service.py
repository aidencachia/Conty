"""Owner-local service startup and per-user instance locking."""

from __future__ import annotations

import fcntl
import logging
import os
import ssl
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from conty.config import ConfigurationError, Diagnostic, load_config


@contextmanager
def instance_lock(directory=None):
    directory = (
        Path(directory) if directory else Path(tempfile.gettempdir()) / f"conty-{os.getuid()}"
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("Conty runtime directory must be owner-only and must not be a symlink")
    descriptor = os.open(directory / "host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("Conty instance lock must be an owner-only regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OSError("Another Conty service is already running for this user") from exc
        yield
    finally:
        os.close(descriptor)


def serve(path, *, ssl_certfile=None, ssl_keyfile=None):
    import qrcode
    import uvicorn

    from conty.server import connection_urls, create_app

    snapshot = load_config(path)
    if bool(ssl_certfile) != bool(ssl_keyfile):
        raise ConfigurationError(
            [Diagnostic("server", "Use both --ssl-certfile and --ssl-keyfile for HTTPS")]
        )
    try:
        if ssl_certfile:
            ssl_certfile = str(Path(ssl_certfile).expanduser().resolve())
            ssl_keyfile = str(Path(ssl_keyfile).expanduser().resolve())
            # Fail before advertising a URL or requesting pairing with invalid TLS material.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(ssl_certfile, ssl_keyfile, password=lambda: "")
        with instance_lock():
            logger = logging.getLogger("conty")
            if not logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.info("service_starting revision=%s", snapshot.revision)
            urls = connection_urls(snapshot, scheme="https" if ssl_certfile else "http")
            for url in urls:
                print(f"Dashboard: {url}")
            qr = qrcode.QRCode(border=2)
            qr.add_data(urls[0])
            qr.print_ascii(tty=False, invert=True)
            if ssl_certfile:
                print(
                    "HTTPS: use a certificate trusted by your clients. Ctrl-C revokes all sessions.",
                    flush=True,
                )
            else:
                print(
                    "Trusted-LAN HTTP: traffic is unencrypted. "
                    "Ctrl-C stops the host and revokes all sessions.",
                    flush=True,
                )
            uvicorn.run(
                create_app(snapshot, owner_output=lambda line: print(line, flush=True)),
                host=snapshot.server["bind"],
                port=snapshot.server["port"],
                workers=1,
                proxy_headers=False,
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
                access_log=False,
                limit_concurrency=128,
                backlog=128,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=10,
            )
    except OSError as exc:
        raise ConfigurationError([Diagnostic("server", str(exc))]) from exc
