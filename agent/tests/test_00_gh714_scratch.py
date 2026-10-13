import threading
import unittest


class Scratch(unittest.TestCase):
    def test_a_fails(self):
        self.fail("gh714-early-failure")

    def test_b_blocks_forever(self):
        threading.Event().wait()
