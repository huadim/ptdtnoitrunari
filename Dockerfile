FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system eduschool && useradd --system --gid eduschool eduschool \
    && mkdir /app/staticfiles && chown eduschool:eduschool /app/staticfiles
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY manage.py ./
COPY eduschool eduschool
COPY school school
COPY deploy deploy
USER eduschool
EXPOSE 8000
CMD ["/bin/sh", "deploy/start.sh"]
