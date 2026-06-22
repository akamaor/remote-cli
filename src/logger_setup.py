import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_loggers(log_dir: str) -> tuple:
    """
    Returns (app_logger, audit_logger).

    app.log  — general lifecycle events, errors, startup/shutdown.
    audit.log — security decisions: executed commands (no stdout), rejections,
                timeouts. This is the forensic trail.
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        print(f"[FATAL] Cannot create log directory {log_dir!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    def _rotating(filename: str) -> RotatingFileHandler:
        handler = RotatingFileHandler(
            os.path.join(log_dir, filename),
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        return handler

    # ---- App logger ----
    app = logging.getLogger("remote_cli.app")
    app.setLevel(logging.INFO)
    app.addHandler(_rotating("app.log"))
    # Mirror to stderr so systemd/journald picks it up via StandardError=journal
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    app.addHandler(stderr_handler)
    app.propagate = False

    # ---- Audit logger ----
    audit = logging.getLogger("remote_cli.audit")
    audit.setLevel(logging.INFO)
    audit.addHandler(_rotating("audit.log"))
    # Mirror to stderr so systemd/journald picks it up
    audit_stderr = logging.StreamHandler(sys.stderr)
    audit_stderr.setFormatter(formatter)
    audit.addHandler(audit_stderr)
    audit.propagate = False

    return app, audit
