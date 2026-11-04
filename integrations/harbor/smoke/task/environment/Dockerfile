FROM python:3.12.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates && rm -rf /var/lib/apt/lists/* && ! command -v git
WORKDIR /workspace
COPY mock_endpoint.py /opt/mock_endpoint.py
COPY start.sh /opt/start.sh
ENTRYPOINT ["sh", "/opt/start.sh"]
HEALTHCHECK --interval=1s --timeout=2s --retries=30 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["sleep", "infinity"]
