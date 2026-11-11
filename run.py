"""Start the finder and print the address to open on the iPad."""
import socket

import uvicorn

PORT = 8756


def lan_ip() -> str:
    """This PC's address on the local network (no packets are actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


if __name__ == "__main__":
    print()
    print(f"  在 iPad 的 Safari 輸入： http://{lan_ip()}:{PORT}")
    print(f"  在這台電腦上打開：     http://localhost:{PORT}")
    print("  （iPad 要和這台電腦在同一個 Wi-Fi；關掉這個視窗就會停止）")
    print()
    uvicorn.run("app.server:app", host="0.0.0.0", port=PORT, log_level="warning")
