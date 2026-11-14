"""Validate the corporate TLS certificate before bd-live-proxy serves it.

    python -m bdlive.certcheck <portal.crt> <portal.key> <fqdn>

Checks: PEM parses, the private key matches the certificate, the FQDN is in the SAN,
the certificate is currently valid (and warns inside 30 days of expiry), and the file
carries the intermediate chain. Never prints key material. Exit 0 = usable.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
from datetime import UTC, datetime


def _decode(path: str) -> dict:
    # CPython helper used by the stdlib test-suite; parses the first certificate in a PEM.
    return ssl._ssl._test_decode_cert(path)  # noqa: SLF001


def _when(value: str) -> datetime:
    return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)


def check(crt: str, key: str, fqdn: str) -> dict:
    result = {"certificate": crt, "fqdn": fqdn, "ok": False, "errors": [], "warnings": []}
    try:
        pem = open(crt, encoding="ascii").read()
    except (OSError, UnicodeDecodeError) as exc:
        result["errors"].append(f"cannot read certificate: {type(exc).__name__}")
        return result
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----", pem)
    result["chain_length"] = len(blocks)
    if not blocks:
        result["errors"].append("no PEM certificate found (expected -----BEGIN CERTIFICATE-----)")
        return result
    try:
        info = _decode(crt)
    except (ssl.SSLError, OSError, ValueError) as exc:
        result["errors"].append(f"certificate does not parse: {type(exc).__name__}")
        return result
    subject = dict(x[0] for x in info.get("subject", ()))
    issuer = dict(x[0] for x in info.get("issuer", ()))
    sans = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"]
    not_before, not_after = _when(info["notBefore"]), _when(info["notAfter"])
    now = datetime.now(UTC)
    result.update(subject_cn=subject.get("commonName"), issuer_cn=issuer.get("commonName"),
                  san=sans, not_before=not_before.isoformat(), not_after=not_after.isoformat())

    def covered(name: str) -> bool:
        name = name.lower()
        for san in (s.lower() for s in sans):
            if san == name or (san.startswith("*.") and name.split(".", 1)[-1] == san[2:]):
                return True
        return False

    if not covered(fqdn):
        result["errors"].append(f"{fqdn} is not in the certificate SAN {sans}")
    if now < not_before:
        result["errors"].append("certificate is not valid yet")
    if now > not_after:
        result["errors"].append("certificate has expired")
    elif (not_after - now).days < 30:
        result["warnings"].append(f"certificate expires in {(not_after - now).days} days")
    if subject == issuer:
        result["errors"].append("certificate is self-signed; a corporate-CA-issued "
                                "certificate is required for shared access")
    if len(blocks) < 2:
        result["warnings"].append("only one certificate in the file; append the corporate "
                                  "intermediate CA(s) unless clients already trust them")
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)  # raises if the key does not match the certificate
    except ssl.SSLError:
        result["errors"].append("private key does not match the certificate (or is "
                                "encrypted/unsupported)")
    except OSError as exc:
        result["errors"].append(f"cannot read private key: {type(exc).__name__}")
    result["ok"] = not result["errors"]
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    result = check(*argv)
    print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
