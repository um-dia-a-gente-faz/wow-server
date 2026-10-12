import threading
import unittest


class T(unittest.TestCase):
    def test_stall_707(self):
        threading.Event().wait()
