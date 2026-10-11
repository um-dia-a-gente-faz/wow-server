import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parents[1]


def _stubs():
    """Stand-ins for pymysql and prometheus_client (not installed here); connect always fails."""
    class Family:
        def __init__(self, name, doc, value=None, labels=None):
            self.name, self.value = name, value

    pymysql = types.ModuleType("pymysql")
    pymysql.cursors = types.SimpleNamespace(Cursor=object)

    def boom(**_kw):
        raise OSError("mysql down")

    pymysql.connect = boom
    prom = types.ModuleType("prometheus_client")
    prom.REGISTRY = None
    prom.start_http_server = lambda *_: None
    core = types.ModuleType("prometheus_client.core")
    core.GaugeMetricFamily = Family
    reg = types.ModuleType("prometheus_client.registry")
    reg.Collector = object
    return {"pymysql": pymysql, "prometheus_client": prom,
            "prometheus_client.core": core, "prometheus_client.registry": reg}


class BackupMetricsSurviveMysqlFailure(unittest.TestCase):
    def test_backup_metrics_served_when_mysql_collection_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "auth-1.sql.gz")
            p.write_bytes(b"x" * 7)
            os.utime(p, (1234, 1234))
            with mock.patch.dict(sys.modules, _stubs()), \
                    mock.patch.object(sys, "path", [str(HERE)] + sys.path):
                sys.modules.pop("exporter", None)
                import exporter
                self.addCleanup(sys.modules.pop, "exporter", None)
                exporter.BACKUP_DIR = d
                got = {f.name: f.value for f in exporter.WowCollector().collect()}
        self.assertEqual(got["wow_exporter_up"], 0)
        self.assertEqual(got["wow_backup_count"], 1)
        self.assertEqual(got["wow_backup_newest_timestamp_seconds"], 1234.0)
        self.assertEqual(got["wow_backup_newest_size_bytes"], 7.0)


if __name__ == "__main__":
    unittest.main()
