FROM rust:1.82-bookworm

# Switch debian apt to Aliyun mirror for faster downloads in China
RUN printf 'Types: deb\nURIs: https://mirrors.aliyun.com/debian\nSuites: bookworm bookworm-updates bookworm-backports\nComponents: main contrib non-free non-free-firmware\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: https://mirrors.aliyun.com/debian-security\nSuites: bookworm-security\nComponents: main contrib non-free non-free-firmware\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' > /etc/apt/sources.list.d/debian.sources \
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources.bak \
    && apt-get update -qq \
    && apt-get install -y -qq \
        libdbus-1-dev \
        libwebkit2gtk-4.1-dev \
        libssl-dev \
        libgtk-3-dev \
        libayatana-appindicator3-dev \
        librsvg2-dev \
        pkg-config \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install latest stable Rust via rustup (rsproxy mirror)
ENV RUSTUP_DIST_SERVER=https://rsproxy.cn
ENV RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
RUN rustup install stable --profile minimal \
    && rustup default stable \
    && rustc --version \
    && cargo --version

# Verify system libraries are discoverable
RUN pkg-config --exists dbus-1 && echo "dbus-1 OK" \
    && pkg-config --exists webkit2gtk-4.1 && echo "webkit2gtk-4.1 OK"

WORKDIR /src/apps/desktop/src-tauri
