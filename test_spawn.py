import pty
import os

buffer = b""
def read_master(fd):
    global buffer
    data = os.read(fd, 1024)
    buffer += data
    if b"hello" in buffer.lower():
        print("\r\n[DETECTED]\r\n")
        buffer = b""
    return data

pty.spawn(["echo", "Hello World"], master_read=read_master)
