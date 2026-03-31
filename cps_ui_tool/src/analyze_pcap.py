"""Parse a PCAP file and extract UDP sequence numbers for FSK packets."""

import struct

from project_paths import first_capture_path

try:
    from scapy.all import UDP, rdpcap

    HAVE_SCAPY = True
except ImportError:
    HAVE_SCAPY = False
    print("Scapy not available, trying manual parsing...")

MAGIC_FSK1 = 0x314B5346
HEADER_FMT = "<IHHIHH"
HEADER_SIZE = 16


def analyze_with_scapy(pcap_file):
    """Analyze using scapy."""
    print(f"Reading {pcap_file} with scapy...")
    packets = rdpcap(pcap_file)

    udp_packets = [p for p in packets if UDP in p and p[UDP].dport == 9000]
    print(f"Found {len(udp_packets)} UDP packets to port 9000\n")

    sequences = []
    for i, pkt in enumerate(udp_packets):
        payload = bytes(pkt[UDP].payload)
        if len(payload) >= HEADER_SIZE:
            magic, version, hdr_sz, seq, rec_count, rec_size = struct.unpack_from(
                HEADER_FMT, payload, 0
            )
            if magic == MAGIC_FSK1:
                timestamp = float(pkt.time)
                sequences.append((i, timestamp, seq, rec_count))

    return sequences


def analyze_manual(_pcap_file):
    """Manual parsing without scapy."""
    print("ERROR: Need scapy to parse pcap. Install with: pip install scapy")
    return []


if __name__ == "__main__":
    pcap_path = first_capture_path()
    if not pcap_path.exists():
        raise FileNotFoundError(f"Capture not found: {pcap_path}")

    if HAVE_SCAPY:
        sequences = analyze_with_scapy(str(pcap_path))
    else:
        sequences = analyze_manual(str(pcap_path))

    if sequences:
        print(f"Total FSK packets in capture: {len(sequences)}\n")
        print("First 10 packets:")
        for pkt_num, ts, seq, rec_count in sequences[:10]:
            print(f"  Packet {pkt_num}: time={ts:.6f} seq={seq} records={rec_count}")

        print("\n" + "=" * 70)
        print("Checking for gaps around sequence 23-165...")
        print("=" * 70)

        relevant = [
            (pkt_num, ts, seq, rec_count)
            for pkt_num, ts, seq, rec_count in sequences
            if 20 <= seq <= 170
        ]

        if relevant:
            print(f"\nFound {len(relevant)} packets with sequence 20-170:\n")
            for pkt_num, ts, seq, rec_count in relevant:
                print(
                    f"  Wireshark packet #{pkt_num}: time={ts:.6f} "
                    f"seq={seq:3d} records={rec_count}"
                )

            seqs_only = [seq for _, _, seq, _ in relevant]
            print("\n" + "=" * 70)
            print("Sequence analysis:")
            print("=" * 70)
            missing = []
            for i in range(len(seqs_only) - 1):
                current = seqs_only[i]
                next_seq = seqs_only[i + 1]
                expected = (current + 1) if current < 169 else 0

                if next_seq != expected:
                    gap = abs(next_seq - current)
                    missing.append((current, next_seq, gap))
                    print(f"  GAP DETECTED: seq {current} -> {next_seq} (gap={gap})")

            if not missing:
                print("  OK: no gaps found in Wireshark.")
                print("\n  CONCLUSION: Python missed packets that were on the network.")
                print("  ROOT CAUSE: Application-level packet loss")
            else:
                print(f"\n  Found {len(missing)} gaps in Wireshark capture")
                print("  ROOT CAUSE: Network-level packet loss")
        else:
            print("No packets found in that range!")
