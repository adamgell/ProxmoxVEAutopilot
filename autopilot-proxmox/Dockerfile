FROM python:3.12-slim-bookworm

# Install system deps + PowerShell in one layer to reduce image size
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg apt-transport-https genisoimage procps \
    openssh-client sshpass \
    # Kerberos stack for AD monitoring probes (SASL/GSSAPI bind with
    # signing — required by modern Windows DCs with "LDAP signing"
    # enforced). libsasl2-modules-gssapi-mit is the GSSAPI plugin
    # OpenLDAP/libldap2 dispatches to; libldap2-dev + libsasl2-dev
    # are build deps for python-ldap in the pip layer below.
    krb5-user libkrb5-dev libsasl2-modules-gssapi-mit libldap2-dev libsasl2-dev \
    build-essential \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/microsoft-debian-bookworm-prod bookworm main" \
       > /etc/apt/sources.list.d/microsoft.list \
    && apt-get update && apt-get install -y --no-install-recommends powershell \
    && rm -rf /var/lib/apt/lists/*

# Install PowerShell modules
RUN pwsh -NonInteractive -Command " \
    Set-PSRepository PSGallery -InstallationPolicy Trusted; \
    Install-Module -Name Microsoft.Graph.Authentication -Scope AllUsers -Force; \
    Install-Module -Name WindowsAutopilotIntune -MinimumVersion 5.4.0 -Scope AllUsers -Force"

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Bake in Kerberos config pointing at HOME.GELL.ONE. Operators with a
# different realm overlay this via docker-compose volume mount.
RUN cp /app/files/krb5.conf /etc/krb5.conf

# Embed the build SHA and timestamp so the UI can show what's running.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
RUN printf '%s\n%s\n' "$GIT_SHA" "$BUILD_TIME" > /app/VERSION
ENV APP_VERSION_FILE=/app/VERSION

# Create runtime directories
RUN mkdir -p /app/jobs /app/output/hashes /app/secrets

# Enable OpenSSL legacy provider so hashlib.new("md4") works. NTLM
# auth (used by ldap3 when talking to our DC) needs MD4 for the NT
# hash; OpenSSL 3.x hides MD4 behind the legacy provider by default.
# TODO: when we migrate AD auth to Kerberos/LDAPS+channel-binding we
# can delete this and drop the legacy provider. Tracked in
# docs/specs/2026-04-20-device-state-monitoring-design.md.
RUN printf '%s\n' \
    'openssl_conf = openssl_init' \
    '' \
    '[openssl_init]' \
    'providers = provider_sect' \
    '' \
    '[provider_sect]' \
    'default = default_sect' \
    'legacy = legacy_sect' \
    '' \
    '[default_sect]' \
    'activate = 1' \
    '' \
    '[legacy_sect]' \
    'activate = 1' \
    > /etc/ssl/openssl-legacy.cnf
ENV OPENSSL_CONF=/etc/ssl/openssl-legacy.cnf

EXPOSE 5000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "5000"]
