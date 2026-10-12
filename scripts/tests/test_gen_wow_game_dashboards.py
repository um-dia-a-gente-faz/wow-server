"""#377: the generated realm-health dashboard carries the Deploys annotation; the other generated
dashboards do not. Reads the committed output: scripts/generate.py --check proves it is the generator's."""
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DASHBOARD = "monitoring/grafana-dashboard-{}.json"


def annotations(uid):
    doc = json.loads((ROOT / DASHBOARD.format(uid)).read_text())
    return doc.get("annotations", {}).get("list", [])


class DeploysAnnotationTest(unittest.TestCase):
    def test_realm_health_has_exactly_one_deploys_annotation(self):
        anns = annotations("wow-realm-health")
        self.assertEqual([a["name"] for a in anns], ["Deploys"])
        self.assertEqual(anns[0]["expr"], "wow_deploy_timestamp_seconds * 1000")
        self.assertTrue(anns[0]["useValueForTime"])
        self.assertTrue(anns[0]["enable"])

    def test_other_generated_dashboards_have_no_deploys_annotation(self):
        for uid in ("wow-players", "wow-server-host", "wow-live-map"):
            with self.subTest(uid=uid):
                self.assertNotIn("Deploys", [a["name"] for a in annotations(uid)])


if __name__ == "__main__":
    unittest.main()
