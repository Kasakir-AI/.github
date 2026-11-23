#!/usr/bin/env python3
"""Deliver GitHub Sponsors rewards.

Reads the maintainer's sponsorships and keeps these in step with them:

* SPONSORS.md in the .github repository (public sponsors, by tier).
* A sponsors section in each project README, between
  <!-- sponsors:start --> and <!-- sponsors:end -->.
* A thank-you section in the first release of each project published after a
  qualifying one-time sponsorship.
* A priority label on open issues opened by sponsors on the priority tier.

Sponsors who chose to sponsor privately are never named in anything this
script writes or prints.

Usage:
  sponsor_rewards.py [--dry-run] [--sponsors-json FILE] [--config FILE]

The token comes from SPONSOR_REWARDS_TOKEN (or GH_TOKEN). Reading sponsors
needs a classic token with the read:user scope; writing needs public_repo.
--sponsors-json replaces the sponsor query with a local file, which lets a
dry run work with any token that can read the public repositories.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.github.com"
README_START = "<!-- sponsors:start -->"
README_END = "<!-- sponsors:end -->"
THANKS_START = "<!-- sponsor-thanks:start -->"
THANKS_END = "<!-- sponsor-thanks:end -->"

SPONSORS_QUERY = """
query($after: String) {
  viewer {
    login
    sponsorshipsAsMaintainer(first: 100, after: $after, includePrivate: true, activeOnly: false) {
      pageInfo { hasNextPage endCursor }
      nodes {
        privacyLevel
        isOneTimePayment
        isActive
        createdAt
        tier { monthlyPriceInDollars isOneTime }
        sponsorEntity {
          __typename
          ... on User { login name url }
          ... on Organization { login name url }
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------- data


@dataclass(frozen=True)
class Sponsorship:
    login: str
    name: str
    url: str
    is_org: bool
    public: bool
    one_time: bool
    active: bool
    price: int
    created: dt.datetime

    @property
    def label(self) -> str:
        return self.name or self.login


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def sponsorship_from_node(node: dict) -> Sponsorship | None:
    entity = node.get("sponsorEntity") or {}
    if not entity.get("login"):
        return None  # deleted or ghost account
    tier = node.get("tier") or {}
    return Sponsorship(
        login=entity["login"],
        name=(entity.get("name") or "").strip(),
        url=entity.get("url") or f"https://github.com/{entity['login']}",
        is_org=entity.get("__typename") == "Organization",
        public=node.get("privacyLevel") == "PUBLIC",
        one_time=bool(node.get("isOneTimePayment") or tier.get("isOneTime")),
        active=bool(node.get("isActive")),
        price=int(tier.get("monthlyPriceInDollars") or 0),
        created=parse_time(node["createdAt"]),
    )


# ---------------------------------------------------------------- rules


def _best_per_login(sponsorships):
    """Keep one sponsorship per login: the highest price, then the newest."""
    best: dict[str, Sponsorship] = {}
    for s in sponsorships:
        current = best.get(s.login)
        if current is None or (s.price, s.created) > (current.price, current.created):
            best[s.login] = s
    return sorted(best.values(), key=lambda s: (-s.price, s.created, s.login.lower()))


def sponsors_list(sponsorships, rules):
    """Public sponsors named in SPONSORS.md: (monthly, one_time)."""
    monthly = [
        s for s in sponsorships
        if s.public and not s.one_time and s.active and s.price >= rules["sponsors_list_monthly_min"]
    ]
    one_time = [
        s for s in sponsorships
        if s.public and s.one_time and s.price >= rules["sponsors_list_one_time_min"]
    ]
    monthly = _best_per_login(monthly)
    monthly_logins = {s.login for s in monthly}
    one_time = [s for s in _best_per_login(one_time) if s.login not in monthly_logins]
    return monthly, one_time


def readme_sponsors(sponsorships, rules, now):
    """Public sponsors shown in project READMEs: (logos, names)."""
    logos = _best_per_login(
        s for s in sponsorships
        if s.public and not s.one_time and s.active and s.price >= rules["readme_logo_monthly_min"]
    )
    logo_logins = {s.login for s in logos}
    window = dt.timedelta(days=rules["readme_one_time_days"])
    names = _best_per_login(
        s for s in sponsorships
        if s.public and s.login not in logo_logins and (
            (not s.one_time and s.active and s.price >= rules["readme_name_monthly_min"])
            or (s.one_time and s.price >= rules["readme_one_time_min"] and now - s.created < window)
        )
    )
    return logos, names


def priority_logins(sponsorships, rules):
    """Logins whose issues get the priority label, public or private."""
    return {
        s.login for s in sponsorships
        if not s.one_time and s.active and s.price >= rules["priority_monthly_min"]
    }


def release_thanks(releases, sponsorships, rules, start):
    """Map release id -> public one-time sponsors to thank in that release.

    A sponsor is thanked in the first published, non-prerelease release that
    comes after their sponsorship, in every project. Releases published before
    `start` are never touched.
    """
    eligible = [
        s for s in sponsorships
        if s.public and s.one_time and s.price >= rules["release_thanks_one_time_min"]
    ]
    published = sorted(
        (r for r in releases if not r.get("draft") and not r.get("prerelease") and r.get("published_at")),
        key=lambda r: parse_time(r["published_at"]),
    )
    result = {}
    previous = None
    for release in published:
        when = parse_time(release["published_at"])
        if when >= start:
            thanked = [
                s for s in eligible
                if (previous is None or s.created >= previous) and s.created < when
            ]
            result[release["id"]] = _best_per_login(thanked)
        previous = when
    return result


# ---------------------------------------------------------------- rendering


def md_text(value: str) -> str:
    """Escape text for use inside a Markdown link label."""
    value = html.escape(value, quote=False)
    return re.sub(r"([\\\[\]*_`|])", r"\\\1", value)


def md_link(s: Sponsorship) -> str:
    return f"[{md_text(s.label)}]({s.url})"


def render_readme_block(logos, names, config) -> str:
    page = config["sponsor_page"]
    full_list = (
        f"https://github.com/{config['maintainer']}/{config['sponsors_file']['repo']}"
        f"/blob/main/{config['sponsors_file']['path']}"
    )
    lines = [README_START, "<!-- This section is updated automatically. Edits here are overwritten. -->", ""]
    if not logos and not names:
        lines.append(f"No sponsors featured here yet. [Become the first]({page})!")
    else:
        if logos:
            lines.append(" ".join(
                f'<a href="{html.escape(s.url)}"><img src="https://github.com/{s.login}.png?size=64" '
                f'width="64" height="64" alt="{html.escape(s.label)}"></a>'
                for s in logos
            ))
            lines.append("")
        if names:
            lines.append(" · ".join(md_link(s) for s in names))
            lines.append("")
        lines.append(f"Thank you! Want to be here too? [Sponsor on GitHub]({page}).")
    lines.append(f"All sponsors: [SPONSORS.md]({full_list}).")
    lines.append(README_END)
    return "\n".join(lines)


def replace_block(text: str, start: str, end: str, block: str) -> str | None:
    """Replace start..end (inclusive) with block. None if the markers are missing."""
    i = text.find(start)
    j = text.find(end, i + len(start)) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    return text[:i] + block + text[j + len(end):]


def render_sponsors_md(monthly, one_time, config) -> str:
    page = config["sponsor_page"]
    out = [
        "# Sponsors",
        "",
        f"Thank you to everyone who supports my projects! 💛 Want to join them? [Sponsor on GitHub]({page}).",
        "",
        "This list is updated automatically. Sponsors who chose to sponsor privately are not listed.",
        "",
        "## Monthly sponsors",
        "",
    ]
    out += [f"- {md_link(s)}" for s in monthly] or ["No monthly sponsors listed yet."]
    out += ["", "## One-time sponsors", ""]
    out += [f"- {md_link(s)}" for s in one_time] or ["No one-time sponsors listed yet."]
    return "\n".join(out) + "\n"


def apply_thanks(body: str, sponsors) -> str:
    body = body or ""
    existing = replace_block(body, THANKS_START, THANKS_END, "")
    base = (existing if existing is not None else body).rstrip()
    if not sponsors:
        return base + ("\n" if base else "")
    names = [md_link(s) for s in sponsors]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    block = "\n".join([
        THANKS_START,
        "### Thank you 💛",
        "",
        f"This release is supported by {joined}. Thank you for sponsoring!",
        THANKS_END,
    ])
    return (base + "\n\n" if base else "") + block + "\n"


# ---------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body=None, ok404=False):
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "sponsor-rewards")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} {path} failed: HTTP {e.code} {detail}") from None

    def graphql(self, query: str, variables: dict):
        result = self.request("POST", "/graphql", {"query": query, "variables": variables})
        if result.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in result["errors"])
            raise RuntimeError(f"GraphQL error: {messages}")
        return result["data"]

    def sponsorships(self, maintainer: str):
        nodes, after = [], None
        while True:
            data = self.graphql(SPONSORS_QUERY, {"after": after})
            viewer = data["viewer"]
            if viewer["login"].lower() != maintainer.lower():
                raise RuntimeError(f"Token belongs to {viewer['login']}, expected {maintainer}")
            page = viewer["sponsorshipsAsMaintainer"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return [s for s in (sponsorship_from_node(n) for n in nodes) if s]

    def get_file(self, owner, repo, path):
        data = self.request("GET", f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}", ok404=True)
        if data is None:
            return None, None
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    def put_file(self, owner, repo, path, text, sha, message, author):
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "author": author,
            "committer": author,
        }
        if sha:
            body["sha"] = sha
        self.request("PUT", f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}", body)

    def releases(self, owner, repo):
        return self.request("GET", f"/repos/{owner}/{repo}/releases?per_page=100") or []

    def update_release_body(self, owner, repo, release_id, body):
        self.request("PATCH", f"/repos/{owner}/{repo}/releases/{release_id}", {"body": body})

    def open_issues(self, owner, repo):
        issues, page = [], 1
        while True:
            batch = self.request("GET", f"/repos/{owner}/{repo}/issues?state=open&per_page=100&page={page}") or []
            issues += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return issues
            page += 1

    def ensure_label(self, owner, repo, label):
        name = urllib.parse.quote(label["name"])
        if self.request("GET", f"/repos/{owner}/{repo}/labels/{name}", ok404=True) is None:
            self.request("POST", f"/repos/{owner}/{repo}/labels", label)

    def add_label(self, owner, repo, number, name):
        self.request("POST", f"/repos/{owner}/{repo}/issues/{number}/labels", {"labels": [name]})


# ---------------------------------------------------------------- run


@dataclass
class Report:
    dry_run: bool
    changes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def change(self, message: str, before: str | None = None, after: str | None = None):
        prefix = "[dry run] would " if self.dry_run else ""
        self.changes.append(prefix + message)
        print(prefix + message)
        if self.dry_run and before is not None and after is not None:
            sys.stdout.writelines(difflib.unified_diff(
                before.splitlines(True), after.splitlines(True), "before", "after", n=1))
            print()


def sync_file(gh, report, owner, repo, path, render, author):
    text, sha = gh.get_file(owner, repo, path)
    new = render(text)
    if new is None:
        report.errors.append(f"{repo}/{path}: sponsors markers not found, skipped")
        print(f"warning: {repo}/{path}: sponsors markers not found, skipped")
        return
    if new == text:
        return
    report.change(f"update {repo}/{path}", text or "", new)
    if not report.dry_run:
        gh.put_file(owner, repo, path, new, sha, "Update sponsors", author)


def run(gh, config, sponsorships, now, dry_run) -> Report:
    report = Report(dry_run)
    owner, rules, author = config["maintainer"], config["rules"], config["commit_author"]
    public = sum(1 for s in sponsorships if s.public)
    print(f"{len(sponsorships)} sponsorships ({public} public, {len(sponsorships) - public} private)")

    def step(name, fn):
        try:
            fn()
        except Exception as e:  # keep going with the other projects
            report.errors.append(f"{name}: {e}")
            print(f"error: {name}: {e}")

    monthly, one_time = sponsors_list(sponsorships, rules)
    sf = config["sponsors_file"]
    step("SPONSORS.md", lambda: sync_file(
        gh, report, owner, sf["repo"], sf["path"],
        lambda _old: render_sponsors_md(monthly, one_time, config), author))

    logos, names = readme_sponsors(sponsorships, rules, now)
    block = render_readme_block(logos, names, config)
    for repo in config["projects"]:
        step(f"{repo} README", lambda repo=repo: sync_file(
            gh, report, owner, repo, config["readme_path"],
            lambda old: replace_block(old or "", README_START, README_END, block), author))

    start = parse_time(config["release_thanks_start"])
    for repo in config["projects"]:
        def thank(repo=repo):
            releases = gh.releases(owner, repo)
            by_id = {r["id"]: r for r in releases}
            for release_id, sponsors in release_thanks(releases, sponsorships, rules, start).items():
                old = by_id[release_id].get("body") or ""
                new = apply_thanks(old, sponsors)
                if new.rstrip() != old.rstrip():
                    report.change(f"update release notes of {repo} {by_id[release_id]['tag_name']}", old, new)
                    if not dry_run:
                        gh.update_release_body(owner, repo, release_id, new)
        step(f"{repo} releases", thank)

    priority = {login.lower() for login in priority_logins(sponsorships, rules)}
    label = config["priority_label"]
    for repo in config["projects"]:
        def prioritize(repo=repo):
            todo = [
                i for i in gh.open_issues(owner, repo)
                if i["user"]["login"].lower() in priority
                and label["name"] not in {lb["name"] for lb in i.get("labels", [])}
            ]
            if not todo:
                return
            if not dry_run:
                gh.ensure_label(owner, repo, label)
            for issue in todo:
                report.change(f"add '{label['name']}' to {repo}#{issue['number']}")
                if not dry_run:
                    gh.add_label(owner, repo, issue["number"], label["name"])
        step(f"{repo} issues", prioritize)

    print(f"{len(report.changes)} change(s), {len(report.errors)} error(s)")
    return report


def load_sponsors_json(path: Path):
    data = json.loads(path.read_text())
    nodes = data["nodes"] if isinstance(data, dict) else data
    return [s for s in (sponsorship_from_node(n) for n in nodes) if s]


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--config", type=Path, default=here / "sponsor-rewards.json")
    parser.add_argument("--sponsors-json", type=Path, help="read sponsorships from this file instead of GitHub")
    args = parser.parse_args(argv)

    token = os.environ.get("SPONSOR_REWARDS_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("error: set SPONSOR_REWARDS_TOKEN", file=sys.stderr)
        return 2
    config = json.loads(args.config.read_text())
    gh = GitHub(token)
    try:
        sponsorships = (
            load_sponsors_json(args.sponsors_json) if args.sponsors_json
            else gh.sponsorships(config["maintainer"])
        )
    except Exception as e:
        print(f"error: reading sponsors: {e}", file=sys.stderr)
        return 1
    now = dt.datetime.now(dt.timezone.utc)
    report = run(gh, config, sponsorships, now, args.dry_run)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
