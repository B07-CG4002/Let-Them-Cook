"""Generate short-lived B07 Communications development PKI outside every Git worktree.

Only this local provisioning utility requires cryptography. Runtime peers use stdlib.
"""
import argparse
import datetime
import os
from pathlib import Path
import subprocess


def _protect_directory(folder):
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()
        # The folder is still empty: clear pre-existing explicit grants before
        # removing inherited grants, then create keys under an owner-only ACL.
        subprocess.run(["icacls", str(folder), "/reset"], check=True, capture_output=True)
        subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r",
                        identity + ":(OI)(CI)F"], check=True, capture_output=True)
    else:
        folder.chmod(0o700)


def generate_pki(output_dir):
    """Create a new CA and server identity; refuse overwrites and Git directories."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder = Path(output_dir).resolve()
    if any((parent / ".git").exists() for parent in (folder,) + tuple(folder.parents)):
        raise ValueError("PKI output must be outside Git repositories and worktrees")
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("PKI output directory must be empty; refusing to overwrite keys")
    _protect_directory(folder)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "B07 Communications Development CA")])
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    name = "ultra96.week7.internal"
    server_cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_name).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    for filename, key in (("ca-key.pem", ca_key), ("server-key.pem", server_key)):
        data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
        path = folder / filename
        with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(data)
    for filename, cert in (("ca-cert.pem", ca_cert), ("server-cert.pem", server_cert)):
        with (folder / filename).open("xb") as handle:
            handle.write(cert.public_bytes(serialization.Encoding.PEM))
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    folder = generate_pki(args.output_dir)
    print("Created B07 Communications development PKI in " + str(folder))
    print("Copy only ca-cert.pem to clients; copy server-cert.pem/server-key.pem to Ultra96.")


if __name__ == "__main__":
    main()
