FROM python:3.11-slim

ARG EDGETERMINAL_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_OPTIONAL_LOCKS=0 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    TZ=UTC \
    EDGETERMINAL_IMAGE_REVISION=${EDGETERMINAL_REVISION} \
    EDGETERMINAL_IMAGE_REQUIREMENTS=/opt/edgeterminal/requirements.json

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin edgeterminal \
    && mkdir -p /app /opt/edgeterminal \
    && chown 10001:10001 /app \
    && git config --system --add safe.directory /app

COPY requirements.txt requirements-model.txt /opt/edgeterminal/
COPY deploy/constraints.txt /opt/edgeterminal/constraints.txt
RUN python -m pip install --no-cache-dir -r /opt/edgeterminal/requirements-model.txt -c /opt/edgeterminal/constraints.txt \
    && python -m pip check \
    && python -c 'import hashlib,json,pathlib; p=pathlib.Path("/opt/edgeterminal"); d={n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in ("requirements.txt","requirements-model.txt")}; d["deploy/constraints.txt"]=hashlib.sha256((p/"constraints.txt").read_bytes()).hexdigest(); (p/"requirements.json").write_text(json.dumps(d))'

WORKDIR /app
USER 10001:10001
CMD ["python", "deploy/runtime.py", "web"]
