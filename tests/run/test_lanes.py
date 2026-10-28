from __future__ import annotations

import importlib

from papi.core import registry
from papi.core.registry import handles, job_kind, periodic
from papi.run.lanes import partition


def _noop(*args: object) -> None:
    return None


def _register_sample() -> None:
    periodic("telegram:poll", 1.0)(_noop)
    periodic("kap:poll", 30.0)(lambda ctx: None)
    job_kind("kap:fetch")(lambda payload, ctx: None)
    handles("analysis.requested.v1", name="analyst.analyse")(lambda ev, ctx: None)
    job_kind("notify:ops")(lambda payload, ctx: None)
    periodic("notify:digest", 60.0)(lambda ctx: None)

    def checker(payload: object, ctx: object) -> None:
        return None

    checker.__module__ = "papi.verify.checker"
    job_kind("check:claims")(checker)


def test_partition_puts_each_kind_on_exactly_one_lane(clean_registry, settings):
    _register_sample()
    lanes = {lane.name: lane for lane in partition()}
    assert lanes["telegram"].kinds == ["periodic:telegram:poll"]
    assert lanes["telegram"].concurrency == 1
    assert sorted(lanes["io"].kinds) == ["kap:fetch", "periodic:kap:poll"]
    assert sorted(lanes["llm"].kinds) == ["check:claims", "event:analyst.analyse"]
    assert lanes["llm"].concurrency == settings.llm.concurrency
    assert sorted(lanes["general"].kinds) == ["notify:ops", "periodic:notify:digest"]
    assert lanes["general"].concurrency == 2
    every = [k for lane in lanes.values() for k in lane.kinds]
    assert len(every) == len(set(every)) == len(registry.REGISTRY.all_kinds())


def test_lane_config_from_run_section(clean_registry, write_config):
    write_config(
        "[llm]\nconcurrency = 2\n\n"
        "[run]\ngeneral_concurrency = 3\nio_concurrency = 1\n"
        'io_kind_prefixes = ["kap:", "periodic:kap:", "prices:"]\n'
        'llm_kind_prefixes = ["notify:"]\nllm_modules = []\n'
    )
    _register_sample()
    job_kind("prices:sync")(lambda payload, ctx: None)
    lanes = {lane.name: lane for lane in partition()}
    assert "prices:sync" in lanes["io"].kinds
    assert lanes["llm"].kinds == ["notify:ops"]
    assert lanes["llm"].concurrency == 2
    assert "check:claims" in lanes["general"].kinds
    assert lanes["general"].concurrency == 3


def test_empty_lanes_are_omitted(clean_registry, settings):
    job_kind("notify:ops")(lambda payload, ctx: None)
    assert [lane.name for lane in partition()] == ["general"]


def test_real_plugins_telegram_poll_only_on_telegram_lane(clean_registry, settings):
    # Re-register the real slices' handlers into the (empty, restored afterwards) registry.
    import papi.analyst.handlers
    import papi.run.handlers
    import papi.sources.kap.handlers
    import papi.telegram.handlers

    for mod in (papi.telegram.handlers, papi.sources.kap.handlers, papi.analyst.handlers, papi.run.handlers):
        importlib.reload(mod)
    lanes = {lane.name: lane for lane in partition()}
    assert lanes["telegram"].kinds == ["periodic:telegram:poll"]
    for name, lane in lanes.items():
        if name != "telegram":
            assert "periodic:telegram:poll" not in lane.kinds
    assert {"periodic:kap:poll", "kap:fetch"} <= set(lanes["io"].kinds)
    assert "event:analyst.analyse" in lanes["llm"].kinds
    assert {"periodic:run:backup", "event:telegram.process_message"} <= set(lanes["general"].kinds)
