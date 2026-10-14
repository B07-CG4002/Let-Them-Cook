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


SERVER_NAME = "ultra96.week7.internal"


def _new_output_folder(output_dir):
    folder = Path(output_dir).resolve()
    if any((parent / ".git").exists() for parent in (folder,) + tuple(folder.parents)):
        raise ValueError("PKI output must be outside Git repositories and worktrees")
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("PKI output directory must be empty; refusing to overwrite keys")
    _protect_directory(folder)
    return folder


def _server_certificate(server_key, ca_key, issuer, now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    return (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_NAME)]))
        .issuer_name(issuer).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(SERVER_NAME)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))


def _write_private_key(path, key):
    from cryptography.hazmat.primitives import serialization

    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)


def _write_certificate(path, cert):
    from cryptography.hazmat.primitives import serialization

    with path.open("xb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))


def renew_server_identity(ca_dir, output_dir):
    """Issue a new server key and certificate from an existing CA; the CA itself is unchanged.

    Clients keep trusting the same ca-cert.pem, so the fingerprint enrolled in the phone app stays valid.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    source = Path(ca_dir).resolve()
    ca_pem = (source / "ca-cert.pem").read_bytes()
    ca_cert = x509.load_pem_x509_certificate(ca_pem)
    ca_key = serialization.load_pem_private_key((source / "ca-key.pem").read_bytes(), password=None)
    if ca_key.public_key().public_numbers() != ca_cert.public_key().public_numbers():
        raise ValueError("ca-key.pem does not belong to ca-cert.pem")
    now = datetime.datetime.now(datetime.timezone.utc)
    if now + datetime.timedelta(days=30) > ca_cert.not_valid_after_utc:
        raise ValueError("the CA expires before a new server certificate would; generate a new PKI")
    folder = _new_output_folder(output_dir)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = _server_certificate(server_key, ca_key, ca_cert.subject, now)
    _write_private_key(folder / "server-key.pem", server_key)
    _write_certificate(folder / "server-cert.pem", server_cert)
    with (folder / "ca-cert.pem").open("xb") as handle:
        handle.write(ca_pem)
    return folder


def generate_pki(output_dir):
    """Create a new CA and server identity; refuse overwrites and Git directories."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    folder = _new_output_folder(output_dir)
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
    server_cert = _server_certificate(server_key, ca_key, ca_name, now)
    _write_private_key(folder / "ca-key.pem", ca_key)
    _write_private_key(folder / "server-key.pem", server_key)
    _write_certificate(folder / "ca-cert.pem", ca_cert)
    _write_certificate(folder / "server-cert.pem", server_cert)
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--renew-server-from", metavar="CA_DIR",
                        help="folder holding the existing ca-cert.pem and ca-key.pem; issue only a new server "
                             "certificate from that CA instead of creating a new CA")
    args = parser.parse_args()
    if args.renew_server_from:
        folder = renew_server_identity(args.renew_server_from, args.output_dir)
        print("Issued a new server certificate from the existing CA in " + str(folder))
        print("Clients keep their ca-cert.pem; copy server-cert.pem/server-key.pem to Ultra96.")
        return
    folder = generate_pki(args.output_dir)
    print("Created B07 Communications development PKI in " + str(folder))
    print("Copy only ca-cert.pem to clients; copy server-cert.pem/server-key.pem to Ultra96.")


if __name__ == "__main__":
    main()
