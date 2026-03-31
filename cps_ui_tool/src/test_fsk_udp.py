"""Send sample FSK1 UDP packets to localhost:9000 for testing both modes."""
import socket
import struct
import time
from udp_fsk import FSKReceiver, FSK_CSV_FILE, FSK_LOG_FILE

MAGIC = 0x314B5346

def build_normal_packet():
    header = struct.pack('<IHHIHH', MAGIC, 1, 16, 42, 3, 8)
    recs  = struct.pack('<HhHBB', 1200, -15, 500, 0b001, 2)
    recs += struct.pack('<HhHBB', 1180,  20, 520, 0b011, 3)
    recs += struct.pack('<HhHBB', 1210,   0, 480, 0b100, 1)
    return header + recs

def build_test_packet():
    header = struct.pack('<IHHIHH', MAGIC, 1, 16, 99, 3, 8)
    recs  = struct.pack('<IHBB', 1, 0xA55A, 0x01, 0x00)   # valid pattern
    recs += struct.pack('<IHBB', 2, 0xA55A, 0x02, 0x03)   # valid pattern
    recs += struct.pack('<IHBB', 3, 0xDEAD, 0x01, 0x01)   # BAD pattern
    return header + recs

def send(payload):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(payload, ('127.0.0.1', 9000))
    sock.close()

# ── Test mode ────────────────────────────────────────────
print("=== TEST MODE ===")
r = FSKReceiver(test_mode=True)
r.start()
time.sleep(0.5)
send(build_test_packet())
time.sleep(1)
r.stop()
time.sleep(0.5)

# Print results
with open(FSK_LOG_FILE, encoding="utf-8") as f:
    print(f.read())
with open(FSK_CSV_FILE, encoding="utf-8") as f:
    print(f.read())

# ── Normal mode ──────────────────────────────────────────
print("\n=== NORMAL MODE ===")
r2 = FSKReceiver(test_mode=False)
r2.start()
time.sleep(0.5)
send(build_normal_packet())
time.sleep(1)
r2.stop()
time.sleep(0.5)

with open(FSK_LOG_FILE, encoding="utf-8") as f:
    print(f.read())
with open(FSK_CSV_FILE, encoding="utf-8") as f:
    print(f.read())
