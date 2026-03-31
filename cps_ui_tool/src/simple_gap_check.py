"""Analyze an FSK PCAP and write a human-readable gap report."""
from __future__ import annotations

import argparse
import struct
import sys
from datetime import datetime
from pathlib import Path

from scapy.all import UDP, rdpcap
from project_paths import REPORTS_DIR, first_capture_path

MAGIC_FSK1 = 0x314B5346
HEADER_FMT = "<IHHIHH"
HEADER_SIZE = 16
DEFAULT_INPUT = first_capture_path()
DEFAULT_OUTPUT = REPORTS_DIR / "gap_analysis.txt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decode an FSK Wireshark capture and report sequence gaps."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(DEFAULT_INPUT),
        help=f"Path to the PCAP/PCAPNG file. Default: {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Path to the text report. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--seq-min",
        type=int,
        default=None,
        help="Only display packets whose sequence is greater than or equal to this value.",
    )
    parser.add_argument(
        "--seq-max",
        type=int,
        default=None,
        help="Only display packets whose sequence is less than or equal to this value.",
    )
    parser.add_argument(
        "--wrap-at",
        type=int,
        default=None,
        help="Sequence rollover value. If omitted, sequence numbers are treated as monotonic.",
    )
    return parser.parse_args()


def extract_fsk_packets(pcap_path: Path) -> list[dict[str, float | int]]:
    packets = rdpcap(str(pcap_path))
    fsk_packets: list[dict[str, float | int]] = []

    for pkt_num, pkt in enumerate(packets):
        if UDP not in pkt or pkt[UDP].dport != 9000:
            continue

        payload = bytes(pkt[UDP].payload)
        if len(payload) < HEADER_SIZE:
            continue

        magic, version, hdr_sz, seq, rec_count, rec_size = struct.unpack_from(
            HEADER_FMT, payload, 0
        )
        if magic != MAGIC_FSK1:
            continue

        fsk_packets.append(
            {
                "wireshark_num": pkt_num + 1,
                "time": float(pkt.time),
                "seq": seq,
                "records": rec_count,
            }
        )

    return fsk_packets


def find_gaps(
    fsk_packets: list[dict[str, float | int]], wrap_at: int | None = None
) -> list[dict[str, float | int]]:
    gaps_found: list[dict[str, float | int]] = []

    for i in range(1, len(fsk_packets)):
        prev_seq = int(fsk_packets[i - 1]["seq"])
        curr_seq = int(fsk_packets[i]["seq"])
        if wrap_at is None:
            expected_seq = prev_seq + 1
        else:
            expected_seq = 0 if prev_seq >= wrap_at else prev_seq + 1

        if curr_seq == expected_seq:
            continue

        if wrap_at is None or curr_seq > expected_seq:
            missing_count = curr_seq - expected_seq
        else:
            missing_count = ((wrap_at + 1) - expected_seq) + curr_seq

        time_gap_ms = (float(fsk_packets[i]["time"]) - float(fsk_packets[i - 1]["time"])) * 1000

        gaps_found.append(
            {
                "before": prev_seq,
                "after": curr_seq,
                "missing": missing_count,
                "time_gap_ms": time_gap_ms,
                "wireshark_before": int(fsk_packets[i - 1]["wireshark_num"]),
                "wireshark_after": int(fsk_packets[i]["wireshark_num"]),
            }
        )

    return gaps_found


def write_report(
    output_path: Path,
    input_path: Path,
    fsk_packets: list[dict[str, float | int]],
    gaps_found: list[dict[str, float | int]],
    seq_min: int | None = None,
    seq_max: int | None = None,
    wrap_at: int | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as report:
        min_seq = min((int(pkt["seq"]) for pkt in fsk_packets), default=None)
        max_seq = max((int(pkt["seq"]) for pkt in fsk_packets), default=None)
        if wrap_at is None:
            seq_mode = "monotonic (+1 expected)"
        else:
            seq_mode = f"wrap at {wrap_at}"

        print("FSK GAP ANALYSIS REPORT", file=report)
        print("=" * 90, file=report)
        print(f"Generated: {datetime.now().isoformat(timespec='seconds')}", file=report)
        print(f"Input capture: {input_path.resolve()}", file=report)
        print(f"Output report: {output_path.resolve()}", file=report)
        print(f"Sequence mode: {seq_mode}", file=report)
        if min_seq is None:
            print("Sequence range: no FSK packets decoded", file=report)
        else:
            print(f"Sequence range: {min_seq} -> {max_seq}", file=report)
        if seq_min is None and seq_max is None:
            print("Displayed packets: all decoded FSK packets", file=report)
        else:
            lower = "-inf" if seq_min is None else seq_min
            upper = "+inf" if seq_max is None else seq_max
            print(f"Displayed packets: sequence filter {lower} -> {upper}", file=report)
        print(file=report)
        print("Loading PCAP file...", file=report)
        print("Extracting FSK packets...", file=report)
        print(f"\nTotal FSK packets found: {len(fsk_packets)}\n", file=report)
        print("=" * 90, file=report)
        print("DECODED FSK PACKETS", file=report)
        print("=" * 90, file=report)
        print(
            f"{'WS Pkt#':<10} {'Timestamp':<20} {'Time Delta (ms)':<18} {'Seq':<6} {'Records':<10}",
            file=report,
        )
        print("-" * 90, file=report)

        prev_pkt: dict[str, float | int] | None = None
        for pkt in fsk_packets:
            seq = int(pkt["seq"])
            if seq_min is not None and seq < seq_min:
                continue
            if seq_max is not None and seq > seq_max:
                continue

            if prev_pkt is None:
                time_delta_ms = 0.0
                seq_gap = 0
            else:
                time_delta_ms = (float(pkt["time"]) - float(prev_pkt["time"])) * 1000
                prev_seq = int(prev_pkt["seq"])
                seq_gap = seq - prev_seq if seq > prev_seq else 0

            gap_marker = "  <-- GAP!" if seq_gap > 1 else ""
            print(
                f"{int(pkt['wireshark_num']):<10} {float(pkt['time']):<20.6f} "
                f"{time_delta_ms:<18.3f} {seq:<6} {int(pkt['records']):<10}{gap_marker}",
                file=report,
            )
            prev_pkt = pkt

        print("\n" + "=" * 90, file=report)
        print("SUMMARY OF ALL GAPS", file=report)
        print("=" * 90, file=report)

        if gaps_found:
            print(f"\nFound {len(gaps_found)} sequence gaps:\n", file=report)
            for idx, gap in enumerate(gaps_found, 1):
                print(f"Gap #{idx}:", file=report)
                print(
                    f"  Sequence jump: {int(gap['before'])} -> {int(gap['after'])} "
                    f"(missing {int(gap['missing'])} packets)",
                    file=report,
                )
                print(f"  Time gap: {float(gap['time_gap_ms']):.3f} ms", file=report)
                print(
                    f"  Wireshark packet numbers: #{int(gap['wireshark_before'])} "
                    f"-> #{int(gap['wireshark_after'])}",
                    file=report,
                )
                print(file=report)
        else:
            print("\nNo gaps found!", file=report)

        print("=" * 90, file=report)
        print("HOW TO VERIFY MANUALLY IN WIRESHARK", file=report)
        print("=" * 90, file=report)
        if gaps_found:
            gap = gaps_found[0]
            print(
                f"""
1. Open Wireshark
2. File -> Open -> {input_path.name}
3. Apply filter: udp.dstport == 9000
4. Go to packet #{int(gap['wireshark_before'])}:
   - Click on packet
   - Expand "Data" section at bottom
   - Look at bytes (hex view on right)
   - Bytes [8-9] = sequence number in little-endian
   - You should see: {int(gap['before']):02x} 00

5. Go to packet #{int(gap['wireshark_after'])}:
   - Same steps
   - Bytes [8-9] should show: {int(gap['after']):02x} 00

6. Note: There are NO packets between #{int(gap['wireshark_before'])} and #{int(gap['wireshark_after'])}
   This proves the {int(gap['missing'])} packets were never received by your PC!
""",
                file=report,
            )
        else:
            print(
                "\nNo manual gap verification section was generated because no sequence gaps were detected.",
                file=report,
            )


def write_error_report(output_path: Path, input_path: Path, exc: Exception) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as report:
        print("FSK GAP ANALYSIS REPORT", file=report)
        print("=" * 90, file=report)
        print(f"Generated: {datetime.now().isoformat(timespec='seconds')}", file=report)
        print(f"Input capture: {input_path.resolve()}", file=report)
        print(f"Output report: {output_path.resolve()}", file=report)
        print(file=report)
        print("Analysis failed.", file=report)
        print(f"Error type: {type(exc).__name__}", file=report)
        print(f"Error: {exc}", file=report)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()

    try:
        if not input_path.exists():
            raise FileNotFoundError(f"Capture file not found: {input_path}")

        fsk_packets = extract_fsk_packets(input_path)
        gaps_found = find_gaps(fsk_packets, wrap_at=args.wrap_at)
        write_report(
            output_path,
            input_path,
            fsk_packets,
            gaps_found,
            seq_min=args.seq_min,
            seq_max=args.seq_max,
            wrap_at=args.wrap_at,
        )
    except Exception as exc:
        write_error_report(output_path, input_path, exc)
        print(f"Analysis failed. Check: {output_path}", file=sys.stderr)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Analysis complete! Check: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
