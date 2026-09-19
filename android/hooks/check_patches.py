"""Apply every `patches/aiortc_*.py` to a fixture and check the result.

Run before the APK build (`android.yml` does): the patches are string
replacements against upstream aiortc, so they rot silently when aiortc
changes. This turns that into a build failure instead of a robot that cannot
open a WebRTC connection.

    python3 android/hooks/check_patches.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PATCHES = Path(__file__).resolve().parent.parent / "patches"

# The four snippets of aiortc/rtcdtlstransport.py the patches target.
FIXTURE = '''
from OpenSSL import SSL


class RTCDtlsTransport:
    def _get_timeout(self):
        # get timeout
        timeout = None
        if not self.encrypted:
            timeout = self._ssl.DTLSv1_get_timeout()
        return timeout

    def _handle(self):
        if True:
            if True:
                self._ssl.DTLSv1_handle_timeout()

    def _validate_peer_identity(self):
        certificate = self._ssl.get_peer_certificate(as_cryptography=True)
        return certificate

    def _setup_srtp(self):
        openssl_profile = self._ssl.get_selected_srtp_profile()
        return openssl_profile
'''

EXPECTED_MARKERS = [
    "asimoov: the pyOpenSSL",
    "asimoov: no DTLSv1_get_timeout",
    "asimoov: old pyOpenSSL, convert through PEM",
    "asimoov: old pyOpenSSL, call SSL_get_selected_srtp_profile",
]


def load_patches():
    files = sorted(PATCHES.glob("aiortc_*.py"))
    if not files:
        raise SystemExit(f"no aiortc patches in {PATCHES}")
    for path in files:
        namespace: dict = {}
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
        if "apply" not in namespace:
            raise SystemExit(f"{path.name} defines no apply(source)")
        yield path.name, namespace["apply"]


def main() -> int:
    patches = list(load_patches())

    source = FIXTURE
    for name, apply in patches:
        source = apply(source)
        print(f"applied {name}")

    for marker in EXPECTED_MARKERS:
        if marker not in source:
            raise SystemExit(f"patch result is missing {marker!r}")

    # Applying twice must be a no-op.
    twice = source
    for _, apply in patches:
        twice = apply(twice)
    if twice != source:
        raise SystemExit("patches are not idempotent")

    compile(source, "<patched>", "exec")

    # A source that no longer contains the target must fail loudly.
    for name, apply in patches:
        try:
            apply("import os\n")
        except RuntimeError:
            continue
        raise SystemExit(f"{name} silently accepted an unrelated source")

    print(f"ok: {len(patches)} patches apply, are idempotent, and fail loudly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
