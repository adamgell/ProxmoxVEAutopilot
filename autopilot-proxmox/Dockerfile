FROM python:3.12-slim-bookworm

# Install system deps + PowerShell in one layer to reduce image size
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg apt-transport-https genisoimage procps \
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

# Embed the build SHA and timestamp so the UI can show what's running.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
RUN printf '%s\n%s\n' "$GIT_SHA" "$BUILD_TIME" > /app/VERSION
ENV APP_VERSION_FILE=/app/VERSION

# Create runtime directories
RUN mkdir -p /app/jobs /app/output/hashes

EXPOSE 5000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "5000"]
