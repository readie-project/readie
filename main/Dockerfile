FROM python:3.13-alpine
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN wget -qO- https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz | tar -xz -C /usr/local/bin

COPY . .

ENV SERVICE_NAME=main_node
ENV PORT=50051
ENV APP_ENV=production

EXPOSE 50051

CMD ["python", "main.py"]