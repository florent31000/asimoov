"""python-for-android build hook.

Neon needed five manual steps between `buildozer android debug` runs: three
source patches to aiortc, a hand cross-compilation of libsrtp + pylibsrtp, and
a copy of missing `.pyc` files into the python bundle. They lived in scripts
full of `/home/flore` and `/tmp` paths, so nobody but that laptop could build
the APK. They are automatic here.

Every path comes from the p4a `Context`; nothing is hardcoded. Every step is
idempotent and refuses to guess: if it cannot find what it is meant to patch,
it raises instead of quietly producing an APK that crashes on the first
WebRTC connection.

Hook points (python-for-android/toolchain.py):
  before_apk_build    -- recipes are installed in python-installs, the APK
                         content has not been assembled yet. Source patches
                         and the pylibsrtp build go here.
  after_apk_build     -- the `_python_bundle__<arch>` directory exists and
                         gradle has not run. Bundle-level fixes go here.
"""

from __future__ import annotations

import compileall
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

LIBSRTP_VERSION = "2.5.0"
LIBSRTP_URL = (
    f"https://github.com/cisco/libsrtp/archive/refs/tags/v{LIBSRTP_VERSION}.tar.gz"
)

HERE = Path(__file__).resolve().parent
PATCHES = HERE.parent / "patches"


def _log(message: str) -> None:
    print(f"[asimoov-hook] {message}", file=sys.stderr, flush=True)


def _archs(ctx):
    archs = list(getattr(ctx, "archs", []))
    if not archs:
        raise RuntimeError("p4a context exposes no archs; cannot locate the build")
    return archs


def _install_dir(ctx, arch) -> Path:
    return Path(ctx.get_python_install_dir(arch.arch))


# ---------------------------------------------------------------------------
# aiortc source patches (Neon: patch_dtls.py, patch_dtls_timeout.py,
# patch_peer_cert.py, patch_srtp.py). The pyOpenSSL that p4a builds against
# predates DTLS_METHOD, DTLSv1_get_timeout, get_peer_certificate(as_cryptography)
# and get_selected_srtp_profile. Each patch adds a fallback and nothing else.
# ---------------------------------------------------------------------------


def _apply_aiortc_patches(install_dir: Path) -> None:
    target = install_dir / "aiortc" / "rtcdtlstransport.py"
    if not target.is_file():
        _log(f"aiortc not installed in {install_dir}, skipping its patches")
        return

    source = target.read_text(encoding="utf-8")
    original = source

    for patch_file in sorted(PATCHES.glob("aiortc_*.py")):
        namespace: dict = {}
        exec(compile(patch_file.read_text(encoding="utf-8"), str(patch_file), "exec"), namespace)
        apply = namespace.get("apply")
        if apply is None:
            raise RuntimeError(f"{patch_file.name} defines no apply(source) function")
        source = apply(source)

    if source != original:
        target.write_text(source, encoding="utf-8")
        _log(f"patched {target}")
    else:
        _log("aiortc already patched")


# ---------------------------------------------------------------------------
# pylibsrtp (Neon: build_srtp.sh). p4a has no libsrtp recipe, so pylibsrtp's
# CFFI extension never gets built and aiortc fails at SRTP setup. Cross-compile
# libsrtp with the NDK toolchain and link the extension against the OpenSSL
# that p4a already produced for this arch.
# ---------------------------------------------------------------------------


def _ndk_clang(ctx, arch) -> Path:
    toolchain = Path(ctx.ndk_dir) / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64"
    clang = toolchain / "bin" / f"{arch.command_prefix}{ctx.ndk_api}-clang"
    if not clang.is_file():
        raise RuntimeError(f"NDK clang not found at {clang}")
    return clang


def _build_libsrtp(work_dir: Path, clang: Path, arch) -> Path:
    src = work_dir / f"libsrtp-{LIBSRTP_VERSION}"
    lib = src / "libsrtp2.a"
    if lib.is_file():
        return src

    if not src.is_dir():
        archive = work_dir / f"libsrtp-{LIBSRTP_VERSION}.tar.gz"
        if not archive.is_file():
            _log(f"downloading libsrtp {LIBSRTP_VERSION}")
            with urllib.request.urlopen(LIBSRTP_URL, timeout=600) as response:
                archive.write_bytes(response.read())
        with tarfile.open(archive) as tar:
            tar.extractall(work_dir)

    env = dict(os.environ, CC=str(clang), AR=str(clang.parent / "llvm-ar"))
    subprocess.run(
        [
            "./configure",
            f"--host={arch.command_prefix}",
            "--disable-shared",
            "--enable-static",
            "--disable-openssl",
        ],
        cwd=src,
        env=env,
        check=True,
    )
    subprocess.run(["make", "-j4", "libsrtp2.a"], cwd=src, env=env, check=True)
    if not lib.is_file():
        raise RuntimeError(f"libsrtp build produced no {lib}")
    _log(f"built {lib}")
    return src


def _build_pylibsrtp(ctx, arch, install_dir: Path) -> None:
    package = install_dir / "pylibsrtp"
    if not package.is_dir():
        _log("pylibsrtp not installed, skipping its native build")
        return

    binding = package / "_binding.abi3.so"
    if binding.is_file():
        _log("pylibsrtp binding already built")
        return

    sources = sorted(Path(ctx.build_dir, "other_builds").glob("pylibsrtp*/**/_binding.c"))
    if not sources:
        sources = sorted(Path(ctx.build_dir).glob("**/pylibsrtp/_binding.c"))
    if not sources:
        raise RuntimeError(
            "pylibsrtp _binding.c not found under the p4a build dir; the recipe "
            "layout changed and this hook must be updated"
        )
    binding_c = sources[0]

    clang = _ndk_clang(ctx, arch)
    work_dir = Path(ctx.build_dir) / "asimoov-libsrtp" / arch.arch
    work_dir.mkdir(parents=True, exist_ok=True)
    libsrtp = _build_libsrtp(work_dir, clang, arch)

    include_root = Path(ctx.python_recipe.include_root(arch.arch))
    link_root = Path(ctx.python_recipe.link_root(arch.arch))
    libs_dir = Path(ctx.get_libs_dir(arch.arch))
    crypto = next(iter(sorted(libs_dir.glob("libcrypto*.so"))), None)
    if crypto is None:
        raise RuntimeError(
            f"no libcrypto*.so in {libs_dir}; pylibsrtp cannot be linked without "
            "the OpenSSL that p4a built for this arch"
        )

    obj = work_dir / "_binding.o"
    subprocess.run(
        [
            str(clang), "-fPIC", "-c",
            f"-I{libsrtp / 'include'}",
            f"-I{include_root}",
            f"-I{link_root}",
            "-o", str(obj),
            str(binding_c),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(clang), "-shared",
            "-o", str(binding),
            str(obj),
            str(libsrtp / "libsrtp2.a"),
            f"-L{libs_dir}",
            f"-l:{crypto.name}",
            "-lm", "-llog",
        ],
        check=True,
    )
    _log(f"built {binding}")


# ---------------------------------------------------------------------------
# Bundle .pyc completion (Neon: fix_crypto.sh). p4a ships compiled modules
# only; a recipe that installs a `.py` without leaving a matching `.pyc` in the
# bundle produces an ImportError at runtime. Generic version: every module
# present in python-installs must be present in the bundle.
# ---------------------------------------------------------------------------


def _complete_bundle_pyc(ctx, arch, dist_dir: Path) -> None:
    bundle = dist_dir / f"_python_bundle__{arch.arch}" / "_python_bundle" / "site-packages"
    if not bundle.is_dir():
        raise RuntimeError(f"python bundle not found at {bundle}")

    install_dir = _install_dir(ctx, arch)
    added = 0
    for py in install_dir.rglob("*.py"):
        relative = py.relative_to(install_dir)
        if relative.parts[0] in {"__pycache__"} or "__pycache__" in relative.parts:
            continue
        expected = bundle / relative.with_suffix(".pyc")
        if expected.exists() or not expected.parent.is_dir():
            continue
        compileall.compile_file(str(py), quiet=2)
        cached = sorted(py.parent.glob(f"__pycache__/{py.stem}.*.pyc"))
        if not cached:
            raise RuntimeError(f"could not compile {py} for the bundle")
        shutil.copyfile(cached[0], expected)
        added += 1

    _log(f"bundle .pyc check: {added} module(s) added")


# ---------------------------------------------------------------------------
# Hook entry points.
# ---------------------------------------------------------------------------


def before_apk_build(command) -> None:
    ctx = command.ctx
    for arch in _archs(ctx):
        install_dir = _install_dir(ctx, arch)
        if not install_dir.is_dir():
            raise RuntimeError(f"python install dir missing for {arch.arch}: {install_dir}")
        _log(f"patching {arch.arch} in {install_dir}")
        _apply_aiortc_patches(install_dir)
        _build_pylibsrtp(ctx, arch, install_dir)


def after_apk_build(command) -> None:
    ctx = command.ctx
    dist_dir = Path(command._dist.dist_dir)
    for arch in _archs(ctx):
        _complete_bundle_pyc(ctx, arch, dist_dir)
