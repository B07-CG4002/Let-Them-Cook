#!/usr/bin/env python3
"""Generate isolated, short-lived PKI for the iOS transport XCTest bundle.

These credentials are only for loopback test peers. Never enroll them in the
app, upload them, or commit the generated directory. OpenSSL output is hidden.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


REPOSITORY = Path(__file__).resolve().parents[3]
MARKER = ".comms-test-pki-owner"
OWNER = "CommsNativePreview transport XCTest PKI; generated fixture version 1\n"
FILES = {"ca.pem", "server.pem", "server.key"}
VARIANTS = (
    [(f"valid-{index:02d}", "ultra96.week7.internal", False) for index in range(8)]
    + [(f"wrong-host-{index:02d}", "wrong.week7.internal", False) for index in range(2)]
    + [(f"expired-{index:02d}", "ultra96.week7.internal", True) for index in range(2)]
)


def openssl(directory: Path, *arguments: str) -> None:
    """Bounded command with no certificate/key bytes or tool output exposed."""
    subprocess.run(
        ["/usr/bin/openssl", *arguments], cwd=directory,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=True, timeout=30,
    )


def generate_fixture(root: Path, name: str, hostname: str, expired: bool) -> None:
    destination = root / name
    destination.mkdir(mode=0o700)
    # Issuer keys and signing metadata are never copied into the test bundle.
    with tempfile.TemporaryDirectory(prefix=".issuer-", dir=root) as staging:
        issuer = Path(staging)
        (issuer / "config").write_text(f"""[req]
distinguished_name=dn
prompt=no
[dn]
CN=Comms isolated test authority {name}
[ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
[server]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:{hostname}
[issuer]
database=index
serial=serial
private_key=ca.key
certificate=ca.pem
new_certs_dir=.
default_md=sha256
policy=subject_policy
[subject_policy]
commonName=supplied
""", encoding="utf-8")
        openssl(issuer, "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", "ca.key", "-out", "ca.pem", "-days", "1", "-config", "config", "-extensions", "ca")
        openssl(issuer, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
                "-out", "server.csr", "-config", "config", "-subj", f"/CN={hostname}")
        if expired:
            (issuer / "index").write_text("", encoding="ascii")
            (issuer / "serial").write_text("01\n", encoding="ascii")
            openssl(issuer, "ca", "-batch", "-config", "config", "-name", "issuer", "-in", "server.csr",
                    "-out", "server.pem", "-extensions", "server", "-startdate", "20000101000000Z",
                    "-enddate", "20000102000000Z", "-notext")
        else:
            openssl(issuer, "x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                    "-CAcreateserial", "-out", "server.pem", "-days", "1", "-extfile", "config", "-extensions", "server")
        for filename in FILES:
            shutil.copyfile(issuer / filename, destination / filename)
            (destination / filename).chmod(0o600)


def validate_owned(directory: Path) -> None:
    """Refuse replacement/deletion of anything except this exact owned layout."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("output must be a real directory")
    entries = {entry.name for entry in directory.iterdir()}
    if entries != {MARKER, *(variant[0] for variant in VARIANTS)}:
        raise ValueError("output contains foreign or incomplete fixture content")
    marker = directory / MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != OWNER:
        raise ValueError("output is not owned by this generator")
    for name, _, _ in VARIANTS:
        fixture = directory / name
        if fixture.is_symlink() or not fixture.is_dir() or {entry.name for entry in fixture.iterdir()} != FILES:
            raise ValueError("output contains foreign fixture content")
        if any(entry.is_symlink() or not entry.is_file() for entry in fixture.iterdir()):
            raise ValueError("output contains links or non-file fixture content")


def generate(output: Path) -> None:
    output = output.absolute()
    if output.name != "CommsFixturePKI":
        raise ValueError("output directory must be named CommsFixturePKI")
    if output.is_symlink():
        raise ValueError("output must not be a symbolic link")
    if output.exists():
        validate_owned(output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Build the entire replacement before touching an existing valid pool.
    staging = Path(tempfile.mkdtemp(prefix=".comms-pki-new-", dir=output.parent))
    backup: Path | None = None
    try:
        for variant in VARIANTS:
            generate_fixture(staging, *variant)
        (staging / MARKER).write_text(OWNER, encoding="utf-8")
        validate_owned(staging)
        if output.exists() or output.is_symlink():
            validate_owned(output)
            backup = Path(tempfile.mkdtemp(prefix=".comms-pki-old-", dir=output.parent))
            backup.rmdir()
            output.rename(backup)
        try:
            staging.rename(output)
        except BaseException:
            if backup is not None:
                backup.rename(output)
                backup = None
            raise
        if backup is not None:
            validate_owned(backup)
            shutil.rmtree(backup)
            backup = None
    finally:
        # This path was allocated by mkdtemp in this invocation only.
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPOSITORY / ".comms-local" / "CommsFixturePKI")
    args = parser.parse_args()
    previous_umask = os.umask(0o077)
    try:
        generate(args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Error types are enough for CI diagnosis; no process output or key data.
        parser.exit(1, f"Fixture generation failed ({type(error).__name__}). No credentials were printed.\n")
    finally:
        os.umask(previous_umask)
    print("Generated 12 isolated test authorities: 8 valid, 2 wrong-host, 2 expired. Credentials were not printed.")


if __name__ == "__main__":
    main()
