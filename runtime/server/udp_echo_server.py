#!/usr/bin/env python3
import argparse
import socket
import time
import os
from datetime import datetime, timezone

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    os.makedirs("logs", exist_ok=True)
    log_path = args.log or f"logs/udp_echo_server_{args.port}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))

    print(f"[udp_echo] listening on {args.host}:{args.port}")
    print(f"[udp_echo] log: {log_path}", flush=True)

    with open(log_path, "a", buffering=1) as f:
        f.write("utc_time,client_ip,client_port,bytes\n")
        while True:
            data, addr = sock.recvfrom(65535)
            t = utc_now()
            client_ip, client_port = addr
            sock.sendto(data, addr)
            f.write(f"{t},{client_ip},{client_port},{len(data)}\n")

if __name__ == "__main__":
    main()
