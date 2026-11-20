import unittest

from src.protocol import STREAMER_EOP, STREAMER_SOP, split_new_streamer_records


def make_record(payload: bytes, packet_timestamp: bytes | None = None) -> bytes:
    header = (
        STREAMER_SOP
        + bytes.fromhex("01 02 03 04")
        + (1200).to_bytes(2, "little")
        + (350).to_bytes(2, "little")
        + len(payload).to_bytes(2, "little")
    )
    return header + payload + (packet_timestamp or b"") + STREAMER_EOP


class SplitNewStreamerRecordsTests(unittest.TestCase):
    def setUp(self):
        self.pending = bytearray()

    def split(self, stream_bytes: bytes) -> list[dict]:
        return split_new_streamer_records({"stream_bytes": stream_bytes}, self.pending)

    def test_splits_legacy_record(self):
        records = self.split(make_record(b"hello"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"], b"hello")
        self.assertIsNone(records[0]["packet_timestamp_bytes"])

    def test_splits_record_with_ten_byte_timestamp_before_eop(self):
        timestamp = bytes.fromhex("01 23 45 67 89 AB CD EF 10 32")
        records = self.split(make_record(b"hello", timestamp))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"], b"hello")
        self.assertEqual(records[0]["packet_timestamp_bytes"], timestamp)
        self.assertEqual(records[0]["packet_start_ts"], 0x67452301)
        self.assertEqual(records[0]["packet_end_ts"], 0xEFCDAB89)
        self.assertEqual(records[0]["packet_reserved_bytes"], bytes.fromhex("10 32"))
        self.assertEqual(records[0]["rect_v"], 1200)
        self.assertEqual(records[0]["rect_i"], 350)

    def test_handles_timestamped_record_split_across_polls(self):
        record = make_record(b"hello", bytes(range(10)))

        self.assertEqual(self.split(record[:-3]), [])
        records = self.split(record[-3:])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["packet_timestamp_bytes"], bytes(range(10)))


if __name__ == "__main__":
    unittest.main()