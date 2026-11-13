"""Part 4: the eval harness itself runs green on deterministic cases (CI gate without API keys)."""

from evals.run import run


async def test_deterministic_eval_cases_pass(settings):
    report = await run(settings, fake=True)
    failed = [r for r in report["results"] if r["status"] == "fail"]
    assert not failed, failed
    assert report["ran"] >= 8
