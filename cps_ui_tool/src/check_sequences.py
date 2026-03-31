"""Simple script to extract and display FSK sequence numbers from a PCAP."""

import struct

from scapy.all import UDP, rdpcap

from project_paths import first_capture_path

MAGIC_FSK1 = 0x314B5346
HEADER_FMT = "<IHHIHH"
HEADER_SIZE = 16

pcap_path = first_capture_path()
if not pcap_path.exists():
    raise FileNotFoundError(f"Capture not found: {pcap_path}")

print("Loading PCAP file...")
packets = rdpcap(str(pcap_path))

print("Extracting FSK packets...\n")
fsk_packets = []

for pkt_num, pkt in enumerate(packets):
    if UDP in pkt and pkt[UDP].dport == 9000:
        payload = bytes(pkt[UDP].payload)
        if len(payload) >= HEADER_SIZE:
            magic, version, hdr_sz, seq, rec_count, rec_size = struct.unpack_from(
                HEADER_FMT, payload, 0
            )
            if magic == MAGIC_FSK1:
                timestamp = float(pkt.time)
                fsk_packets.append(
                    {
                        "wireshark_num": pkt_num + 1,
                        "time": timestamp,
                        "seq": seq,
                        "records": rec_count,
                    }
                )

print(f"Total FSK packets found: {len(fsk_packets)}\n")
print("=" * 80)
print("CHECKING SEQUENCES 20-170 (where packet loss occurred)")
print("=" * 80)

for i, pkt in enumerate(fsk_packets):
    if 20 <= pkt["seq"] <= 170:
        if i > 0:
            prev_time = fsk_packets[i - 1]["time"]
            time_delta_ms = (pkt["time"] - prev_time) * 1000
        else:
            time_delta_ms = 0.0

        if i > 0:
            prev_seq = fsk_packets[i - 1]["seq"]
            gap = pkt["seq"] - prev_seq if pkt["seq"] > prev_seq else 0
        else:
            gap = 0

        marker = " <-- GAP!" if gap > 1 else ""

        print(
            f"Wireshark Pkt #{pkt['wireshark_num']:5d} | "
            f"Time: {pkt['time']:.6f} | "
            f"Delta: {time_delta_ms:7.3f}ms | "
            f"Seq: {pkt['seq']:3d} | "
            f"Records: {pkt['records']:3d}{marker}"
        )

print("\n" + "=" * 80)
print("SUMMARY OF GAPS")
print("=" * 80)

gaps_found = []
for i in range(1, len(fsk_packets)):
    prev_seq = fsk_packets[i - 1]["seq"]
    curr_seq = fsk_packets[i]["seq"]
    expected_seq = (prev_seq + 1) if prev_seq < 169 else 0

    if curr_seq != expected_seq:
        if curr_seq > expected_seq:
            missing_count = curr_seq - expected_seq
        else:
            missing_count = (170 - expected_seq) + curr_seq

        time_gap_ms = (fsk_packets[i]["time"] - fsk_packets[i - 1]["time"]) * 1000

        gaps_found.append(
            {
                "before": prev_seq,
                "after": curr_seq,
                "missing": missing_count,
                "time_gap_ms": time_gap_ms,
                "wireshark_before": fsk_packets[i - 1]["wireshark_num"],
                "wireshark_after": fsk_packets[i]["wireshark_num"],
            }
        )

if gaps_found:
    print(f"\nFound {len(gaps_found)} gaps in sequence:\n")
    for gap in gaps_found:
        print(f"  Gap: seq {gap['before']:3d} -> {gap['after']:3d}")
        print(f"       Missing {gap['missing']} packets")
        print(f"       Time gap: {gap['time_gap_ms']:.3f} ms")
        print(
            f"       Wireshark packets: #{gap['wireshark_before']} "
            f"-> #{gap['wireshark_after']}"
        )
        print()
else:
    print("\nNo gaps found - all sequences are continuous!")

print("=" * 80)
print("HOW TO VERIFY IN WIRESHARK:")
print("=" * 80)
print(
    """
1. Open Wireshark and load: {}
2. Apply filter: udp.dstport == 9000
3. Look at packet numbers shown above
4. For each packet, expand: Data -> Data (XXX bytes)
5. Check bytes [8-9] (little-endian) = sequence number

Example for the gap:
  - Wireshark packet #{}: sequence = 23 (bytes 8-9: 17 00)
  - Wireshark packet #{}: sequence = 165 (bytes 8-9: A5 00)
  - Missing: sequences 24-164 (141 packets)
""".format(
        pcap_path.name,
        gaps_found[0]["wireshark_before"] if gaps_found else "N/A",
        gaps_found[0]["wireshark_after"] if gaps_found else "N/A",
    )
)
