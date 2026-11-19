#!/usr/bin/env bash
#
# build-deb.sh — Empaqueta Argent Multimedia Player en un .deb instalable
#
# Uso:
#   ./build-deb.sh                    → genera el .deb en el directorio actual
#   ./build-deb.sh --install          → genera e instala con sudo dpkg -i
#   ./build-deb.sh --clean            → borra artefactos de build y sale
#   ./build-deb.sh --version 1.4.0    → override de versión
#   ./build-deb.sh --help             → esta ayuda
#
# Requiere (build-deps, no del runtime):
#   dpkg-deb, dpkg, gzip, tar
#   desktop-file-utils (recomendado: valida el .desktop antes de empaquetar)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuración editable
# ---------------------------------------------------------------------------

MAINTAINER_NAME="Tavo"
MAINTAINER_EMAIL="tavopc@openargentos.local"
DEBIAN_DIST="unstable"

PKG_NAME="argent-multimedia-player"
APP_ID_FALLBACK="com.argent.MultimediaPlayer"
SCRIPT_PY="argent-multimedia-player.py"

ICON_CANDIDATES=(
    "com.argent.MultimediaPlayer.png"
    "com.argent.MultimediaPlayer.svg"
    "argent-multimedia-player.png"
    "argent-multimedia-player.svg"
    "packaging/icons/hicolor/256x256/apps/com.argent.MultimediaPlayer.png"
    "packaging/icons/hicolor/scalable/apps/com.argent.MultimediaPlayer.svg"
    "packaging/icons/hicolor/256x256/apps/argent-multimedia-player.png"
    "packaging/icons/hicolor/scalable/apps/argent-multimedia-player.svg"
    "packaging/com.argent.MultimediaPlayer.png"
    "packaging/argent-multimedia-player.png"
)

# ---------------------------------------------------------------------------
# Colores y helpers
# ---------------------------------------------------------------------------

if [ -t 1 ]; then
    C_OK="\033[1;32m"; C_INFO="\033[1;34m"; C_WARN="\033[1;33m"
    C_ERR="\033[1;31m"; C_DIM="\033[2m"; C_OFF="\033[0m"
else
    C_OK=""; C_INFO=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

log()  { printf "${C_INFO}▶${C_OFF} %s\n" "$*"; }
ok()   { printf "${C_OK}✓${C_OFF} %s\n" "$*"; }
warn() { printf "${C_WARN}⚠${C_OFF} %s\n" "$*" >&2; }
err()  { printf "${C_ERR}✗${C_OFF} %s\n" "$*" >&2; }
die()  { err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Parseo de argumentos
# ---------------------------------------------------------------------------

DO_INSTALL=0
DO_CLEAN=0
VERSION_OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --install)  DO_INSTALL=1; shift ;;
        --clean)    DO_CLEAN=1; shift ;;
        --version)
            [ -n "${2:-}" ] || die "--version requiere un valor"
            VERSION_OVERRIDE="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            die "Argumento desconocido: $1 (probá --help)" ;;
    esac
done

# ---------------------------------------------------------------------------
# Directorios base
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_DIR="$SCRIPT_DIR/build"
PKG_ROOT="$BUILD_DIR/${PKG_NAME}"

# ---------------------------------------------------------------------------
# --clean: solo limpia y sale
# ---------------------------------------------------------------------------

if [ "$DO_CLEAN" = "1" ]; then
    if [ -d "$BUILD_DIR" ]; then
        rm -rf "$BUILD_DIR"
        ok "Limpieza completa: $BUILD_DIR"
    else
        ok "Nada que limpiar"
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Verificaciones de entorno
# ---------------------------------------------------------------------------

for cmd in dpkg-deb dpkg gzip; do
    command -v "$cmd" >/dev/null 2>&1 || die "Falta '$cmd'. Instalá dpkg (suele venir con el sistema)."
done

[ -f "$SCRIPT_PY" ] || die "No se encontró '$SCRIPT_PY' en $SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Extraer metadatos del propio código fuente
# ---------------------------------------------------------------------------

extract_py() {
    local key="$1" fallback="$2"
    local val
    val=$(grep -oP "${key}\\s*=\\s*\"\\K[^\"]+" "$SCRIPT_PY" | head -n1 || true)
    [ -n "$val" ] && echo "$val" || echo "$fallback"
}

APP_ID=$(extract_py "APP_ID" "$APP_ID_FALLBACK")
APP_NAME=$(extract_py "APP_NAME" "Argent Multimedia Player")
APP_VERSION=$(extract_py "APP_VERSION" "0.0.0")

[ -n "$VERSION_OVERRIDE" ] && APP_VERSION="$VERSION_OVERRIDE"

if ! echo "$APP_VERSION" | grep -Eq '^[0-9][A-Za-z0-9.+~-]*$'; then
    die "Versión inválida '$APP_VERSION' para Debian (debe empezar con dígito)."
fi

log "Paquete:   $PKG_NAME"
log "Versión:   $APP_VERSION"
log "App ID:    $APP_ID"
log "Nombre:    $APP_NAME"

# ---------------------------------------------------------------------------
# Localizar el ícono
# ---------------------------------------------------------------------------

ICON_PATH=""
ICON_EXT=""
for cand in "${ICON_CANDIDATES[@]}"; do
    if [ -f "$SCRIPT_DIR/$cand" ]; then
        ICON_PATH="$SCRIPT_DIR/$cand"
        ICON_EXT="${cand##*.}"
        break
    fi
done

if [ -z "$ICON_PATH" ]; then
    warn "No se encontró ícono. La app usará un ícono genérico en el menú."
    warn "Colocá '${APP_ID}.png' (o .svg) junto al script y volvé a ejecutar."
else
    ok "Ícono:     $(basename "$ICON_PATH")"
fi

# ---------------------------------------------------------------------------
# Preparar árbol de build
# ---------------------------------------------------------------------------

log "Preparando árbol de build…"
rm -rf "$BUILD_DIR"
mkdir -p "$PKG_ROOT/DEBIAN"

mkdir -p "$PKG_ROOT/usr/bin"
mkdir -p "$PKG_ROOT/usr/lib/$PKG_NAME"
mkdir -p "$PKG_ROOT/usr/share/applications"
mkdir -p "$PKG_ROOT/usr/share/metainfo"
mkdir -p "$PKG_ROOT/usr/share/doc/$PKG_NAME"
mkdir -p "$PKG_ROOT/usr/share/$PKG_NAME"

# ---------------------------------------------------------------------------
# Copiar código
# ---------------------------------------------------------------------------

log "Copiando código fuente…"
install -m 644 "$SCRIPT_PY" "$PKG_ROOT/usr/lib/$PKG_NAME/argent-multimedia-player.py"
ok "Código instalado en /usr/lib/$PKG_NAME/"

cat > "$PKG_ROOT/usr/bin/argent-multimedia-player" <<'EOF'
#!/bin/sh
# Wrapper de Argent Multimedia Player (paquete Debian)
exec python3 "/usr/lib/argent-multimedia-player/argent-multimedia-player.py" "$@"
EOF
chmod 755 "$PKG_ROOT/usr/bin/argent-multimedia-player"
ok "Wrapper en /usr/bin/argent-multimedia-player"

# ---------------------------------------------------------------------------
# Copiar ícono(s)
# ---------------------------------------------------------------------------

if [ -n "$ICON_PATH" ]; then
    case "$ICON_EXT" in
        png)
            mkdir -p "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps"
            install -m 644 "$ICON_PATH" \
                "$PKG_ROOT/usr/share/icons/hicolor/256x256/apps/${APP_ID}.png"
            ok "Ícono PNG instalado (256x256)"
            ;;
        svg)
            mkdir -p "$PKG_ROOT/usr/share/icons/hicolor/scalable/apps"
            install -m 644 "$ICON_PATH" \
                "$PKG_ROOT/usr/share/icons/hicolor/scalable/apps/${APP_ID}.svg"
            ok "Ícono SVG instalado (scalable)"
            ;;
        *)
            warn "Extensión de ícono no manejada: .$ICON_EXT (se omite)"
            ;;
    esac
    install -m 644 "$ICON_PATH" "$PKG_ROOT/usr/share/$PKG_NAME/$(basename "$ICON_PATH")"
fi

# ---------------------------------------------------------------------------
# .desktop  (CRÍTICO: MimeType en UNA sola línea — multilínea rompe el parser)
# ---------------------------------------------------------------------------

log "Generando .desktop…"
DESKTOP_FILE="$PKG_ROOT/usr/share/applications/${APP_ID}.desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=${APP_NAME}
GenericName=Multimedia Player
Comment=Reproductor local · IPTV · Buscador de YouTube
Exec=argent-multimedia-player %U
TryExec=argent-multimedia-player
Icon=${APP_ID}
Terminal=false
Categories=AudioVideo;Audio;Video;Player;
MimeType=audio/mpeg;audio/flac;audio/ogg;audio/opus;audio/x-wav;audio/mp4;video/mp4;video/x-matroska;video/webm;video/quicktime;video/mp2t;application/x-mpegurl;audio/x-mpegurl;application/vnd.apple.mpegurl;
Keywords=multimedia;player;video;audio;iptv;m3u;youtube;stream;mpv;
StartupNotify=true
StartupWMClass=${APP_ID}
X-GNOME-UsesNotifications=true
EOF
chmod 644 "$DESKTOP_FILE"

if command -v desktop-file-validate >/dev/null 2>&1; then
    if ! desktop-file-validate "$DESKTOP_FILE"; then
        die "desktop-file-validate encontró errores en el .desktop (ver arriba)."
    fi
    ok ".desktop validado con desktop-file-validate"
else
    warn "desktop-file-validate no está instalado (sudo apt install desktop-file-utils)."
fi
ok ".desktop generado"

# ---------------------------------------------------------------------------
# AppStream metainfo
# ---------------------------------------------------------------------------

log "Generando metainfo AppStream…"
TODAY=$(date -u +%Y-%m-%d)
cat > "$PKG_ROOT/usr/share/metainfo/${APP_ID}.metainfo.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>${APP_ID}</id>
  <name>${APP_NAME}</name>
  <summary>Reproductor multimedia todo-en-uno</summary>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>MIT</project_license>
  <description>
    <p>
      ${APP_NAME} es un reproductor multimedia construido sobre GTK4,
      Libadwaita y GStreamer. Soporta reproducción local, listas IPTV/M3U,
      un buscador nativo de YouTube (usando yt-dlp y mpv), y un ecualizador
      de 10 bandas.
    </p>
    <ul>
      <li>Biblioteca local de audio y video</li>
      <li>IPTV / M3U (incluye HLS)</li>
      <li>Buscador nativo de YouTube (bajo consumo de RAM)</li>
      <li>Equalizador de 10 bandas</li>
      <li>Límite de calidad configurable para YouTube (720p por defecto)</li>
    </ul>
  </description>
  <launchable type="desktop-id">${APP_ID}.desktop</launchable>
  <url type="homepage">https://example.com/${PKG_NAME}</url>
  <releases>
    <release version="${APP_VERSION}" date="${TODAY}"/>
  </releases>
  <content_rating type="oars-1.1"/>
</component>
EOF
chmod 644 "$PKG_ROOT/usr/share/metainfo/${APP_ID}.metainfo.xml"
ok "Metainfo instalado"

# ---------------------------------------------------------------------------
# Documentación
# ---------------------------------------------------------------------------

log "Generando documentación (copyright + changelog)…"

cat > "$PKG_ROOT/usr/share/doc/$PKG_NAME/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: ${PKG_NAME}
Source: https://example.com/${PKG_NAME}

Files: *
Copyright: $(date +%Y), ${MAINTAINER_NAME} <${MAINTAINER_EMAIL}>
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a
 copy of this software and associated documentation files (the "Software"),
 to deal in the Software without restriction, including without limitation
 the rights to use, copy, modify, merge, publish, distribute, sublicense,
 and/or sell copies of the Software, and to permit persons to whom the
 Software is furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in
 all copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
 DEALINGS IN THE SOFTWARE.
EOF

CHANGELOG_TMP=$(mktemp)
cat > "$CHANGELOG_TMP" <<EOF
${PKG_NAME} (${APP_VERSION}) ${DEBIAN_DIST}; urgency=medium

  * Versión ${APP_VERSION}
   - Rediseño de la pestaña Web: ahora es un buscador nativo de YouTube.
   - Integración con yt-dlp y mpv (bajo consumo de RAM, sin crasheos de WebKit).
   - Eliminado el filtro de calidad de GStreamer (causaba crashes en gtk4paintablesink).
   - Añadido límite de resolución configurable para YouTube (720p por defecto).
   - Crash recovery automático: si GStreamer falla, el video se abre en mpv.
   - Interfaz de búsqueda con miniaturas y lista de resultados.

 -- ${MAINTAINER_NAME} <${MAINTAINER_EMAIL}>  $(date -R)
EOF
gzip -9n -c "$CHANGELOG_TMP" > "$PKG_ROOT/usr/share/doc/$PKG_NAME/changelog.gz"
rm -f "$CHANGELOG_TMP"
chmod 644 "$PKG_ROOT/usr/share/doc/$PKG_NAME/copyright" \
         "$PKG_ROOT/usr/share/doc/$PKG_NAME/changelog.gz"
ok "Documentación generada"

# ---------------------------------------------------------------------------
# DEBIAN/control
# ---------------------------------------------------------------------------

log "Generando DEBIAN/control…"
INSTALLED_SIZE=$(du -sk "$PKG_ROOT/usr" | awk '{print $1}')

cat > "$PKG_ROOT/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${APP_VERSION}
Section: sound
Priority: optional
Architecture: all
Installed-Size: ${INSTALLED_SIZE}
Maintainer: ${MAINTAINER_NAME} <${MAINTAINER_EMAIL}>
Depends: python3:any,
 python3-gi,
 gir1.2-gtk-4.0,
 gir1.2-adw-1,
 gir1.2-gstreamer-1.0,
 gir1.2-gst-plugins-base-1.0,
 gstreamer1.0-plugins-base,
 gstreamer1.0-plugins-good,
 gstreamer1.0-gtk4
Recommends: gstreamer1.0-plugins-bad,
 gstreamer1.0-plugins-ugly,
 gstreamer1.0-libav,
 python3-mutagen,
 mpv,
 yt-dlp,
 desktop-file-utils
Suggests: fonts-noto-color-emoji
Description: ${APP_NAME} — reproductor multimedia GTK4
 Reproductor multimedia todo-en-uno construido sobre GTK4, Libadwaita,
 GStreamer, y con integración nativa para buscar y reproducir contenido
 de YouTube usando yt-dlp y mpv.
 .
 Características principales:
  * Biblioteca local de audio y video con metadata (mutagen)
  * IPTV / Listas M3U y M3U8 (HLS)
  * Buscador nativo de YouTube con miniaturas y soporte de mpv
  * Equalizador de 10 bandas con presets
  * Límite de calidad configurable para YouTube (720p, 1080p, etc.)
  * Control de salvapantallas durante la reproducción
  * Recuperación automática de video (fallback a mpv si GStreamer falla)
EOF
chmod 644 "$PKG_ROOT/DEBIAN/control"
ok "control generado"

# ---------------------------------------------------------------------------
# Scripts postinst / prerm / postrm
# ---------------------------------------------------------------------------

log "Generando scripts de mantenimiento…"

cat > "$PKG_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

# Refrescar índice de aplicaciones (clave para que aparezca en el menú)
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
fi

# Actualizar caché de íconos
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi

# AppStream (opcional)
if command -v appstreamcli >/dev/null 2>&1; then
    appstreamcli refresh-cache --force >/dev/null 2>&1 || true
fi

# Aviso si yt-dlp es muy viejo (YouTube cambia sus bloqueos seguido)
if command -v yt-dlp >/dev/null 2>&1; then
    YTDLP_VER=$(yt-dlp --version 2>/dev/null | head -n1 | tr -d 'v')
    YTDLP_YEAR=$(echo "$YTDLP_VER" | cut -d. -f1)
    CURRENT_YEAR=$(date +%Y)
    case "$YTDLP_YEAR" in
        ''|*[!0-9]*) YTDLP_YEAR=0 ;;
    esac
    if [ "$YTDLP_YEAR" -gt 0 ] && [ "$YTDLP_YEAR" -lt "$CURRENT_YEAR" ]; then
        echo ""
        echo "Aviso: tu versión de yt-dlp ($YTDLP_VER) puede estar desactualizada."
        echo "Si YouTube deja de funcionar en la app, actualizalo con:"
        echo "  sudo pip install -U yt-dlp --break-system-packages"
        echo ""
    fi
fi

exit 0
EOF
chmod 755 "$PKG_ROOT/DEBIAN/postinst"

cat > "$PKG_ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
exit 0
EOF
chmod 755 "$PKG_ROOT/DEBIAN/prerm"

cat > "$PKG_ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e

if [ "$1" = "purge" ]; then
    for d in /home/*/.config/argent-multimedia-player; do
        [ -d "$d" ] || continue
        rm -rf "$d" 2>/dev/null || true
    done
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi

exit 0
EOF
chmod 755 "$PKG_ROOT/DEBIAN/postrm"
ok "Scripts de mantenimiento listos"

# ---------------------------------------------------------------------------
# Ajustar permisos de todo el árbol
# ---------------------------------------------------------------------------

log "Ajustando permisos…"
find "$PKG_ROOT/usr" -type d -exec chmod 755 {} \;
find "$PKG_ROOT/usr" -type f -exec chmod 644 {} \;
chmod 755 "$PKG_ROOT/usr/bin/argent-multimedia-player"
ok "Permisos ajustados"

# ---------------------------------------------------------------------------
# Build del .deb
# ---------------------------------------------------------------------------

DEB_FILE="${PKG_NAME}_${APP_VERSION}_all.deb"
DEB_PATH="$SCRIPT_DIR/$DEB_FILE"

log "Empaquetando .deb…"
dpkg-deb --root-owner-group -Zxz --build "$PKG_ROOT" "$DEB_PATH" >/dev/null

DEB_SIZE=$(du -h "$DEB_PATH" | awk '{print $1}')
echo
ok "═══════════════════════════════════════════════════════════════"
ok "  Generado: $DEB_FILE  ($DEB_SIZE)"
ok "═══════════════════════════════════════════════════════════════"
echo

# ---------------------------------------------------------------------------
# Verificación básica (lintian si está disponible)
# ---------------------------------------------------------------------------

if command -v lintian >/dev/null 2>&1; then
    log "Ejecutando lintian (puede tardar unos segundos)…"
    if lintian --no-tag-display-limit "$DEB_PATH" 2>&1 | grep -E "^E:|^W:" ; then
        warn "lintian reportó advertencias arriba (no bloquean la instalación)"
    else
        ok "lintian: sin errores ni advertencias"
    fi
fi

# ---------------------------------------------------------------------------
# Instalación opcional
# ---------------------------------------------------------------------------

if [ "$DO_INSTALL" = "1" ]; then
    log "Instalando con dpkg…"
    if [ "$(id -u)" -eq 0 ]; then
        dpkg -i "$DEB_PATH" || apt-get install -f -y
    else
        sudo dpkg -i "$DEB_PATH" || sudo apt-get install -f -y
    fi
    ok "Instalado. Probá con: argent-multimedia-player"
    echo
    log "Para desinstalar:  sudo apt remove ${PKG_NAME}"
    log "Para purgar todo:  sudo apt purge ${PKG_NAME}"
else
    echo "${C_DIM}Instalación manual:${C_OFF}"
    echo "  sudo dpkg -i $DEB_FILE"
    echo "  sudo apt-get install -f    # si faltan dependencias"
    echo
    echo "${C_DIM}O volvé a correr:${C_OFF}  ./build-deb.sh --install"
fi