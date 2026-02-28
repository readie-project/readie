import socket
import os
import sys
import cloudpickle


def process_execution_request(server: socket):
    try:
        conn, addr = server.accept()
        with conn:
            print("Connection established with Go client.", flush=True)
            data = conn.recv(1024)
            if data:
                file_path = os.path.join(socket_dir, data.decode())
                response_file = "response.pkl"
                response_file_path = os.path.join(
                    socket_dir, response_file)
                if os.path.exists(file_path):
                    print(f"Python received: {data.decode()}", flush=True)
                    with open(file_path, 'rb') as f:
                        loaded_data = cloudpickle.load(f)
                        func_return = loaded_data['func'](
                            *loaded_data['args'], **loaded_data['kwargs'])

                        with open(response_file_path, "wb") as f:
                            cloudpickle.dump(func_return, f)
                        conn.sendall(response_file.encode())
                else:
                    print("Could not execute code, missing request")
                    conn.sendall("".encode())
    except Exception as e:
        print(f"Error in executing request: {e}", file=sys.stderr)


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
            process_execution_request(server)

    except Exception as e:
        print(f"Error in socket server: {e}")
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
