FROM python:3.11-slim

WORKDIR /app

COPY analyze.py .
COPY requirements.txt .

RUN mkdir /output

RUN pip install packaging
RUN pip install --no-cache-dir -r requirements.txt

ENTRYPOINT ["python", "-u", "analyze.py"]