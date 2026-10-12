import os
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parents[1]


def _stubs():
    """Stand-ins for pymysql and prometheus_client (not installed here); connect always fails."""
    class Family:
        def __init__(self, name, doc, value=None, labels=None):
            self.name, self.value, self.samples = name, value, []

        def add_metric(self, labels, value):
            self.samples.append((tuple(labels), value))

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


class DeployMetricsSurviveMysqlFailure(unittest.TestCase):
    def collect(self, log_path):
        with mock.patch.dict(sys.modules, _stubs()), \
                mock.patch.object(sys, "path", [str(HERE)] + sys.path):
            sys.modules.pop("exporter", None)
            import exporter
            self.addCleanup(sys.modules.pop, "exporter", None)
            exporter.DEPLOY_LOG = log_path
            return {f.name: f for f in exporter.WowCollector().collect()}

    def test_deploy_family_and_readable_flag_served_with_mysql_down(self):
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d, "deploy.log")
            log.write_text("2026-10-12T19:10:49+00:00 deployed 1a2b3c4, stacks: game monitoring\n"
                           "2026-10-12T19:15:00+00:00 DEFERRED game 1a2b3c4, players online: 3\n"
                           "2026-10-12T19:30:00+00:00 deployed 5d6e7f8, stacks: monitoring\n")
            got = self.collect(str(log))
        self.assertEqual(got["wow_exporter_up"].value, 0)
        self.assertEqual(got["wow_deploy_log_readable"].value, 1)
        samples = dict(got["wow_deploy_timestamp_seconds"].samples)
        self.assertEqual(samples, {
            ("1a2b3c4", "game+monitoring"): datetime(2026, 10, 12, 19, 10, 49, tzinfo=UTC).timestamp(),
            ("5d6e7f8", "monitoring"): datetime(2026, 10, 12, 19, 30, 0, tzinfo=UTC).timestamp(),
        })

    def test_missing_deploy_log_is_unreadable_and_has_no_samples(self):
        got = self.collect("/nonexistent/deploy.log")
        self.assertEqual(got["wow_deploy_log_readable"].value, 0)
        self.assertEqual(got["wow_deploy_timestamp_seconds"].samples, [])


if __name__ == "__main__":
    unittest.main()
