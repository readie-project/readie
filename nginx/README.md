# NGINX gRPC Proxy

NGINX is used as the public entrypoint for the Readie router.

The architecture is:

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
Python client ──→ <public_hostname>:443
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

The router and workers are kept private inside the Docker Compose network. Only NGINX exposes a port on the host.

## Files

```text
nginx/
├── nginx.local.conf       # Local development, plaintext gRPC
├── nginx.prod.conf        # Production, TLS + gRPC
└── certs/                 # Production TLS certificates
```

Production certificates should **not** be committed to Git.

## Local development

Local development does not require a domain name or TLS certificate.

The local NGINX configuration listens on port `50051`:

```nginx
server {
    listen 50051 http2;

    location / {
        grpc_pass grpc://router:50051;

        grpc_read_timeout 1h;
        grpc_send_timeout 1h;
    }
}
```

The gRPC client connects to:

```text
localhost:50051
```

NGINX then forwards the request to:

```text
router:50051
```

No `50051` port needs to be exposed directly by the router container.

## Production

Production uses a public hostname. The client connects to port 443 of the hostname.

NGINX terminates TLS and proxies the gRPC request to:

```text
router:50051
```

The production configuration uses:

```nginx
server {
    listen 443 ssl;
    http2 on;

    server_name <public_hostname>;

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;

    location / {
        grpc_pass grpc://grpc_router;

        grpc_read_timeout 1h;
        grpc_send_timeout 1h;
    }
}
```

### TLS certificates

The production certificate can be obtained from [Let's Encrypt](https://letsencrypt.org/) using Certbot.

On the Azure VM:

```bash
sudo apt update
sudo apt install certbot
```

Obtain a certificate:

```bash
sudo certbot certonly --standalone \
    -d <public_hostname>
```

Certbot will create the certificate under:

```text
/etc/letsencrypt/live/<public_hostname>/
```

The important files are:

```text
fullchain.pem
privkey.pem
```

These are mounted into the NGINX container as:

```text
/etc/nginx/certs/server.crt
/etc/nginx/certs/server.key
```

Do **not** commit the private key to the repository.

### DNS

The DNS record must point:

```text
<public_hostname> → <public_IP>
```

The service should have inbound access to port `443`.

Port `50051` and the worker port `50052` should remain private and should not be exposed through the Azure Network Security Group.

## Production deployment

Start the production stack with:

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

## Client configuration

The client should not hardcode the router container address. Instead use the `configure` method.
There is no need to configure for the production environment.

Local:

```python
readie.configure(router_uri="localhost:50051")
```

Example Python setup:

```python
import os
import grpc

endpoint = os.getenv("READIE_ROUTER_URI", "localhost:50051")
tls = os.getenv("READIE_TLS", "false").lower() == "true"

if tls:
    channel = grpc.secure_channel(
        endpoint,
        grpc.ssl_channel_credentials(),
    )
else:
    channel = grpc.insecure_channel(endpoint)
```

## Troubleshooting

Check that NGINX can resolve the router:

```bash
docker compose exec nginx getent hosts router
```

Check that the router is reachable from NGINX:

```bash
docker compose exec nginx \
    grpcurl -plaintext router:50051 grpc.health.v1.Health/Check
```

Check NGINX logs:

```bash
docker compose logs nginx
```

Check router logs:

```bash
docker compose logs router
```

If the client cannot connect in production, verify:

1. `<public_hostname>` resolves to the VM.
2. Azure allows inbound TCP `443`.
3. The TLS certificate is valid for `<public_hostname>`.
4. NGINX is listening on port `443`.
5. The router is healthy.
6. NGINX can reach `router:50051`.

The important networking rule is:

```text
Host/client → NGINX → router → worker
```

not:

```text
Host/client → router
```

NGINX is the only public entrypoint.