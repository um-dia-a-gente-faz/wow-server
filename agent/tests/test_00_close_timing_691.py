"""THROWAWAY (#691): time Memory.close() under three readers, 300 times, on CI's interpreter. Never merged."""
import statistics
import sys
import threading
import time
import unittest

from agent.memory import Memory


class CloseTiming(unittest.TestCase):
    def test_close_timing(self):
        times = []
        for _ in range(300):
            m = Memory(None)
            for i in range(2000):
                m.remember("action", f"k{i}", i)
            m.flush()
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    m.recall("action", "k5")
                    m.recent("action", 10)

            readers = [threading.Thread(target=reader, name="reader", daemon=True) for _ in range(3)]
            try:
                for r in readers:
                    r.start()
                closer = threading.Thread(target=m.close, name="closer", daemon=True)
                t0 = time.monotonic()
                closer.start()
                closer.join(30)
                times.append(time.monotonic() - t0)
            finally:
                stop.set()
                for r in readers:
                    r.join(30)
        times.sort()
        print(f"\nCLOSE_TIMING python={sys.version.split()[0]} n={len(times)} median={statistics.median(times):.4f}"
              f" p95={times[int(len(times) * .95) - 1]:.4f} max={times[-1]:.4f}"
              f" over1s={sum(t > 1 for t in times)} over5s={sum(t > 5 for t in times)}", flush=True)
