#!/usr/bin/env bash
# Build script for WorkAMA desktop Rust code inside Docker.
# Uses rust:1.82-bookworm as base (bookworm has Tauri system-dep support),
# installs latest stable Rust via rustup (rsproxy.cn mirror), and the
# required Tauri system libraries via USTC debian mirror.
set -euo pipefail

# Switch debian apt to Aliyun mirror for faster, more reliable downloads.
cat > /etc/apt/sources.list.d/debian.sources <<'EOF'
Types: deb
URIs: https://mirrors.aliyun.com/debian
Suites: bookworm bookworm-updates bookworm-backports
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://mirrors.aliyun.com/debian-security
Suites: bookworm-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
# Remove the original deb.debian.org sources to avoid falling back to it.
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources.bak

echo "--- APT UPDATE ---"
apt-get update -qq

echo "--- APT INSTALL ---"
apt-get install -y -qq \
    libdbus-1-dev \
    libwebkit2gtk-4.1-dev \
    libssl-dev \
    libgtk-3-dev \
    libayatana-appindicator3-dev \
    librsvg2-dev \
    pkg-config \
    build-essential

echo "--- INSTALL RUST 1.88 ---"
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
rustup install 1.88.0 --profile minimal
rustup default 1.88.0
rustc --version
cargo --version

echo "--- PKG-CONFIG CHECK ---"
pkg-config --exists dbus-1 && echo "dbus-1 OK"
pkg-config --exists webkit2gtk-4.1 && echo "webkit2gtk-4.1 OK"

echo "--- CARGO CHECK ---"
cargo check 2>&1 | tail -80

echo "--- CARGO TEST ---"
cargo test 2>&1 | tail -80
