# Kasakir-AI defaults

Shared GitHub settings for Kasakir-AI projects. `FUNDING.yml` adds the Sponsor button to each repository.

If one of these projects helps you, you can [buy me a coffee on Ko-fi](https://ko-fi.com/kasakir), [sponsor on GitHub](https://github.com/sponsors/Kasakir-AI), or star the repository. Everyone who sponsors publicly is thanked in [SPONSORS.md](SPONSORS.md).

## Sponsor rewards

The [Sponsor rewards](.github/workflows/sponsor-rewards.yml) workflow runs every hour and delivers the rewards promised on the sponsor tiers:

| Reward | Who gets it | Where |
| --- | --- | --- |
| Name in the sponsors list | $5+ monthly, $10+ one-time | [SPONSORS.md](SPONSORS.md) |
| Name with a link in project READMEs | $25+ monthly, $100 one-time (for a year) | The Sponsors section of each project README |
| Logo with a link in project READMEs | $100+ monthly | The Sponsors section of each project README |
| Thanks in the release notes | $50+ one-time | The next release of each project |
| Priority label on your issues | $100+ monthly | Open issues in each project |

Sponsors who sponsor privately are never named. Their issues still get the priority label, which doesn't mention sponsorship.

Repository access for early access tiers is handled by GitHub Sponsors itself.

The project list, prices and label live in [`sponsor-rewards.json`](sponsor-rewards.json). To preview changes without writing anything, run the workflow by hand with **dry run** checked, or run it locally:

```bash
SPONSOR_REWARDS_TOKEN=<token> python3 scripts/sponsor_rewards.py --dry-run
```

The `SPONSOR_REWARDS_TOKEN` secret is a classic personal access token with the `public_repo` and `read:user` scopes.
