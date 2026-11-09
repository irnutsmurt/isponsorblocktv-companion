"""Self-signed certificate generation for the built-in HTTPS option."""

from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .logging import get_logger

log = get_logger("tls")


def ensure_self_signed(directory: Path, public_origin: str | None) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    cert_path, key_path = directory / "selfsigned.crt", directory / "selfsigned.key"
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=14):
                return cert_path, key_path
        except Exception:
            pass
    host = urlsplit(public_origin).hostname if public_origin else None
    names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    if host:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host or "isponsorblocktv-webui")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("tls.selfsigned.created", "Generated a self-signed certificate", subject=host or "localhost", valid_days=825)
    return cert_path, key_path
