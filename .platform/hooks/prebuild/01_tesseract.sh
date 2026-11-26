#!/bin/bash
# Install Tesseract OCR and English/OSD language data on Amazon Linux 2023.
# Runs before application staging/startup so `ocr_available()` in events/services/event_ocr.py
# detects `tesseract` and enables the Scan Event tab in production.
# Idempotent: skips compilation/downloads when Tesseract 5+ and eng/osd tessdata are ready.
set -euo pipefail

if [ -n "${SKIP_TESSERACT_INSTALL:-}" ]; then
    exit 0
fi

TESSERACT_PREFIX="${TESSERACT_PREFIX:-/usr/local}"
TESSDATA_DIR="${TESSDATA_DIR:-$TESSERACT_PREFIX/share/tessdata}"
TESSERACT_BIN_SYMLINK="${TESSERACT_BIN_SYMLINK:-/usr/bin/tesseract}"
LD_CONF_FILE="${LD_CONF_FILE:-/etc/ld.so.conf.d/tesseract.conf}"
REQUIRE_OCR_READY="${REQUIRE_OCR_READY:-$(/opt/elasticbeanstalk/bin/get-config environment -k REQUIRE_OCR_READY 2>/dev/null || true)}"

log_ocr() {
    local msg="$1"
    echo "$msg"
    if command -v logger >/dev/null 2>&1; then
        logger -t syncmily-ocr "$msg" || true
    fi
}

LEPTONICA_VERSION="1.87.0"
LEPTONICA_URL="https://github.com/DanBloomberg/leptonica/releases/download/${LEPTONICA_VERSION}/leptonica-${LEPTONICA_VERSION}.tar.gz"
LEPTONICA_SHA256="c73363397f96eb1295602bf44d708a994ad42046c791bf03ea0505d829bdb6a7"

TESSERACT_VERSION="5.5.4"
TESSERACT_URL="https://github.com/tesseract-ocr/tesseract/archive/refs/tags/${TESSERACT_VERSION}.tar.gz"
TESSERACT_SHA256="a8dbf8b8c8ee427ff6ad1d99116502feaf59440af1adb893caf60a6576826cf4"

TESSDATA_VERSION="4.1.0"
ENG_DATA_URL="https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/${TESSDATA_VERSION}/eng.traineddata"
ENG_DATA_SHA256="7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
OSD_DATA_URL="https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/${TESSDATA_VERSION}/osd.traineddata"
OSD_DATA_SHA256="9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff"

find_tesseract_bin() {
    if [ -x "$TESSERACT_PREFIX/bin/tesseract" ]; then
        printf '%s\n' "$TESSERACT_PREFIX/bin/tesseract"
        return 0
    fi
    command -v tesseract 2>/dev/null || return 1
}

ensure_bin_symlink() {
    local bin
    bin="$(find_tesseract_bin || true)"
    if [ -n "$bin" ] && [ -n "$TESSERACT_BIN_SYMLINK" ] && [ ! -e "$TESSERACT_BIN_SYMLINK" ]; then
        ln -sf "$bin" "$TESSERACT_BIN_SYMLINK"
    fi
}

has_tesseract_binary() {
    local bin
    bin="$(find_tesseract_bin || true)"
    [ -n "$bin" ] || return 1
    "$bin" --version 2>/dev/null | head -n 1 | grep -qE '^tesseract [5-9]\.'
}

has_required_langs() {
    local bin langs
    bin="$(find_tesseract_bin || true)"
    [ -n "$bin" ] || return 1
    langs="$("$bin" --list-langs 2>/dev/null || true)"
    printf '%s\n' "$langs" | grep -qx "eng" && printf '%s\n' "$langs" | grep -qx "osd"
}

verify_tesseract() {
    has_tesseract_binary || return 1
    ensure_bin_symlink
    has_required_langs || return 1
}

if verify_tesseract; then
    TESS_BIN="$(find_tesseract_bin)"
    log_ocr "Tesseract OCR already ready: $("$TESS_BIN" --version | head -n 1)"
    exit 0
fi

sha256_check() {
    local file="$1"
    local expected="$2"
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$expected" "$file" | sha256sum -c - >/dev/null
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c - >/dev/null
    else
        echo "No SHA-256 verification tool available." >&2
        return 1
    fi
}

download_and_verify() {
    local url="$1"
    local dest="$2"
    local expected_sha="$3"
    curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
    sha256_check "$dest" "$expected_sha"
}

safe_make_jobs() {
    local mem_kb=0
    if [ -r /proc/meminfo ]; then
        mem_kb="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo || echo 0)"
    fi
    if [ "${mem_kb:-0}" -ge 3145728 ]; then
        echo 2
    else
        echo 1
    fi
}

install_tessdata() {
    local tmp_file
    mkdir -p "$TESSDATA_DIR"

    if ! [ -f "$TESSDATA_DIR/eng.traineddata" ] || ! sha256_check "$TESSDATA_DIR/eng.traineddata" "$ENG_DATA_SHA256" 2>/dev/null; then
        tmp_file="$(mktemp "$TESSDATA_DIR/.eng.traineddata.XXXXXX")"
        download_and_verify "$ENG_DATA_URL" "$tmp_file" "$ENG_DATA_SHA256"
        chmod 0644 "$tmp_file"
        mv -f "$tmp_file" "$TESSDATA_DIR/eng.traineddata"
    fi

    if ! [ -f "$TESSDATA_DIR/osd.traineddata" ] || ! sha256_check "$TESSDATA_DIR/osd.traineddata" "$OSD_DATA_SHA256" 2>/dev/null; then
        tmp_file="$(mktemp "$TESSDATA_DIR/.osd.traineddata.XXXXXX")"
        download_and_verify "$OSD_DATA_URL" "$tmp_file" "$OSD_DATA_SHA256"
        chmod 0644 "$tmp_file"
        mv -f "$tmp_file" "$TESSDATA_DIR/osd.traineddata"
    fi
}

install_tesseract_binary() {
    if ! command -v dnf >/dev/null 2>&1; then
        echo "dnf not found; unsupported OS for automatic Tesseract install." >&2
        return 1
    fi

    dnf install -y \
        cmake \
        gcc-c++ \
        make \
        pkgconf-pkg-config \
        zlib-devel \
        libjpeg-turbo-devel \
        libpng-devel \
        libtiff-devel \
        tar \
        gzip

    local jobs
    WORK_DIR="$(mktemp -d)"
    trap 'rm -rf "${WORK_DIR:-}"' EXIT
    jobs="$(safe_make_jobs)"

    download_and_verify "$LEPTONICA_URL" "$WORK_DIR/leptonica.tar.gz" "$LEPTONICA_SHA256"
    tar -xzf "$WORK_DIR/leptonica.tar.gz" -C "$WORK_DIR"
    cmake -S "$WORK_DIR/leptonica-${LEPTONICA_VERSION}" -B "$WORK_DIR/leptonica-build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$TESSERACT_PREFIX" \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DBUILD_PROG=OFF \
        -DBUILD_SHARED_LIBS=ON \
        -DSW_BUILD=OFF \
        -DENABLE_ZLIB=ON \
        -DENABLE_PNG=ON \
        -DENABLE_JPEG=ON \
        -DENABLE_TIFF=ON \
        -DENABLE_GIF=OFF \
        -DENABLE_WEBP=OFF \
        -DENABLE_OPENJPEG=OFF
    cmake --build "$WORK_DIR/leptonica-build" -j "$jobs"
    cmake --install "$WORK_DIR/leptonica-build"

    mkdir -p "$(dirname "$LD_CONF_FILE")"
    printf '%s\n' "$TESSERACT_PREFIX/lib" > "$LD_CONF_FILE"
    ldconfig

    download_and_verify "$TESSERACT_URL" "$WORK_DIR/tesseract.tar.gz" "$TESSERACT_SHA256"
    tar -xzf "$WORK_DIR/tesseract.tar.gz" -C "$WORK_DIR"
    cmake -S "$WORK_DIR/tesseract-${TESSERACT_VERSION}" -B "$WORK_DIR/tesseract-build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$TESSERACT_PREFIX" \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_PREFIX_PATH="$TESSERACT_PREFIX" \
        -DBUILD_TRAINING_TOOLS=OFF \
        -DBUILD_TESTS=OFF \
        -DGRAPHICS_DISABLED=ON \
        -DDISABLE_ARCHIVE=ON \
        -DDISABLE_CURL=ON \
        -DBUILD_SHARED_LIBS=ON
    cmake --build "$WORK_DIR/tesseract-build" -j "$jobs"
    cmake --install "$WORK_DIR/tesseract-build"
    ldconfig

    ensure_bin_symlink
}

if ! (
    if ! has_tesseract_binary; then
        install_tesseract_binary
    fi
    install_tessdata
    verify_tesseract
); then
    log_ocr "ERROR: OCR_READINESS=unavailable (Tesseract OCR installation failed; Scan Event remains disabled while application deployment continues)." >&2
    if [ "${REQUIRE_OCR_READY:-false}" = "true" ]; then
        exit 1
    fi
    exit 0
fi

TESS_BIN="$(find_tesseract_bin)"
log_ocr "OCR_READINESS=ready binary=$TESS_BIN version=$($TESS_BIN --version | head -n 1)"
"$TESS_BIN" --list-langs
