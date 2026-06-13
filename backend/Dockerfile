# Portal-mapper backend. Multi-arch base (pulls linux/arm64 on the Oracle A1).
# All deps are pure-Python or ship manylinux aarch64 wheels (fastapi,
# uvicorn[standard] -> uvloop/httptools, python-multipart), so nothing compiles.
FROM python:3.13-slim

WORKDIR /app

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# ONE uvicorn process (the app owns a single shared ProcessPoolExecutor sized to
# os.cpu_count(); --workers would multiply the pool and oversubscribe the cores).
# No --reload in prod. --proxy-headers because nginx terminates TLS in front.
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
