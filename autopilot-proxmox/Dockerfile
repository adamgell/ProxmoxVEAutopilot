FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg apt-transport-https genisoimage \
    && rm -rf /var/lib/apt/lists/*

# Install PowerShell
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
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

# Create runtime directories
RUN mkdir -p /app/jobs /app/output/hashes

EXPOSE 5000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "5000"]
