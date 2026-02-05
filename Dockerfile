# Use the official Playwright Python image (includes all OS dependencies)
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

# Set environment variables
# PYTHONDONTWRITEBYTECODE: Prevents Python from writing pyc files to disc
# PYTHONUNBUFFERED: Ensures logs are printed immediately (fixes "stuck" logs)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser into the container
RUN playwright install chromium

# Copy application code
COPY . .

# Run the bot
CMD ["python", "bot.py"]