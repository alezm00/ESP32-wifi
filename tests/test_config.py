import _setup  # noqa: F401
import json
import os
import unittest

from _util import exists, rmtree, tmpdir

from wifimanager.config import ConfigStore
from wifimanager.log import Logger


def store(path):
    log = Logger(level="DEBUG", console=False)
    s = ConfigStore(path, log)
    s.load()
    return s


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = self.dir + "/wifi.json"

    def tearDown(self):
        rmtree(self.dir)

    def test_missing_file_is_empty(self):
        self.assertEqual(store(self.path).networks(), [])

    def test_defaults_formats_and_plain_json(self):
        s = store(self.path)
        s.merge_defaults([("Home", "pass1234"), {"ssid": "Office", "password": "officepw1", "hidden": True}, "Cafe"])
        self.assertEqual([n["ssid"] for n in s.networks()], ["Home", "Office", "Cafe"])
        with open(self.path) as f:
            raw = f.read()
        data = json.loads(raw)                      # valid JSON ...
        self.assertIn('"password": "pass1234"', raw)  # ... and the password is plain text
        self.assertEqual(data["networks"][1], {"ssid": "Office", "password": "officepw1", "hidden": True})
        self.assertEqual(data["networks"][2]["password"], "")
        self.assertEqual(sorted(data["defaults_seen"]), ["Cafe", "Home", "Office"])
        self.assertEqual(store(self.path).networks(), s.networks())  # survives a reload

    def test_defaults_do_not_override_edits_or_resurrect_deleted(self):
        s = store(self.path)
        s.merge_defaults([("Home", "pass1234"), ("Office", "officepw1")])
        s.upsert("Home", "changed-pw", None)
        s.remove("Office")
        s2 = store(self.path)
        s2.merge_defaults([("Home", "pass1234"), ("Office", "officepw1"), ("New", "newpass12")])
        got = {n["ssid"]: n["password"] for n in s2.networks()}
        self.assertEqual(got, {"Home": "changed-pw", "New": "newpass12"})

    def test_force_defaults_overwrites(self):
        s = store(self.path)
        s.merge_defaults([("Home", "pass1234")])
        s.upsert("Home", "changed-pw")
        s.merge_defaults([("Home", "pass1234")], force=True)
        self.assertEqual(s.get("Home")["password"], "pass1234")

    def test_invalid_defaults_are_skipped(self):
        s = store(self.path)
        s.merge_defaults([("", "x"), ("Short", "123"), ("Good", "goodpass1")])
        self.assertEqual([n["ssid"] for n in s.networks()], ["Good"])

    def test_corrupt_file_is_moved_aside(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        s = store(self.path)
        self.assertEqual(s.networks(), [])
        self.assertTrue(exists(self.path + ".bad"))

    def test_recovers_from_tmp_if_main_missing(self):
        s = store(self.path)
        s.merge_defaults([("Home", "pass1234")])
        os.rename(self.path, self.path + ".tmp")   # power cut between remove and rename
        self.assertEqual(store(self.path).get("Home")["password"], "pass1234")

    def test_upsert_semantics(self):
        s = store(self.path)
        s.upsert("A", "password1", True)
        s.upsert("A", None, None)                   # nothing given -> keeps everything
        self.assertEqual(s.get("A"), {"ssid": "A", "password": "password1", "hidden": True})
        s.upsert("A2", None, False, original="A")    # rename, keep password, clear hidden
        self.assertEqual(s.get("A2"), {"ssid": "A2", "password": "password1", "hidden": False})
        self.assertIsNone(s.get("A"))
        s.upsert("A2", "", None)                     # explicit empty password -> open network
        self.assertEqual(s.get("A2")["password"], "")

    def test_upsert_validation(self):
        s = store(self.path)
        s.upsert("A", "password1")
        s.upsert("B", "password2")
        for args, kw in ((("",), {}), (("x" * 33,), {}), (("C", "short"), {}), (("C", "p" * 65), {}),
                         (("B", None), {"original": "A"}), (("Z", None), {"original": "missing"})):
            with self.assertRaises(ValueError, msg=repr(args)):
                s.upsert(*args, **kw)
        self.assertEqual(len(s.networks()), 2)

    def test_remove(self):
        s = store(self.path)
        s.upsert("A", "password1")
        self.assertTrue(s.remove("A"))
        self.assertFalse(s.remove("A"))
        self.assertEqual(store(self.path).networks(), [])


if __name__ == "__main__":
    unittest.main()
