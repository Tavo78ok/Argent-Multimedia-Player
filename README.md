# 🎬 Argent Multimedia Player

[![Version](https://img.shields.io/badge/version-1.3.0-blue.svg)]()
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![GTK](https://img.shields.io/badge/GTK-4.0-green.svg)]()
[![Libadwaita](https://img.shields.io/badge/Libadwaita-1.0-purple.svg)]()
[![License](https://img.shields.io/badge/license-MIT-orange.svg)]()

**Argent Multimedia Player** es un reproductor multimedia todo-en-uno, moderno y nativo para Linux, construido sobre **GTK4**, **Libadwaita** y **GStreamer**. Diseñado para ser ligero, rápido y con una integración perfecta en entornos de escritorio como GNOME, Cinnamon y XFCE.

Combina lo mejor de la reproducción local, el streaming IPTV y la búsqueda nativa de YouTube en una interfaz elegante y fluida.

<img width="1440" height="900" alt="Captura de pantalla_2026-11-14_23-57-09" src="https://github.com/user-attachments/assets/ddbec61a-c1cd-4c4d-ba36-e4a5760af802" />

---

## ✨ Características Principales

*   🎵 **Biblioteca Local**: Reproducción de audio y video (MP3, FLAC, MKV, MP4, etc.) con lectura de metadatos mediante `mutagen`.
*   📺 **IPTV / M3U**: Soporte completo para listas M3U y M3U8 (HLS). Incluye un potente **buscador por texto y filtro por categoría** capaz de manejar miles de canales sin lag.
*   📹 **Buscador Nativo de YouTube**: Integración directa con `yt-dlp` y `mpv`. Sin WebKit, sin consumo excesivo de RAM. Incluye miniaturas, límite de calidad configurable (720p por defecto) y reproducción fluida.
*   🎛️ **Equalizador de 10 Bandas**: Ecualizador en tiempo real con presets (Rock, Pop, Jazz, Vocal, etc.) que no bloquea la interfaz.
*   🛡️ **Crash Recovery**: Sistema de registro de fallos (`signals.log`). Si GStreamer falla o la app detecta un cierre abrupto, el video se abre automáticamente en `mpv` en el siguiente intento.
*   🖱️ **Integración con el Sistema**: Soporte para arrastrar y soltar (Drag & Drop), apertura de archivos desde el gestor de archivos y empaquetado `.deb` oficial.

---

## 📦 Instalación

### Opción 1: Usando el paquete `.deb` (Recomendado para Debian/Ubuntu/Mint)
Si ya tienes el archivo `.deb` generado, puedes instalarlo con:
```bash
sudo dpkg -i argent-multimedia-player_*.deb
sudo apt-get install -f  # Para resolver dependencias si es necesario
```
Opción 2: Desde el código fuente
1-Clona el repositorio:
```bash
git clone https://github.com/Tavo78ok/Argent-Multimedia-Player.git
cd Argent-Multimedia-Player
```
02-Instala las dependencias del sistema:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 \
gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
gstreamer1.0-gtk4 python3-mutagen mpv yt-dlp
```
03-Ejecuta la aplicación:
```bash
python3 appmain.py
```
🛠️ Empaquetado (.deb)
El repositorio incluye un script automatizado para generar tu propio paquete Debian de forma limpia y profesional.

```bash
# Hacer ejecutable el script (solo la primera vez)
chmod +x build-deb.sh

# Generar el .deb
./build-deb.sh

# Generar e instalar directamente
./build-deb.sh --install

# Limpiar los archivos temporales de compilación
./build-deb.sh --clean
```
El script extrae automáticamente la versión del código, valida el archivo .desktop, instala los íconos en las rutas de hicolor, actualiza la caché del sistema y verifica todo con lintian.

⌨️ Atajos de Teclado

Tecla	Acción

* Espacio	Reproducir / Pausar

* ← / →	Retroceder / Adelantar 5 segundos

* N / P	Siguiente / Anterior pista

* Ctrl + O	Abrir archivos multimedia

* F o F11	Pantalla completa

* Escape	Salir de pantalla completa

* Ctrl + Q	Cerrar la aplicación

## 🧠 Bajo el Capó

UI Moderna:
* Construida con GTK4 y Libadwaita, con soporte para temas oscuros, claros y del sistema.

Motor de Video:
* Utiliza el pipeline de GStreamer con gtk4paintablesink para un renderizado de video suave y acelerado.

Manejo de Errores:
* Instalación de handlers asíncronos para señales SIGSEGV, SIGABRT y SIGBUS que registran el fallo sin cerrar la aplicación abruptamente.

Asincronía:
* Carga de listas M3U en hilos secundarios y guardado de datos (debounced) para mantener la interfaz siempre fluida.

# 🤝 Contribuciones

Las contribuciones son bienvenidas. Si encuentras un bug o tienes una idea para una nueva característica, siéntete libre de abrir un Issue o enviar un Pull Request.

📄 Licencia
Este proyecto está bajo la Licencia GPL. Consulta el archivo LICENSE para más detalles.

Hecho con ❤️ y mucho mate en Argentina 🇦🇷.

