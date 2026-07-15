FROM python:3.10-slim-bookworm

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD sh -c "rm -rf .git && gunicorn --bind 0.0.0.0:5000 app:app & python3 main.py"
