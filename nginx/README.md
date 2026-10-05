# NGINX gRPC proxy

NGINX is the public entrypoint for the Readie router. It accepts client gRPC
traffic and forwards it to the router inside the Docker Compose network. The
router and the workers are not published on the host. Only NGINX exposes a port.

```text
                    Local
Python client ──→ localhost:50051
                       │
                       ▼
                    NGINX
                       │
                       ▼
                 router:50051
                       │
                       ▼
                  worker(s)


                  Production
Python client ──→ <public-hostname>:443
                       │
                  TLS / HTTP2
                       │
                       ▼
                    NGINX
                       │
                 gRPC / HTTP2
                       │
                       ▼
                 router:50051
                       │
                       ▼
                  worker(s)
```

For the full deployment procedure, see [Deploy with
TLS](https://readie.org/docs/contributing/deploy-with-tls). For the security
posture, see [Security](https://readie.org/docs/architecture/security).

## Files

```text
nginx/
├── nginx.local.conf       # Local development, plaintext gRPC
├── nginx.prod.conf        # Production, TLS + gRPC
├── certs/
│   └── server.crt         # Committed public certificate chain (see below)
└── .gitignore             # Ignores *.key
```

The compose files mount the configuration into the stock `nginx:alpine` image:
`docker-compose.local.yml` mounts `nginx.local.conf` and publishes port `50051`,
and `docker-compose.prod.yml` mounts `nginx.prod.conf` and `nginx/certs/`, and
publishes port `443`.

### Committed certificate

`nginx/certs/server.crt` is committed to the repository. It is a certificate
chain in PEM format, with four certificates. The leaf certificate is issued by
Let's Encrypt for `readie.eastus.cloudapp.azure.com`, the hostname in
`nginx.prod.conf`. The file contains no private key, so it is public
information. The matching `server.key` is ignored by `nginx/.gitignore` and
must never be committed.

The committed chain lets the production configuration start for that hostname
when the key is supplied separately. It is not a self-signed development
certificate. Let's Encrypt certificates expire after 90 days, so the file must
be replaced on renewal, and a deployment under a different hostname must replace
it with its own chain. The local configuration does not use it.

## Playground route

The configurations also forward `/playground/` to the playground service, when it is
running, and keep every other path for gRPC. The playground itself calls the Readie server
that the client uses by default, like any other client. See
[`playground/README.md`](../playground/README.md).

## Local development

Local development needs no domain name and no TLS certificate. The local
configuration listens for plaintext gRPC on port `50051` and forwards to the
router:

```nginx
upstream grpc_router {
    server router:50051;
}

server {
    listen 50051 http2;

    location / {
        grpc_pass grpc://grpc_router;

        grpc_read_timeout 1h;
        grpc_send_timeout 1h;
    }
}
```

The router container does not need to publish port `50051` itself.

## Production

Production uses a public hostname. Clients connect to port `443` of that
hostname. NGINX terminates TLS and proxies the gRPC stream to `router:50051`:

```nginx
server {
    listen 443 ssl;
    http2 on;

    server_name <public-hostname>;

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;

    location / {
        grpc_pass grpc://grpc_router;

        grpc_read_timeout 1h;
        grpc_send_timeout 1h;
    }
}
```

`nginx.prod.conf` defines the `grpc_router` upstream the same way as the local
configuration. Set `server_name` in `nginx.prod.conf` to your own hostname
before deploying.

### Authentication

NGINX does not authenticate callers. The router does, when its `AUTH_TOKEN`
environment variable is set. Each client sends the same value, which it reads
from the `READIE_AUTH_TOKEN` environment variable. An empty or unset `AUTH_TOKEN`
turns authentication off. Use TLS together with a token on any public
deployment, and do not commit the token. For the compose override that sets
`AUTH_TOKEN`, see [Deploy with
TLS](https://readie.org/docs/contributing/deploy-with-tls).

### TLS certificates

Any certificate authority works. The following steps use [Let's Encrypt](https://letsencrypt.org/)
with Certbot on a Debian-based host, such as a cloud VM.

1. Install Certbot:

   ```bash
   sudo apt update
   sudo apt install certbot
   ```

2. Obtain a certificate. The standalone mode needs inbound port `80` open
   during validation:

   ```bash
   sudo certbot certonly --standalone -d <public-hostname>
   ```

   Certbot writes the certificate to `/etc/letsencrypt/live/<public-hostname>/`.
   The files NGINX needs are `fullchain.pem` and `privkey.pem`.

3. Copy them into `nginx/certs/` under the names the compose file mounts into
   the container as `/etc/nginx/certs/server.crt` and
   `/etc/nginx/certs/server.key`:

   ```bash
   sudo cp /etc/letsencrypt/live/<public-hostname>/fullchain.pem nginx/certs/server.crt
   sudo cp /etc/letsencrypt/live/<public-hostname>/privkey.pem   nginx/certs/server.key
   sudo chmod 600 nginx/certs/server.key
   ```

The private key must not be committed to the repository.

### DNS and network access

The DNS record for the hostname must point at the public IP address of the host:

```text
<public-hostname> → <public-ip>
```

Allow inbound TCP on port `443` in the host firewall or the cloud network
security rules. For example, on Azure, add an inbound rule to the network
security group. Keep port `50051` and the worker port `50052` private. They must
not be reachable from the internet.

## Production deployment

Start the production stack:

```bash
docker compose \
    -f docker-compose.yml \
    -f docker-compose.prod.yml \
    up -d --build
```

Check the NGINX configuration:

```bash
docker compose exec nginx nginx -t
```

Reload NGINX after a certificate renewal:

```bash
docker compose exec nginx nginx -s reload
```

Check the containers:

```bash
docker compose ps
```

## Troubleshooting

Check that NGINX can resolve the router:

```bash
docker compose exec nginx getent hosts router
```

Check that the router is healthy. The router image provides `grpcurl`, which
its own health check uses:

```bash
docker compose exec router \
    grpcurl -plaintext router:50051 grpc.health.v1.Health/Check
```

Read the NGINX and router logs:

```bash
docker compose logs nginx
docker compose logs router
```

If a client cannot connect in production, verify the following:

1. `<public-hostname>` resolves to the host.
2. The firewall or cloud security rules allow inbound TCP `443`.
3. The TLS certificate is valid for `<public-hostname>` and has not expired.
4. NGINX is listening on port `443`.
5. The router is healthy.
6. NGINX can reach `router:50051`.
7. The client token matches the router's `AUTH_TOKEN`, if authentication is
   enabled.

Traffic must always follow this path, with NGINX as the only public entrypoint:

```text
client → NGINX → router → worker
```

Clients must not connect to the router directly.
