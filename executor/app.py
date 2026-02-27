import socket
import os
import sys
import cloudpickle


def create_socket_connection(socket_dir: str):
    socket_path = os.path.join(socket_dir, "executor.sock")

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
                    file_path = os.path.join(socket_dir, data.decode())
                    if os.path.exists(file_path):
                        print(f"Python received: {data.decode()}", flush=True)
                        with open(file_path, 'rb') as f:
                            loaded_data = cloudpickle.load(f)
                            conn.sendall(str(loaded_data['func'](
                                *loaded_data['args'], **loaded_data['kwargs'])).encode())
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
    finally:
        server.close()
        try:
            os.remove(socket_path)
        except OSError:
            pass


if __name__ == "__main__":
    socket_dir = os.getenv("EXECUTOR_SOCKET_DIR", None)

    if not socket_dir:
        print("EXECUTOR_SOCKET_DIR environment variables must be set.", file=sys.stderr)
        sys.exit(1)

    create_socket_connection(socket_dir)
