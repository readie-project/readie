import socket
import os
import sys

def create_socket_connection(container_id: str, socket_dir: str):
    socket_path = os.path.join(socket_dir, f"{container_id}.sock")

    try:
        os.remove(socket_path)
    except OSError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        server.bind(socket_path)
        os.chmod(socket_path, 0o777)
        server.listen(1)

        print(f"Python Server listening on {socket_path}", flush=True)

        while True:
            conn, addr = server.accept()
            with conn:
                print("Connection established with Go client.", flush=True)
                data = conn.recv(1024)
                if data:
                    print(f"Python received: {data.decode()}", flush=True)

                    conn.sendall(b"Hello from the Python Server!")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
    finally:
        server.close()
        try:
            os.remove(socket_path)
        except OSError:
            pass


if __name__ == "__main__":
    container_id = os.environ.get("CONTAINER_ID", None)
    socket_dir = os.getenv("EXECUTOR_SOCKET_DIR", None)

    if not container_id or not socket_dir:
        print("CONTAINER_ID and EXECUTOR_SOCKET_DIR environment variables must be set.", file=sys.stderr)
        sys.exit(1)
    
    create_socket_connection(container_id, socket_dir)
