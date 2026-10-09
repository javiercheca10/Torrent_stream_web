FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-libtorrent ffmpeg && rm -rf /var/lib/apt/lists/*
COPY tests/seeder.py /seeder.py
CMD ["python3", "-u", "/seeder.py"]
