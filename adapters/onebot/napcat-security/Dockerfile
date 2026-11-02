# Fixed upstream artifact; do not replace with :latest.
FROM mlikiowa/napcat-docker@sha256:b101b8968cc31ae428ec0f8e117386e4ced7773b2d8226ed9f7ddfcec55f1b7e

# Hash verified against Ubuntu's signed jammy-security package index.
# No unpinned apt upgrade or runtime download is needed to apply this CA update.
RUN set -eu; \
    curl --fail --show-error --location --retry 3 --connect-timeout 20 --max-time 120 \
      https://security.ubuntu.com/ubuntu/pool/main/c/ca-certificates/ca-certificates_20260816ubuntu0.22.04.1_all.deb \
      -o /tmp/ca-certificates.deb; \
    echo '29ca037f49f7a4b41d67ad72946c2e541f3ae52d8f6ab1495d2af09aa391144b  /tmp/ca-certificates.deb' | sha256sum -c -; \
    DEBIAN_FRONTEND=noninteractive dpkg -i /tmp/ca-certificates.deb; \
    update-ca-certificates; \
    rm /tmp/ca-certificates.deb; \
    echo '6801f93e533846cea05f4f531874a0e84371ab2331c5585b57cd7e3b869cf96b  /app/NapCat.Shell.zip' | sha256sum -c -

LABEL org.clonoth.napcat.version="4.18.41" \
      org.clonoth.qq.version="3.2.34-53644" \
      org.clonoth.ca-certificates.version="20260816ubuntu0.22.04.1" \
      org.clonoth.security.status="candidate-requires-runtime-tls-validation"

# Updating this layer DOES NOT replace QQ's embedded Node/Chromium trust store.
# Never deploy until the system, node and chromium probes all pass.
