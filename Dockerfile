# blurd — CPU-only, single process, no build toolchain at runtime.
#
# The image carries opencv-headless + onnxruntime and nothing else of weight:
# the S3 client is stdlib SigV4 rather than boto3 precisely so this stays as
# small as an ONNX runtime allows.
FROM python:3.11-slim AS base

# libgl/libglib are what opencv-python-headless still needs at import time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The postgres backend is optional. Build with --build-arg WITH_POSTGRES=1 to
# include the driver; the sqlite profile does not need it and does not pay for
# it (~4 MB).
ARG WITH_POSTGRES=0
RUN if [ "$WITH_POSTGRES" = "1" ]; then pip install --no-cache-dir "psycopg[binary]>=3.1"; fi

# Likewise the mongo backend: --build-arg WITH_MONGO=1.
ARG WITH_MONGO=0
RUN if [ "$WITH_MONGO" = "1" ]; then pip install --no-cache-dir "pymongo>=4.6"; fi

COPY src/ ./src/
COPY spec/ ./spec/
COPY ui/ ./ui/
COPY run.py ./

# Models are NOT baked in: they are ~8 MB of GPL-lineage weights whose licence
# is still an open question (see spec/capacity.md), and baking them would make
# every image rebuild a redistribution. Pull them into the volume at start.
ENV BLURD_HOME=/var/lib/blurd \
    PYTHONUNBUFFERED=1 \
    OPENCV_LOG_LEVEL=ERROR
# Run as a non-root user. The uid is fixed (65532, the conventional
# "nonroot") so a Kubernetes securityContext can name it, and the home is
# chowned here rather than relying on the pod's fsGroup -- not every CSI driver
# applies fsGroup, and a chart that only works on some of them is worse than
# one that does not work at all.
RUN groupadd -g 65532 blurd \
 && useradd -u 65532 -g 65532 -M -d /var/lib/blurd -s /usr/sbin/nologin blurd \
 && mkdir -p /var/lib/blurd/models \
 && chown -R 65532:65532 /var/lib/blurd /app
USER 65532:65532

EXPOSE 8770
# The health endpoint is unauthenticated by design, which is what makes it
# usable as a k8s probe without mounting a key into the kubelet.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8770/v1/health || exit 1

ENTRYPOINT ["python3", "run.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8770", "--foreground"]
