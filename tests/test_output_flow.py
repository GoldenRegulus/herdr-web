import heapq
import unittest

from herdr_web.output_flow import (
    FullOutputWindow,
    OUTPUT_CHUNK_BYTES,
    OUTPUT_WINDOW_INITIAL_BYTES,
    OUTPUT_WINDOW_MAX_BYTES,
    OUTPUT_WINDOW_MAX_CHUNKS,
    OUTPUT_WINDOW_MIN_BYTES,
)


class FullOutputWindowTests(unittest.TestCase):
    def fill(self, window: FullOutputWindow, now: float, size=OUTPUT_CHUNK_BYTES) -> int:
        while window.has_room(size):
            window.note_sent(size, now)
        return window.sent_bytes

    def test_no_ack_bounds_bytes_and_small_chunk_accounting(self) -> None:
        window = FullOutputWindow()
        self.fill(window, 0)
        self.assertEqual(window.inflight_bytes, OUTPUT_WINDOW_INITIAL_BYTES)
        with self.assertRaises(ValueError):
            window.note_sent(1, 0)
        for invalid_size in (0, -1, OUTPUT_CHUNK_BYTES + 1):
            self.assertFalse(FullOutputWindow().has_room(invalid_size))

        tiny = FullOutputWindow()
        self.fill(tiny, 0, size=1)
        self.assertEqual(tiny.inflight_chunks, OUTPUT_WINDOW_MAX_CHUNKS)
        self.assertEqual(tiny.inflight_bytes, OUTPUT_WINDOW_MAX_CHUNKS)
        self.assertFalse(tiny.has_room(1))

    def test_invalid_duplicate_regressive_and_future_acks_cannot_grant_credit(self) -> None:
        window = FullOutputWindow()
        self.fill(window, 0)
        for value in (True, False, None, '8192', 1.0, [], {}, -1, 0, window.sent_bytes + 1):
            with self.subTest(value=value):
                self.assertFalse(window.acknowledge(value, 0.01))
                self.assertEqual(window.acknowledged_bytes, 0)
                self.assertEqual(window.window_bytes, OUTPUT_WINDOW_INITIAL_BYTES)
        self.assertTrue(window.acknowledge(OUTPUT_CHUNK_BYTES, 0.01))
        for value in (OUTPUT_CHUNK_BYTES, OUTPUT_CHUNK_BYTES - 1, True):
            self.assertFalse(window.acknowledge(value, 0.02))
        self.assertEqual(window.acknowledged_bytes, OUTPUT_CHUNK_BYTES)

    def test_partial_ack_keeps_oldest_deadline_and_cannot_grow_window(self) -> None:
        window = FullOutputWindow()
        window.note_sent(OUTPUT_CHUNK_BYTES, 0)
        window.note_sent(OUTPUT_CHUNK_BYTES, 1)
        for value in range(1, OUTPUT_CHUNK_BYTES):
            self.assertTrue(window.acknowledge(value, 2))
        self.assertEqual(window.window_bytes, OUTPUT_WINDOW_INITIAL_BYTES)
        self.assertEqual(window.inflight_chunks, 2)
        self.assertEqual(window.seconds_until_timeout(59, 60), 1)
        self.assertTrue(window.acknowledge(OUTPUT_CHUNK_BYTES, 59))
        self.assertEqual(window.seconds_until_timeout(60, 60), 1)
        self.assertEqual(window.seconds_until_timeout(61, 60), 0)
        self.assertTrue(window.acknowledge(window.sent_bytes, 61))
        self.assertIsNone(window.seconds_until_timeout(100, 60))

    def test_growth_depends_on_acked_bytes_not_ack_message_count(self) -> None:
        batched, split = FullOutputWindow(), FullOutputWindow()
        self.fill(batched, 0)
        self.fill(split, 0)
        batched.acknowledge(batched.sent_bytes, 0.1)
        for end in range(OUTPUT_CHUNK_BYTES, split.sent_bytes + 1, OUTPUT_CHUNK_BYTES):
            split.acknowledge(end, 0.1)
        self.assertEqual(batched.window_bytes, OUTPUT_WINDOW_INITIAL_BYTES + OUTPUT_CHUNK_BYTES)
        self.assertEqual(split.window_bytes, batched.window_bytes)

    def test_growth_does_not_require_completely_empty_flight(self) -> None:
        window = FullOutputWindow()
        self.fill(window, 0)
        for end in range(OUTPUT_CHUNK_BYTES, OUTPUT_WINDOW_INITIAL_BYTES + 1, OUTPUT_CHUNK_BYTES):
            window.acknowledge(end, 0.05)
            window.note_sent(OUTPUT_CHUNK_BYTES, 0.05)
            self.assertGreater(window.inflight_bytes, 0)
        self.assertEqual(window.window_bytes, OUTPUT_WINDOW_INITIAL_BYTES + OUTPUT_CHUNK_BYTES)

    def test_clean_high_latency_link_grows_to_hard_cap(self) -> None:
        window = FullOutputWindow()
        for round_number in range(100):
            self.fill(window, round_number)
            self.assertLessEqual(window.inflight_bytes, OUTPUT_WINDOW_MAX_BYTES)
            self.assertLessEqual(window.inflight_chunks, OUTPUT_WINDOW_MAX_CHUNKS)
            window.acknowledge(window.sent_bytes, round_number + 0.4)
        self.assertEqual(window.window_bytes, OUTPUT_WINDOW_MAX_BYTES)

    def test_slow_ack_halves_once_per_flight_without_discarding_bytes(self) -> None:
        window = FullOutputWindow()
        self.fill(window, 0)
        window.acknowledge(window.sent_bytes, 0.05)
        self.fill(window, 1)
        flight_end = window.sent_bytes
        first = window.acknowledged_bytes + OUTPUT_CHUNK_BYTES
        window.acknowledge(first, 1.4)
        self.assertEqual(window.window_bytes, (OUTPUT_WINDOW_INITIAL_BYTES + OUTPUT_CHUNK_BYTES) // 2)
        self.assertGreater(window.inflight_bytes, window.window_bytes)
        self.assertFalse(window.has_room(OUTPUT_CHUNK_BYTES))
        target = window.window_bytes
        for end in range(first + OUTPUT_CHUNK_BYTES, flight_end + 1, OUTPUT_CHUNK_BYTES):
            window.acknowledge(end, 1.5)
            self.assertEqual(window.window_bytes, target)
        self.assertEqual(window.inflight_bytes, 0)
        self.assertTrue(window.has_room(OUTPUT_CHUNK_BYTES))
        self.assertEqual(window.sent_bytes, flight_end)

    def test_repeated_slow_flights_stop_at_minimum(self) -> None:
        window = FullOutputWindow()
        self.fill(window, 0)
        window.acknowledge(window.sent_bytes, 0.01)
        for round_number in range(10):
            now = float(round_number * 1000)
            self.fill(window, now)
            window.acknowledge(window.sent_bytes, now + max(0.3, window.ack_seconds * 3))
        self.assertEqual(window.window_bytes, OUTPUT_WINDOW_MIN_BYTES)


def simulate_transfer(adaptive: bool, rtt: float, total_bytes: int = 4 * 1024 * 1024) -> float:
    """Deterministic latency-only link: unlimited bandwidth, zero parser cost.

    Each ACK arrives one RTT after send. This measures removal of the old
    stop-and-wait ceiling, not a real-network/browser performance promise.
    """
    window = FullOutputWindow()
    if not adaptive:
        window.window_bytes = OUTPUT_CHUNK_BYTES
    now = 0.0
    arrivals = []
    while window.acknowledged_bytes < total_bytes:
        while window.sent_bytes < total_bytes and window.has_room(OUTPUT_CHUNK_BYTES):
            window.note_sent(OUTPUT_CHUNK_BYTES, now)
            heapq.heappush(arrivals, (now + rtt, window.sent_bytes))
        now = arrivals[0][0]
        end = 0
        while arrivals and arrivals[0][0] <= now:
            _arrival, end = heapq.heappop(arrivals)
        window.acknowledge(end, now)
        if not adaptive:
            window.window_bytes = OUTPUT_CHUNK_BYTES
        assert window.inflight_bytes <= OUTPUT_WINDOW_MAX_BYTES
    return now


class FullOutputLatencySimulationTests(unittest.TestCase):
    def test_removes_stop_and_wait_throughput_ceiling(self) -> None:
        for rtt in (0.02, 0.1, 0.25):
            with self.subTest(rtt=rtt):
                old_seconds = simulate_transfer(False, rtt)
                adaptive_seconds = simulate_transfer(True, rtt)
                self.assertAlmostEqual(old_seconds, 512 * rtt)
                self.assertLess(adaptive_seconds, old_seconds / 10)


if __name__ == '__main__':
    unittest.main()
