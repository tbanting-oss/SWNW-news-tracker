#!/usr/bin/env python3
"""Tests for sent_registry. Standard library only: python tests/test_sent_registry.py"""
import os, sys, json, tempfile, unittest, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import sent_registry as R

TODAY = dt.date(2026, 9, 29)
def item(n, title=None): return {"url": f"https://x.example/{n}", "title": title or f"Story {n}"}


class Keys(unittest.TestCase):
    def test_the_key_ignores_case_query_trailing_slash_and_title_punctuation(self):
        a = {"url": "HTTPS://X.example/a/?utm=1", "title": "Big News: Vendor Ships!"}
        b = {"url": "https://x.example/a", "title": "big news vendor ships"}
        self.assertEqual(R.item_key(a), R.item_key(b))
    def test_two_items_on_one_address_have_different_keys(self):
        self.assertNotEqual(R.item_key({"url": "https://r/x", "title": "Roadmap A"}), R.item_key({"url": "https://r/x", "title": "Roadmap B"}))
    def test_missing_fields_do_not_crash(self):
        self.assertEqual(len(R.item_key({})), 16)


class Selection(unittest.TestCase):
    def test_an_item_is_sent_until_it_reaches_the_cap_then_held_back(self):
        reg = R.load("/no/such/file.json"); i = item(1)
        for expected_sent in (True, True, False):
            send, held = R.select([i], reg, 2)
            self.assertEqual(bool(send), expected_sent)
            if send:
                R.record(reg, send, TODAY)
        self.assertEqual(R.send_count(reg, i), 2)
    def test_a_cap_of_zero_or_less_restores_the_old_behaviour(self):
        reg = R.record(R.load("/no/such/file.json"), [item(1)] * 5, TODAY)
        self.assertEqual(len(R.select([item(1)], reg, 0)[0]), 1); self.assertEqual(len(R.select([item(1)], reg, -1)[0]), 1)
    def test_a_new_item_on_a_reused_address_is_still_sent(self):
        reg = R.record(R.load("/no/such/file.json"), [{"url": "https://r/x", "title": "Roadmap A"}], TODAY)
        send, held = R.select([{"url": "https://r/x", "title": "Roadmap A"}, {"url": "https://r/x", "title": "Roadmap B"}], reg, 1)
        self.assertEqual([i["title"] for i in send], ["Roadmap B"]); self.assertEqual(len(held), 1)
    def test_nothing_is_recorded_unless_record_is_called(self):
        reg = R.load("/no/such/file.json")
        R.select([item(1)], reg, 1)
        self.assertEqual(R.send_count(reg, item(1)), 0)


class Storage(unittest.TestCase):
    def test_a_missing_or_corrupt_file_gives_an_empty_registry(self):
        self.assertEqual(R.load("/no/such/file.json")["items"], {})
        p = os.path.join(tempfile.mkdtemp(), "bad.json"); open(p, "w").write("{not json")
        self.assertEqual(R.load(p)["items"], {})
        q = os.path.join(tempfile.mkdtemp(), "odd.json"); json.dump({"items": []}, open(q, "w"))
        self.assertEqual(R.load(q)["items"], {})
    def test_save_and_load_round_trip(self):
        p = os.path.join(tempfile.mkdtemp(), "sent.json")
        reg = R.record(R.load(p), [item(1), item(2), item(1)], TODAY)
        R.save(reg, p, "2026-09-29T15:00:00Z")
        back = R.load(p)
        self.assertEqual((R.send_count(back, item(1)), R.send_count(back, item(2)), back["updated"]), (2, 1, "2026-09-29T15:00:00Z"))
    def test_old_entries_are_pruned_and_recent_ones_kept(self):
        reg = R.load("/no/such/file.json")
        R.record(reg, [item(1)], TODAY - dt.timedelta(days=11)); R.record(reg, [item(2)], TODAY - dt.timedelta(days=3))
        R.prune(reg, TODAY)
        self.assertEqual((R.send_count(reg, item(1)), R.send_count(reg, item(2))), (0, 1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
