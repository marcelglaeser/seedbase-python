from __future__ import annotations

import ssl

CERTIFI_HINT = (
    "TLS certificate verification failed. On macOS the system Python often "
    "ships without CA certificates. Install the bundled CA roots with "
    "'pip install seedbase[certs]' (or 'pip install certifi') and try again."
)


def create_ssl_context() -> ssl.SSLContext:
    """Return a verifying SSL context.

    Uses certifi's CA bundle when the (optional) certifi package is installed,
    which resolves the macOS 'CERTIFICATE_VERIFY_FAILED' problem with
    python.org builds. Verification is never disabled.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def is_cert_verify_error(exc: BaseException | None) -> bool:
    """Whether the exception (or its cause/reason) is a TLS certificate failure.

    urllib wraps the underlying SSLError in a URLError and exposes it via the
    'reason' attribute, so that chain is followed too.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
        reason = getattr(exc, "reason", None)
        nxt = reason if isinstance(reason, BaseException) else None
        exc = nxt or exc.__cause__ or exc.__context__
    return False
