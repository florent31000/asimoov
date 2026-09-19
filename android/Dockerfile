# ASIMOOV Android build image.
#
# Everything the APK build needs is pinned here so that a build on a laptop
# and a build in CI produce the same thing. Nothing in this image is specific
# to one machine: the repository is mounted at /work at run time.
#
#   docker build -t asimoov-android android/
#   docker run --rm -v "$PWD":/work -v asimoov-buildozer:/home/builder/.buildozer asimoov-android
#
# Stop at the toolchain stage (SDK + NDK + buildozer, no project) with:
#   docker build --target toolchain -t asimoov-android-toolchain android/

FROM ubuntu:22.04 AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG NDK_VERSION=25.1.8937393
ARG ANDROID_API=35
ARG BUILD_TOOLS=35.0.0
ARG CMDLINE_TOOLS=11076708
ARG P4A_TAG=v2024.01.21
ARG BUILDOZER_VERSION=1.5.0

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    ANDROID_HOME=/opt/android-sdk \
    ANDROIDSDK=/opt/android-sdk \
    ANDROIDNDK=/opt/android-sdk/ndk/25.1.8937393 \
    ANDROIDAPI=35 \
    ANDROIDMINAPI=26 \
    P4A_SOURCE_DIR=/opt/python-for-android \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

RUN apt-get update && apt-get install -y --no-install-recommends \
        autoconf automake libtool libtool-bin pkg-config \
        build-essential ccache cmake ninja-build patch \
        git curl wget unzip zip rsync file \
        openjdk-17-jdk-headless \
        python3 python3-dev python3-pip python3-venv python3-setuptools \
        libffi-dev libssl-dev zlib1g-dev \
        libncurses-dev libncursesw5-dev libtinfo5 \
        libltdl-dev gettext \
    && rm -rf /var/lib/apt/lists/*

# Android SDK: command-line tools, platform-tools, one platform, one build-tools.
RUN mkdir -p "$ANDROID_HOME/cmdline-tools" \
    && curl -fsSL -o /tmp/cmdline-tools.zip \
        "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS}_latest.zip" \
    && unzip -q /tmp/cmdline-tools.zip -d "$ANDROID_HOME/cmdline-tools" \
    && mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest" \
    && rm /tmp/cmdline-tools.zip \
    && yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses > /dev/null \
    && "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --install \
        "platform-tools" \
        "platforms;android-${ANDROID_API}" \
        "build-tools;${BUILD_TOOLS}" \
        "ndk;${NDK_VERSION}" > /dev/null

# NDK 25b is what the Go2 WebRTC stack was proven against; keep the pin.
RUN test -d "$ANDROIDNDK/toolchains/llvm/prebuilt/linux-x86_64" \
    || (echo "NDK layout unexpected at $ANDROIDNDK" && exit 1)

RUN python3 -m pip install --no-cache-dir \
        "buildozer==${BUILDOZER_VERSION}" \
        "Cython==0.29.36" \
        virtualenv \
        wheel \
        packaging \
        sh

# python-for-android is pinned by tag and used through p4a.source_dir so
# buildozer never silently clones a moving master.
RUN git clone --branch "${P4A_TAG}" --depth 1 \
        https://github.com/kivy/python-for-android.git "$P4A_SOURCE_DIR" \
    && python3 -m pip install --no-cache-dir -e "$P4A_SOURCE_DIR"

RUN useradd -m -u 1000 -s /bin/bash builder \
    && mkdir -p /work /home/builder/.buildozer \
    && chown -R builder:builder /work /home/builder

FROM toolchain AS build

USER builder
WORKDIR /work
ENV HOME=/home/builder \
    PATH=/home/builder/.local/bin:$PATH

ENTRYPOINT ["/work/android/build.sh"]
CMD ["--in-container"]
