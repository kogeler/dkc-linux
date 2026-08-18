# Public key material

Only public keys and fingerprints are committed here:

- `dkc-archive-keyring.gpg`, the complete DKC archive public certificate;
- `archive-primary.fingerprint`, the certification-key fingerprint;
- `archive-signing-subkeys.fingerprints`, the reviewed signing-subkey history,
  with the active subkey on the final line.

No third-party repository key is vendored. The toolchain comes from Debian
itself, so the archive key already present on every Debian 13 system is the only
one involved.

No private key, passphrase, or secret ever enters this directory, this
repository, a container layer, a log, or an artifact.
