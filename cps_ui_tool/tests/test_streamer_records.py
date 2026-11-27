import unittest

from src.protocol import (
    STREAMER_EOP,
    STREAMER_SOP,
    format_timestamp_ms,
    split_new_streamer_records,
)


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
        self.assertEqual(
            records[0]["packet_timestamp_delta"], 0xEFCDAB89 - 0x67452301
        )
        self.assertEqual(records[0]["packet_reserved_bytes"], bytes.fromhex("10 32"))
        self.assertEqual(records[0]["rect_v"], 1200)
        self.assertEqual(records[0]["rect_i"], 350)

    def test_handles_timestamped_record_split_across_polls(self):
        record = make_record(b"hello", bytes(range(10)))

        self.assertEqual(self.split(record[:-3]), [])
        records = self.split(record[-3:])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["packet_timestamp_bytes"], bytes(range(10)))

    def test_recovers_after_discarding_partial_record_on_data_loss(self):
        partial_record = make_record(b"incomplete", bytes(range(10)))[:-5]
        self.assertEqual(self.split(partial_record), [])
        self.assertTrue(self.pending)

        self.pending.clear()
        records = self.split(make_record(b"recovered", bytes(range(10))))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"], b"recovered")

    def test_formats_microsecond_ticks_as_milliseconds(self):
        self.assertEqual(format_timestamp_ms(768880), "768.880")
        self.assertEqual(format_timestamp_ms(101519), "101.519")
        self.assertEqual(format_timestamp_ms(101519 - 76880), "24.639")

    def test_decodes_duration_from_screenshot_counter_values(self):
        trailer = (
            (768880).to_bytes(4, "little")
            + (101519).to_bytes(4, "little")
            + bytes.fromhex("00 00")
        )
        record = self.split(make_record(b"hello", trailer))[0]

        self.assertEqual(record["packet_start_ts"], 768880)
        self.assertEqual(record["packet_end_ts"], 101519)
        self.assertEqual(record["packet_timestamp_delta"], -667361)
        self.assertEqual(format_timestamp_ms(record["packet_timestamp_delta"]), "-667.361")


if __name__ == "__main__":
    unittest.main()