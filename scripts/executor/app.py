import socket
import os
import io
import sys
import time
import cloudpickle

CHUNK_SIZE = 1024 * 1024


def process_execution_request(server: socket):
    try:
        conn, addr = server.accept()
        with conn:
            print("Connection established with Go client.", flush=True)
            pickled_bytes = bytearray()
            while True:
                data = conn.recv(CHUNK_SIZE)
                print(data)
                if not data:
                    break
                elif data.endswith(b"EOF"):
                    pickled_bytes.extend(data.rstrip(b"EOF"))
                    break
                else:
                    pickled_bytes.extend(data)

            try:
                loaded_data = cloudpickle.loads(pickled_bytes)
                func_return = loaded_data['func'](
                    *loaded_data['args'], **loaded_data['kwargs'])
                print("Request processed successfully")

                buffer = io.BytesIO(cloudpickle.dumps(func_return))
                while True:
                    piece = buffer.read(CHUNK_SIZE)
                    if not piece:
                        buffer.close()
                        break
                    conn.sendall(piece)
            except Exception as e:
                print(f"Error in executing request: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Error in fetching request: {e}", file=sys.stderr)


def create_socket_connection(socket_dir: str):
    socket_path = os.path.join(socket_dir, "executor.sock")

    try:
        os.remove(socket_path)
    except OSError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        server.bind(socket_path)
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


print("READY_FOR_CHECKPOINT", flush=True)
time.sleep(30)  # Wait to be checkpointed

socket_dir = os.getenv("EXECUTOR_DIR", None)

if not socket_dir:
    print("EXECUTOR_DIR environment variables must be set.", file=sys.stderr)
    sys.exit(1)

create_socket_connection(socket_dir)
