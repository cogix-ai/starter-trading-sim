# DO NOT EDIT. Builds the image Cogix runs in its sandbox. `./submit` uses it.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY sdk /opt/sdk
RUN pip install --no-cache-dir /opt/sdk
COPY agent.py main.py /agent/
WORKDIR /agent
ENTRYPOINT ["/usr/local/bin/python3", "/agent/main.py"]
