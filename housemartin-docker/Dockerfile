# Housemartin scraper — headless Chromium + Playwright on Python 3.11
# Built for linux/amd64 (DS220+ runs an Intel Celeron J4025)
FROM python:3.11-slim

# ── System deps for Chromium ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime libs
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libx11-6 libx11-xcb1 libxcb1 libxext6 libxss1 \
    # Font support (avoids blank-page issues on headless)
    fonts-liberation fonts-noto-color-emoji \
    # Utilities
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade yfinance gspread oauth2client

# Install Playwright's own Chromium binary (pinned to the version matching the lib)
RUN playwright install chromium

# ── App code ──────────────────────────────────────────────────────────────────
COPY . .

# Entrypoint: run the main orchestrator script
CMD ["python", "-u", "run_scraper.py"]
