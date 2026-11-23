import datetime as dt
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sponsor_rewards as sr  # noqa: E402

CONFIG = json.loads((ROOT / "sponsor-rewards.json").read_text())
RULES = CONFIG["rules"]
NOW = dt.datetime(2027, 6, 1, tzinfo=dt.timezone.utc)


def sp(login, price, one_time=False, public=True, active=True, created="2027-01-10T00:00:00Z", org=False, name=""):
    return sr.Sponsorship(
        login=login, name=name, url=f"https://github.com/{login}", is_org=org, public=public,
        one_time=one_time, active=active, price=price, created=sr.parse_time(created),
    )


class FakeGitHub:
    def __init__(self, files=None, releases=None, issues=None):
        self.files = dict(files or {})
        self.releases_by_repo = releases or {}
        self.issues_by_repo = issues or {}
        self.writes = []

    def get_file(self, owner, repo, path):
        text = self.files.get((repo, path))
        return (text, "sha") if text is not None else (None, None)

    def put_file(self, owner, repo, path, text, sha, message, author):
        self.files[(repo, path)] = text
        self.writes.append(("file", repo, path, author["email"]))

    def releases(self, owner, repo):
        return self.releases_by_repo.get(repo, [])

    def update_release_body(self, owner, repo, release_id, body):
        for r in self.releases_by_repo[repo]:
            if r["id"] == release_id:
                r["body"] = body
        self.writes.append(("release", repo, release_id))

    def open_issues(self, owner, repo):
        return self.issues_by_repo.get(repo, [])

    def ensure_label(self, owner, repo, label):
        self.writes.append(("ensure_label", repo))

    def add_label(self, owner, repo, number, name):
        for i in self.issues_by_repo[repo]:
            if i["number"] == number:
                i["labels"].append({"name": name})
        self.writes.append(("label", repo, number))


README = "# App\n\nIntro.\n\n<!-- sponsors:start -->\nold\n<!-- sponsors:end -->\n\nMore.\n"


def readmes():
    return {(p, "README.md"): README for p in CONFIG["projects"]}


class RulesTest(unittest.TestCase):
    def test_sponsors_list_tiers_and_privacy(self):
        s = [
            sp("one", 1), sp("backer", 5), sp("secret", 50, public=False),
            sp("gone", 15, active=False), sp("coffee", 10, one_time=True, active=False),
            sp("tiny", 5, one_time=True),
        ]
        monthly, one_time = sr.sponsors_list(s, RULES)
        self.assertEqual([x.login for x in monthly], ["backer"])
        self.assertEqual([x.login for x in one_time], ["coffee"])

    def test_readme_names_logos_and_one_time_window(self):
        s = [
            sp("insider", 15), sp("featured", 25), sp("champ", 50), sp("company", 100, org=True),
            sp("big", 100, one_time=True, created="2026-12-01T00:00:00Z"),
            sp("expired", 100, one_time=True, created="2026-05-01T00:00:00Z"),
            sp("hidden", 100, public=False),
        ]
        logos, names = sr.readme_sponsors(s, RULES, NOW)
        self.assertEqual([x.login for x in logos], ["company"])
        self.assertEqual({x.login for x in names}, {"featured", "champ", "big"})

    def test_priority_includes_private_monthly_only(self):
        s = [sp("pub", 100), sp("priv", 150, public=False), sp("once", 100, one_time=True), sp("low", 50)]
        self.assertEqual(sr.priority_logins(s, RULES), {"pub", "priv"})

    def test_release_thanks_first_release_after_sponsorship(self):
        start = sr.parse_time("2027-01-01T00:00:00Z")
        releases = [
            {"id": 1, "published_at": "2026-12-01T00:00:00Z", "tag_name": "v1"},
            {"id": 2, "published_at": "2027-02-01T00:00:00Z", "tag_name": "v2"},
            {"id": 3, "published_at": "2027-02-15T00:00:00Z", "tag_name": "v3-beta", "prerelease": True},
            {"id": 4, "published_at": "2027-03-01T00:00:00Z", "tag_name": "v3"},
            {"id": 5, "published_at": None, "draft": True, "tag_name": "v4"},
        ]
        s = [
            sp("early", 50, one_time=True, created="2027-01-10T00:00:00Z"),
            sp("later", 50, one_time=True, created="2027-02-10T00:00:00Z"),
            sp("private", 50, one_time=True, public=False, created="2027-01-10T00:00:00Z"),
            sp("small", 25, one_time=True, created="2027-01-10T00:00:00Z"),
        ]
        result = sr.release_thanks(releases, s, RULES, start)
        self.assertNotIn(1, result)  # before start: never touched
        self.assertEqual([x.login for x in result[2]], ["early"])
        self.assertEqual([x.login for x in result[4]], ["later"])
        self.assertNotIn(3, result)


class RenderTest(unittest.TestCase):
    def test_apply_thanks_is_idempotent_and_removable(self):
        once = sr.apply_thanks("Notes.", [sp("a", 50, one_time=True, name="Ann")])
        self.assertIn("This release is supported by [Ann](https://github.com/a).", once)
        self.assertEqual(sr.apply_thanks(once, [sp("a", 50, one_time=True, name="Ann")]), once)
        self.assertEqual(sr.apply_thanks(once, []), "Notes.\n")

    def test_names_are_escaped(self):
        block = sr.render_readme_block([], [sp("x", 25, name="Evil](http://bad) <b>")], CONFIG)
        self.assertIn(r"[Evil\](http://bad) &lt;b&gt;](https://github.com/x)", block)

    def test_missing_markers(self):
        self.assertIsNone(sr.replace_block("no markers", sr.README_START, sr.README_END, "x"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.sponsors = [
            sp("backer", 5), sp("featured", 25, name="Fea Tured"), sp("acme", 100, org=True),
            sp("shy", 100, public=False), sp("thanks", 50, one_time=True, created="2027-01-10T00:00:00Z"),
        ]
        self.releases = {"Marmot": [
            {"id": 10, "published_at": "2026-11-01T00:00:00Z", "tag_name": "v1", "body": "Old."},
            {"id": 11, "published_at": "2027-02-01T00:00:00Z", "tag_name": "v2", "body": "New."},
        ]}
        self.issues = {"Aster": [
            {"number": 7, "user": {"login": "shy"}, "labels": []},
            {"number": 8, "user": {"login": "random"}, "labels": []},
        ]}

    def fake(self):
        return FakeGitHub(readmes(), json.loads(json.dumps(self.releases)), json.loads(json.dumps(self.issues)))

    def test_full_run_then_second_run_changes_nothing(self):
        gh = self.fake()
        report = sr.run(gh, CONFIG, self.sponsors, NOW, dry_run=False)
        self.assertEqual(report.errors, [])

        sponsors_md = gh.files[(".github", "SPONSORS.md")]
        self.assertIn("[backer]", sponsors_md)
        self.assertIn("[thanks]", sponsors_md)
        self.assertNotIn("shy", sponsors_md)

        readme = gh.files[("Marmot", "README.md")]
        self.assertIn('alt="acme"', readme)
        self.assertIn("[Fea Tured](https://github.com/featured)", readme)
        self.assertNotIn("backer", readme)
        self.assertNotIn("shy", readme)
        self.assertTrue(readme.startswith("# App\n\nIntro.\n\n<!-- sponsors:start -->"))
        self.assertTrue(readme.endswith("<!-- sponsors:end -->\n\nMore.\n"))

        marmot = {r["id"]: r for r in gh.releases_by_repo["Marmot"]}
        self.assertEqual(marmot[10]["body"], "Old.")
        self.assertIn("[thanks](https://github.com/thanks)", marmot[11]["body"])

        self.assertEqual(gh.issues_by_repo["Aster"][0]["labels"], [{"name": "priority"}])
        self.assertEqual(gh.issues_by_repo["Aster"][1]["labels"], [])

        self.assertTrue(all(w[3] == CONFIG["commit_author"]["email"] for w in gh.writes if w[0] == "file"))

        gh.writes.clear()
        again = sr.run(gh, CONFIG, self.sponsors, NOW, dry_run=False)
        self.assertEqual(again.changes, [])
        self.assertEqual(gh.writes, [])

    def test_dry_run_writes_nothing(self):
        gh = self.fake()
        report = sr.run(gh, CONFIG, self.sponsors, NOW, dry_run=True)
        self.assertTrue(report.changes)
        self.assertEqual(gh.writes, [])

    def test_private_sponsor_never_printed(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sr.run(self.fake(), CONFIG, self.sponsors, NOW, dry_run=True)
        self.assertNotIn("shy", out.getvalue())

    def test_missing_markers_reported_not_written(self):
        files = readmes()
        files[("Aster", "README.md")] = "# Aster\n"
        gh = FakeGitHub(files)
        report = sr.run(gh, CONFIG, self.sponsors, NOW, dry_run=False)
        self.assertTrue(any("Aster/README.md" in e for e in report.errors))
        self.assertEqual(gh.files[("Aster", "README.md")], "# Aster\n")


if __name__ == "__main__":
    unittest.main()
