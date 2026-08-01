"""Tests for the declarative layer: resources, webhooks and the controller."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.controller import JobController, schedule_seconds  # noqa: E402
from nestick.resources import (  # noqa: E402
    API_VERSION,
    Condition,
    ConditionType,
    JobSpec,
    JobStore,
    Phase,
    ScrapeJob,
    load_documents,
    parse_selector,
)
from nestick.webhook import AdmissionError, Review, admit, mutate, validate  # noqa: E402

MANIFEST = {
    "apiVersion": API_VERSION,
    "kind": "ScrapeJob",
    "metadata": {"name": "demo", "labels": {"team": "sales"}},
    "spec": {
        "urls": ["https://example.com"],
        "engine": "urls",
        "output": {"formats": ["json"], "path": "out/demo"},
    },
}


def job(**spec) -> ScrapeJob:
    d = json.loads(json.dumps(MANIFEST))
    d["spec"].update(spec)
    return ScrapeJob.from_dict(d)


# --------------------------------------------------------------------------- #
class TestResourceModel:
    def test_roundtrip(self):
        j = ScrapeJob.from_dict(MANIFEST)
        again = ScrapeJob.from_dict(j.to_dict())
        assert again.name == "demo"
        assert again.spec.urls == ["https://example.com"]

    def test_defaults_applied(self):
        j = ScrapeJob.from_dict({"apiVersion": API_VERSION, "kind": "ScrapeJob",
                                 "spec": {"urls": ["https://a.com"]}})
        assert j.name and j.uid and j.generation == 1

    def test_rejects_wrong_kind(self):
        with pytest.raises(ValueError, match="kind"):
            ScrapeJob.from_dict({"apiVersion": API_VERSION, "kind": "Pod"})

    def test_rejects_wrong_api_version(self):
        with pytest.raises(ValueError, match="apiVersion"):
            ScrapeJob.from_dict({"apiVersion": "v0", "kind": "ScrapeJob"})

    def test_unknown_spec_keys_ignored(self):
        j = job(**{"totallyMadeUp": 42})
        assert not hasattr(j.spec, "totallyMadeUp")

    def test_yaml_output_parses_back(self):
        j = ScrapeJob.from_dict(MANIFEST)
        docs = load_documents(j.to_yaml())
        assert docs[0]["metadata"]["name"] == "demo"

    def test_to_settings_projection(self):
        j = job(pages=3, want=["email"])
        s = j.to_settings()
        assert s.urls == ["https://example.com"]
        assert s.pages == 3 and s.want == ("email",)
        assert s.formats == ("json",)

    def test_generation_bump(self):
        j = ScrapeJob.from_dict(MANIFEST)
        j.bump_generation()
        assert j.generation == 2


class TestConditions:
    def test_set_and_read(self):
        j = ScrapeJob.from_dict(MANIFEST)
        j.status.set_condition(ConditionType.READY, True, "Ok", "all good")
        c = j.status.condition(ConditionType.READY)
        assert c.ok and c.reason == "Ok"
        assert j.status.is_ready()

    def test_transition_timestamp_only_on_change(self):
        st = ScrapeJob.from_dict(MANIFEST).status
        st.set_condition(ConditionType.READY, True)
        first = st.condition(ConditionType.READY).last_transition
        time.sleep(0.01)
        st.set_condition(ConditionType.READY, True)          # same status
        assert st.condition(ConditionType.READY).last_transition == first
        st.set_condition(ConditionType.READY, False)         # changed
        assert st.condition(ConditionType.READY).last_transition > first

    def test_serialises(self):
        c = Condition("Ready", "True", "Why", "How")
        assert c.to_dict()["type"] == "Ready"


# --------------------------------------------------------------------------- #
class TestMutatingWebhook:
    def test_name_normalised(self):
        j = ScrapeJob.from_dict({**MANIFEST, "metadata": {"name": "My Job Name"}})
        mutate(j)
        assert j.name == "my-job-name"

    def test_engine_aliases(self):
        j = job(engine="DDG")
        mutate(j)
        assert j.spec.engine == "duckduckgo"

    def test_numeric_clamping(self):
        j = job(pages=9999)
        j.spec.crawl.concurrency = 100_000
        r = mutate(j)
        assert j.spec.pages == 50 and j.spec.crawl.concurrency == 256
        assert r.patches

    def test_urls_normalised_and_deduped(self):
        """Bare hosts gain a scheme, and equivalent forms collapse to one entry."""
        j = job(urls=["example.com", "https://example.com/", "  "])
        mutate(j)
        assert len(j.spec.urls) == 1
        assert j.spec.urls[0].startswith("https://example.com")

    def test_engine_inferred_from_urls(self):
        j = job(engine="auto", urls=["https://a.com"])
        j.spec.queries = []
        mutate(j)
        assert j.spec.engine == "urls"

    def test_empty_formats_defaulted(self):
        j = job()
        j.spec.output.formats = []
        mutate(j)
        assert j.spec.output.formats == ["csv", "json"]


class TestValidatingWebhook:
    def test_accepts_good_spec(self):
        assert validate(job()).allowed

    def test_requires_a_target(self):
        j = job()
        j.spec.urls = []
        j.spec.queries = []
        assert not validate(j).allowed

    @pytest.mark.parametrize("bad", ["nonsense", "google-search", ""])
    def test_bad_engine(self, bad):
        j = job(engine=bad)
        assert not validate(j).allowed

    def test_bad_format(self):
        j = job()
        j.spec.output.formats = ["csv", "pdf"]
        r = validate(j)
        assert not r.allowed and "pdf" in r.errors[0]

    def test_bad_schedule(self):
        j = job(schedule="whenever")
        assert not validate(j).allowed

    @pytest.mark.parametrize("good", ["@daily", "@hourly", "every 30m", "every 6h"])
    def test_good_schedules(self, good):
        assert validate(job(schedule=good)).allowed

    def test_serpapi_needs_query(self):
        j = job(engine="serpapi")
        assert not validate(j).allowed

    def test_warnings_do_not_block(self):
        j = job()
        j.spec.crawl.respectRobots = False
        r = validate(j)
        assert r.allowed and r.warnings

    def test_sets_validated_condition(self):
        j = job()
        validate(j)
        assert j.status.condition(ConditionType.VALIDATED).ok

    def test_admit_runs_both(self):
        j = job(engine="DDG", pages=10_000)
        r = admit(j)
        assert r.allowed and j.spec.engine == "duckduckgo" and j.spec.pages == 50

    def test_review_raises(self):
        r = Review()
        r.deny("nope")
        with pytest.raises(AdmissionError):
            r.raise_for_status()


# --------------------------------------------------------------------------- #
class TestStore:
    def test_create_then_unchanged(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        _, a1 = s.apply(ScrapeJob.from_dict(MANIFEST))
        _, a2 = s.apply(ScrapeJob.from_dict(MANIFEST))
        assert a1 == "created" and a2 == "unchanged"

    def test_update_bumps_generation_and_keeps_uid(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        first, _ = s.apply(ScrapeJob.from_dict(MANIFEST))
        uid = first.uid
        changed = ScrapeJob.from_dict(MANIFEST)
        changed.spec.pages = 5
        updated, action = s.apply(changed)
        assert action == "configured"
        assert updated.uid == uid and updated.generation == 2

    def test_status_survives_update(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        j, _ = s.apply(ScrapeJob.from_dict(MANIFEST))
        j.status.leads = 42
        s.save()
        changed = ScrapeJob.from_dict(MANIFEST)
        changed.spec.pages = 9
        updated, _ = s.apply(changed)
        assert updated.status.leads == 42

    def test_persists_to_disk(self, tmp_path):
        p = tmp_path / "j.json"
        JobStore(p).apply(ScrapeJob.from_dict(MANIFEST))
        assert JobStore(p).get("demo") is not None

    def test_delete_and_list(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        s.apply(ScrapeJob.from_dict(MANIFEST))
        assert len(s) == 1
        assert s.delete("demo") and not s.delete("demo")

    def test_label_selector(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        s.apply(ScrapeJob.from_dict(MANIFEST))
        other = ScrapeJob.from_dict(MANIFEST)
        other.metadata = {"name": "other", "labels": {"team": "research"}}
        s.apply(other)
        assert [j.name for j in s.list({"team": "sales"})] == ["demo"]

    def test_corrupt_file_is_survivable(self, tmp_path):
        p = tmp_path / "j.json"
        p.write_text("{not json")
        assert len(JobStore(p)) == 0


class TestSelectorParsing:
    def test_parses(self):
        assert parse_selector("a=1,b=2") == {"a": "1", "b": "2"}

    def test_empty(self):
        assert parse_selector("") == {} and parse_selector(None) == {}


# --------------------------------------------------------------------------- #
class TestSchedule:
    @pytest.mark.parametrize("text,secs", [
        ("@hourly", 3600), ("@daily", 86_400), ("@weekly", 604_800),
        ("every 30m", 1800), ("every 2h", 7200), ("every 45s", 45),
        ("", None), ("nonsense", None),
    ])
    def test_parsing(self, text, secs):
        assert schedule_seconds(text) == secs


class TestController:
    def test_due_logic(self, tmp_path):
        j = ScrapeJob.from_dict(MANIFEST)
        assert JobController.due(j)                    # never run
        j.status.runCount = 1
        j.status.observedGeneration = j.generation
        assert not JobController.due(j)                # one-shot, done
        j.bump_generation()
        assert JobController.due(j)                    # spec changed

    def test_suspended_never_due(self):
        j = job(suspend=True)
        assert not JobController.due(j)

    def test_schedule_gate(self):
        j = job(schedule="@daily")
        j.status.runCount = 1
        j.status.observedGeneration = j.generation
        j.status.nextRunTime = time.time() + 3600
        assert not JobController.due(j)
        j.status.nextRunTime = time.time() - 1
        assert JobController.due(j)

    def test_denied_job_marked_failed(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        bad = ScrapeJob.from_dict(MANIFEST)
        bad.spec.output.formats = ["pdf"]
        s.apply(bad)
        result = asyncio.run(JobController(s).reconcile_once("demo"))
        assert result.status.phase is Phase.FAILED
        assert not result.status.condition(ConditionType.READY).ok

    def test_successful_run_updates_status(self, tmp_path, monkeypatch):
        from nestick.models import Response

        s = JobStore(tmp_path / "j.json")
        j = ScrapeJob.from_dict(MANIFEST)
        j.spec.output.path = str(tmp_path / "out")
        j.spec.crawl.respectRobots = False
        j.spec.crawl.cache = False
        s.apply(j)

        page = ('<html><body><a href="mailto:hi@example.com">m</a>'
                '<a href="tel:+14155550132">t</a></body></html>')
        import nestick.pipeline as pl

        real_init = pl.Pipeline.__aenter__

        async def patched(self):
            out = await real_init(self)
            async def fake_get(url, **kw):
                return Response(url=url, status=200, text=page)
            self._fetcher.get = fake_get
            return out

        monkeypatch.setattr(pl.Pipeline, "__aenter__", patched)
        result = asyncio.run(JobController(s).reconcile_once("demo", force=True))
        assert result.status.phase is Phase.SUCCEEDED
        assert result.status.leads == 1
        assert result.status.runCount == 1
        assert result.status.observedGeneration == result.generation
        assert result.status.condition(ConditionType.READY).ok
        assert result.status.files

    def test_schedule_sets_next_run(self, tmp_path, monkeypatch):
        from nestick.models import Response
        import nestick.pipeline as pl

        s = JobStore(tmp_path / "j.json")
        j = ScrapeJob.from_dict(MANIFEST)
        j.spec.schedule = "every 1h"
        j.spec.output.path = str(tmp_path / "o")
        j.spec.crawl.cache = False
        s.apply(j)

        real_init = pl.Pipeline.__aenter__

        async def patched(self):
            out = await real_init(self)
            async def fake_get(url, **kw):
                return Response(url=url, status=200, text="<html>hi</html>")
            self._fetcher.get = fake_get
            return out

        monkeypatch.setattr(pl.Pipeline, "__aenter__", patched)
        r = asyncio.run(JobController(s).reconcile_once("demo", force=True))
        assert r.status.nextRunTime and r.status.nextRunTime > time.time()
        assert r.status.condition(ConditionType.SCHEDULED).ok

    def test_events_emitted(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        bad = ScrapeJob.from_dict(MANIFEST)
        bad.spec.engine = "bogus"
        s.apply(bad)
        seen: list[str] = []
        ctrl = JobController(s, on_event=lambda j, e: seen.append(e))
        asyncio.run(ctrl.reconcile_once("demo"))
        assert "denied" in seen

    def test_missing_job(self, tmp_path):
        s = JobStore(tmp_path / "j.json")
        assert asyncio.run(JobController(s).reconcile_once("ghost")) is None


# --------------------------------------------------------------------------- #
class TestCtlCli:
    def test_template_is_valid(self, capsys):
        from nestick.ctl import main

        assert main(["template"]) == 0
        docs = load_documents(capsys.readouterr().out)
        j = ScrapeJob.from_dict(docs[0])
        assert admit(j).allowed

    def test_apply_get_delete(self, tmp_path, capsys):
        from nestick.ctl import main

        f = tmp_path / "j.yaml"
        f.write_text(json.dumps(MANIFEST))
        store = str(tmp_path / "store.json")
        assert main(["--store", store, "apply", "-f", str(f)]) == 0
        assert "created" in capsys.readouterr().out

        assert main(["--store", store, "get"]) == 0
        assert "demo" in capsys.readouterr().out

        assert main(["--store", store, "describe", "demo"]) == 0
        assert "Conditions" in capsys.readouterr().out

        assert main(["--store", store, "delete", "demo"]) == 0

    def test_apply_invalid_exits_nonzero(self, tmp_path):
        from nestick.ctl import main

        bad = json.loads(json.dumps(MANIFEST))
        bad["spec"]["engine"] = "nope"
        f = tmp_path / "bad.yaml"
        f.write_text(json.dumps(bad))
        assert main(["--store", str(tmp_path / "s.json"), "apply", "-f", str(f)]) == 1

    def test_dry_run_stores_nothing(self, tmp_path):
        from nestick.ctl import main

        f = tmp_path / "j.yaml"
        f.write_text(json.dumps(MANIFEST))
        store = tmp_path / "s.json"
        main(["--store", str(store), "apply", "-f", str(f), "--dry-run"])
        assert len(JobStore(store)) == 0

    def test_suspend_resume(self, tmp_path, capsys):
        from nestick.ctl import main

        f = tmp_path / "j.yaml"
        f.write_text(json.dumps(MANIFEST))
        store = str(tmp_path / "s.json")
        main(["--store", store, "apply", "-f", str(f)])
        capsys.readouterr()
        assert main(["--store", store, "suspend", "demo"]) == 0
        assert JobStore(store).get("demo").spec.suspend is True
        assert main(["--store", store, "resume", "demo"]) == 0
        assert JobStore(store).get("demo").spec.suspend is False

    def test_get_missing_is_error(self, tmp_path):
        from nestick.ctl import main

        assert main(["--store", str(tmp_path / "s.json"), "get", "ghost"]) == 1

    def test_yaml_and_json_output(self, tmp_path, capsys):
        from nestick.ctl import main

        f = tmp_path / "j.yaml"
        f.write_text(json.dumps(MANIFEST))
        store = str(tmp_path / "s.json")
        main(["--store", store, "apply", "-f", str(f)])
        capsys.readouterr()
        main(["--store", store, "get", "-o", "json"])
        assert json.loads(capsys.readouterr().out)["items"]
        main(["--store", store, "get", "-o", "yaml"])
        assert "apiVersion" in capsys.readouterr().out

    def test_multi_document_file(self, tmp_path, capsys):
        from nestick.ctl import main

        second = json.loads(json.dumps(MANIFEST))
        second["metadata"]["name"] = "second"
        f = tmp_path / "multi.yaml"
        try:
            import yaml
            f.write_text(yaml.safe_dump_all([MANIFEST, second]))
        except ImportError:
            pytest.skip("PyYAML not installed")
        assert main(["--store", str(tmp_path / "s.json"), "apply", "-f", str(f)]) == 0
        assert capsys.readouterr().out.count("created") == 2

    def test_routed_from_main_cli(self, capsys):
        from nestick.cli import main as cli_main

        assert cli_main(["job", "template"]) == 0
        assert "ScrapeJob" in capsys.readouterr().out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
