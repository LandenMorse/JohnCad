FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# --- PrusaSlicer CLI (attachments/slicing.py) ---------------------------
# BEST-EFFORT install, NOT verified against real Pi hardware -- confirm
# `prusa-slicer --export-gcode --output out.gcode in.stl` actually works
# headless on the target device before relying on this in production.
# PrusaSlicer's official Linux releases are primarily x86_64; arm64 support
# is newer/less consistently packaged (see e.g. github.com/prusa3d/PrusaSlicer
# releases, or third-party arm64 builds like davidk/PrusaSlicer-ARM.AppImage)
# -- double check the asset name/URL for whatever version you actually want
# before trusting this step. TARGETARCH is set automatically by BuildKit.
ARG TARGETARCH
ARG PRUSASLICER_VERSION=2.9.6
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/* \
    && case "$TARGETARCH" in \
        amd64) SUFFIX=linux-x64 ;; \
        arm64) SUFFIX=linux-arm64 ;; \
        *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    && curl -fL -o /tmp/prusaslicer.AppImage \
        "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSASLICER_VERSION}/PrusaSlicer-${PRUSASLICER_VERSION}+${SUFFIX}-GTK3.AppImage" \
    && chmod +x /tmp/prusaslicer.AppImage \
    && cd /tmp && ./prusaslicer.AppImage --appimage-extract \
    && mv /tmp/squashfs-root /opt/prusaslicer \
    && ln -s /opt/prusaslicer/AppRun /usr/local/bin/prusa-slicer \
    && rm /tmp/prusaslicer.AppImage
# --------------------------------------------------------------------------

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh
RUN useradd --create-home appuser \
    && mkdir -p /app/private_media /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "cms.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
