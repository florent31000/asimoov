#!/usr/bin/env bash
#
# Build the ASIMOOV debug APK.
#
#   ./android/build.sh              build the image, then build inside it
#   ./android/build.sh --in-container   run the build here (the image's entrypoint)
#   ./android/build.sh --release    same, release mode (unsigned)
#
# The repository is mounted, not copied: the APK lands in android/bin/.

set -euo pipefail

ANDROID_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$ANDROID_DIR/.." && pwd)"
IMAGE="${ASIMOOV_ANDROID_IMAGE:-asimoov-android}"
MODE="debug"
IN_CONTAINER="no"

for arg in "$@"; do
  case "$arg" in
    --in-container) IN_CONTAINER="yes" ;;
    --release) MODE="release" ;;
    --debug) MODE="debug" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# --- staging ---------------------------------------------------------------
# buildozer packages `source.dir`, so the package and the robot it runs have
# to sit next to main.py. Both copies are gitignored and rebuilt every time.
stage() {
  echo "==> staging asimoov + robots/avatar into $ANDROID_DIR"
  rm -rf "$ANDROID_DIR/asimoov" "$ANDROID_DIR/robots"
  mkdir -p "$ANDROID_DIR/robots"
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude 'secrets.yaml' \
    "$REPO_DIR/src/asimoov/" "$ANDROID_DIR/asimoov/"
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude 'secrets.yaml' \
    "$REPO_DIR/robots/avatar/" "$ANDROID_DIR/robots/avatar/"

  if find "$ANDROID_DIR/asimoov" "$ANDROID_DIR/robots" -name 'secrets.yaml' | grep -q .; then
    echo "refusing to build: a secrets.yaml reached the staging directory" >&2
    exit 1
  fi
}

# --- in-container build ----------------------------------------------------
if [ "$IN_CONTAINER" = "yes" ]; then
  stage
  echo "==> checking the aiortc patches still apply"
  python3 "$ANDROID_DIR/hooks/check_patches.py"
  echo "==> buildozer android $MODE"
  cd "$ANDROID_DIR"
  buildozer -v android "$MODE"
  ls -la "$ANDROID_DIR/bin"
  exit 0
fi

# --- host side -------------------------------------------------------------
if ! docker version > /dev/null 2>&1; then
  echo "docker is not available; start Docker and retry" >&2
  exit 1
fi

echo "==> building image $IMAGE"
docker build -t "$IMAGE" "$ANDROID_DIR"

echo "==> running the build in $IMAGE"
docker run --rm \
  -v "$REPO_DIR":/work \
  -v asimoov-buildozer:/home/builder/.buildozer \
  -v asimoov-gradle:/home/builder/.gradle \
  -e ASIMOOV_BUILD_MODE="$MODE" \
  "$IMAGE" "--$MODE" --in-container
