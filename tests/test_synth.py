import asyncio  # noqa: F401
from dataclasses import field
import os
import time
import typing

import numpy as np
import pytest

import ezmsg.core as ez
from ezmsg.util.messages.axisarray import AxisArray
from ezmsg.util.messagelogger import MessageLogger, MessageLoggerSettings
from ezmsg.util.messagecodec import message_log
from ezmsg.util.terminate import TerminateOnTotalSettings, TerminateOnTotal
from util import get_test_fn
from ezmsg.sigproc.synth import (
    clock,
    aclock,
    Clock,
    ClockSettings,
    acounter,
    Counter,
    CounterSettings,
    sin,
    EEGSynth,
    EEGSynthSettings,
)


# TEST CLOCK
@pytest.mark.parametrize("dispatch_rate", [None, 2.0, 20.0])
def test_clock_gen(dispatch_rate: typing.Optional[float]):
    run_time = 1.0
    n_target = int(np.ceil(dispatch_rate * run_time)) if dispatch_rate else 100
    gen = clock(dispatch_rate=dispatch_rate)
    result = []
    t_start = time.time()
    while len(result) < n_target:
        result.append(next(gen))
    t_elapsed = time.time() - t_start
    assert all([_ == ez.Flag() for _ in result])
    if dispatch_rate is not None:
        assert (run_time - 1 / dispatch_rate) < t_elapsed < (run_time + 0.1)
    else:
        # 100 usec per iteration is pretty generous
        assert t_elapsed < (n_target * 1e-4)


@pytest.mark.parametrize("dispatch_rate", [None, 2.0, 20.0])
@pytest.mark.asyncio
async def test_aclock_agen(dispatch_rate: typing.Optional[float]):
    run_time = 1.0
    n_target = int(np.ceil(dispatch_rate * run_time)) if dispatch_rate else 100
    agen = aclock(dispatch_rate=dispatch_rate)
    result = []
    t_start = time.time()
    while len(result) < n_target:
        new_result = await agen.__anext__()
        result.append(new_result)
    t_elapsed = time.time() - t_start
    assert all([_ == ez.Flag() for _ in result])
    if dispatch_rate:
        assert (run_time - 1.1 / dispatch_rate) < t_elapsed < (run_time + 0.1)
    else:
        # 100 usec per iteration is pretty generous
        assert t_elapsed < (n_target * 1e-4)


class ClockTestSystemSettings(ez.Settings):
    clock_settings: ClockSettings
    log_settings: MessageLoggerSettings
    term_settings: TerminateOnTotalSettings = field(
        default_factory=TerminateOnTotalSettings
    )


class ClockTestSystem(ez.Collection):
    SETTINGS = ClockTestSystemSettings

    CLOCK = Clock()
    LOG = MessageLogger()
    TERM = TerminateOnTotal()

    def configure(self) -> None:
        self.CLOCK.apply_settings(self.SETTINGS.clock_settings)
        self.LOG.apply_settings(self.SETTINGS.log_settings)
        self.TERM.apply_settings(self.SETTINGS.term_settings)

    def network(self) -> ez.NetworkDefinition:
        return (
            (self.CLOCK.OUTPUT_CLOCK, self.LOG.INPUT_MESSAGE),
            (self.LOG.OUTPUT_MESSAGE, self.TERM.INPUT_MESSAGE),
        )


@pytest.mark.parametrize("dispatch_rate", [None, 2.0, 20.0])
def test_clock_system(
    dispatch_rate: typing.Optional[float],
    test_name: typing.Optional[str] = None,
):
    run_time = 1.0
    n_target = int(np.ceil(dispatch_rate * run_time)) if dispatch_rate else 100
    test_filename = get_test_fn(test_name)
    ez.logger.info(test_filename)
    settings = ClockTestSystemSettings(
        clock_settings=ClockSettings(dispatch_rate=dispatch_rate),
        log_settings=MessageLoggerSettings(output=test_filename),
        term_settings=TerminateOnTotalSettings(total=n_target),
    )
    system = ClockTestSystem(settings)
    ez.run(SYSTEM=system)

    # Collect result
    messages: typing.List[AxisArray] = [_ for _ in message_log(test_filename)]
    os.remove(test_filename)

    assert all([_ == ez.Flag() for _ in messages])
    assert len(messages) >= n_target


@pytest.mark.parametrize("block_size", [1, 20])
@pytest.mark.parametrize("fs", [10.0, 1000.0])
@pytest.mark.parametrize("n_ch", [3])
@pytest.mark.parametrize(
    "dispatch_rate", [None, "realtime", "ext_clock", 2.0, 20.0]
)  # "ext_clock" needs a separate test
@pytest.mark.parametrize("mod", [2**3, None])
@pytest.mark.asyncio
async def test_acounter(
    block_size: int,
    fs: float,
    n_ch: int,
    dispatch_rate: typing.Optional[typing.Union[float, str]],
    mod: typing.Optional[int],
):
    target_dur = 2.6  # 2.6 seconds per test
    if dispatch_rate is None:
        # No sleep / wait
        chunk_dur = 0.1
    elif isinstance(dispatch_rate, str):
        if dispatch_rate == "realtime":
            chunk_dur = block_size / fs
        elif dispatch_rate == "ext_clock":
            # No sleep / wait
            chunk_dur = 0.1
    else:
        # Note: float dispatch_rate will yield different number of samples than expected by target_dur and fs
        chunk_dur = 1.0 / dispatch_rate
    target_messages = int(target_dur / chunk_dur)

    # Run generator
    agen = acounter(block_size, fs, n_ch=n_ch, dispatch_rate=dispatch_rate, mod=mod)
    messages = [await agen.__anext__() for _ in range(target_messages)]

    # Test contents of individual messages
    for msg in messages:
        assert type(msg) is AxisArray
        assert msg.data.shape == (block_size, n_ch)
        assert "time" in msg.axes
        assert msg.axes["time"].gain == 1 / fs

    agg = AxisArray.concatenate(*messages, dim="time")

    target_samples = block_size * target_messages
    expected_data = np.arange(target_samples)
    if mod is not None:
        expected_data = expected_data % mod
    assert np.array_equal(agg.data[:, 0], expected_data)

    offsets = np.array([m.axes["time"].offset for m in messages])
    expected_offsets = np.arange(target_messages) * block_size / fs
    if dispatch_rate == "realtime" or dispatch_rate == "ext_clock":
        expected_offsets += offsets[0]  # offsets are in real-time
        atol = 0.002
    else:
        # Offsets are synthetic.
        atol = 1.0e-8
    assert np.allclose(offsets[2:], expected_offsets[2:], atol=atol)


class CounterTestSystemSettings(ez.Settings):
    counter_settings: CounterSettings
    log_settings: MessageLoggerSettings
    term_settings: TerminateOnTotalSettings = field(
        default_factory=TerminateOnTotalSettings
    )


class CounterTestSystem(ez.Collection):
    SETTINGS = CounterTestSystemSettings

    COUNTER = Counter()
    LOG = MessageLogger()
    TERM = TerminateOnTotal()

    def configure(self) -> None:
        self.COUNTER.apply_settings(self.SETTINGS.counter_settings)
        self.LOG.apply_settings(self.SETTINGS.log_settings)
        self.TERM.apply_settings(self.SETTINGS.term_settings)

    def network(self) -> ez.NetworkDefinition:
        return (
            (self.COUNTER.OUTPUT_SIGNAL, self.LOG.INPUT_MESSAGE),
            (self.LOG.OUTPUT_MESSAGE, self.TERM.INPUT_MESSAGE),
        )


# Integration Test.
# General functionality of acounter verified above. Here we only need to test a couple configs.
@pytest.mark.parametrize(
    "block_size, fs, dispatch_rate, mod",
    [
        (1, 10.0, None, None),
        (20, 1000.0, "realtime", None),
        (1, 1000.0, 2.0, 2**3),
        (10, 10.0, 20.0, 2**3),
        # No test for ext_clock because that requires a different system
        # (20, 10.0, "ext_clock", None),
    ],
)
def test_counter_system(
    block_size: int,
    fs: float,
    dispatch_rate: typing.Optional[typing.Union[float, str]],
    mod: typing.Optional[int],
    test_name: typing.Optional[str] = None,
):
    n_ch = 3
    target_dur = 2.6  # 2.6 seconds per test
    if dispatch_rate is None:
        # No sleep / wait
        chunk_dur = 0.1
    elif isinstance(dispatch_rate, str):
        if dispatch_rate == "realtime":
            chunk_dur = block_size / fs
    else:
        # Note: float dispatch_rate will yield different number of samples than expected by target_dur and fs
        chunk_dur = 1.0 / dispatch_rate
    target_messages = int(target_dur / chunk_dur)

    test_filename = get_test_fn(test_name)
    ez.logger.info(test_filename)
    settings = CounterTestSystemSettings(
        counter_settings=CounterSettings(
            n_time=block_size,
            fs=fs,
            n_ch=n_ch,
            dispatch_rate=dispatch_rate,
            mod=mod,
        ),
        log_settings=MessageLoggerSettings(
            output=test_filename,
        ),
        term_settings=TerminateOnTotalSettings(
            total=target_messages,
        ),
    )
    system = CounterTestSystem(settings)
    ez.run(SYSTEM=system)

    # Collect result
    messages: typing.List[AxisArray] = [_ for _ in message_log(test_filename)]
    os.remove(test_filename)

    if dispatch_rate is None:
        # The number of messages depends on how fast the computer is
        target_messages = len(messages)
    # This should be an equivalence assertion (==) but the use of TerminateOnTotal does
    #  not guarantee that MessageLogger will exit before an additional message is received.
    #  Let's just clip the last message if we exceed the target messages.
    if len(messages) > target_messages:
        messages = messages[:target_messages]
    assert len(messages) == target_messages

    # Just do one quick data check
    agg = AxisArray.concatenate(*messages, dim="time")
    target_samples = block_size * target_messages
    expected_data = np.arange(target_samples)
    if mod is not None:
        expected_data = expected_data % mod
    assert np.array_equal(agg.data[:, 0], expected_data)


# TEST SIN #
def test_sin_gen(freq: float = 1.0, amp: float = 1.0, phase: float = 0.0):
    axis: typing.Optional[str] = "time"
    srate = max(4.0 * freq, 1000.0)
    sim_dur = 30.0
    n_samples = int(srate * sim_dur)
    n_msgs = min(n_samples, 10)
    axis_idx = 0

    messages = []
    for split_dat in np.array_split(
        np.arange(n_samples)[:, None], n_msgs, axis=axis_idx
    ):
        _time_axis = AxisArray.Axis.TimeAxis(fs=srate, offset=float(split_dat[0, 0]))
        messages.append(
            AxisArray(split_dat, dims=["time", "ch"], axes={"time": _time_axis})
        )

    def f_test(t):
        return amp * np.sin(2 * np.pi * freq * t + phase)

    gen = sin(axis=axis, freq=freq, amp=amp, phase=phase)
    results = []
    for msg in messages:
        res = gen.send(msg)
        assert np.allclose(res.data, f_test(msg.data / srate))
        results.append(res)
    concat_ax_arr = AxisArray.concatenate(*results, dim="time")
    assert np.allclose(
        concat_ax_arr.data, f_test(np.arange(n_samples) / srate)[:, None]
    )


# TODO: test SinGenerator in a system.


class EEGSynthSettingsTest(ez.Settings):
    synth_settings: EEGSynthSettings
    log_settings: MessageLoggerSettings
    term_settings: TerminateOnTotalSettings = field(
        default_factory=TerminateOnTotalSettings
    )


class EEGSynthIntegrationTest(ez.Collection):
    SETTINGS = EEGSynthSettingsTest

    SOURCE = EEGSynth()
    SINK = MessageLogger()
    TERM = TerminateOnTotal()

    def configure(self) -> None:
        self.SOURCE.apply_settings(self.SETTINGS.synth_settings)
        self.SINK.apply_settings(self.SETTINGS.log_settings)
        self.TERM.apply_settings(self.SETTINGS.term_settings)

    def network(self) -> ez.NetworkDefinition:
        return (
            (self.SOURCE.OUTPUT_SIGNAL, self.SINK.INPUT_MESSAGE),
            (self.SINK.OUTPUT_MESSAGE, self.TERM.INPUT_MESSAGE),
        )


def test_eegsynth_system(
    test_name: typing.Optional[str] = None,
):
    # Just a quick test to make sure the system runs. We aren't checking validity of values or anything.
    fs = 500.0
    n_time = 100  # samples per block. dispatch_rate = fs / n_time
    target_dur = 2.0
    target_messages = int(target_dur * fs / n_time)

    test_filename = get_test_fn(test_name)
    ez.logger.info(test_filename)

    settings = EEGSynthSettingsTest(
        synth_settings=EEGSynthSettings(
            fs=fs,
            n_time=n_time,
            alpha_freq=10.5,
            n_ch=8,
        ),
        log_settings=MessageLoggerSettings(
            output=test_filename,
        ),
        term_settings=TerminateOnTotalSettings(
            total=target_messages,
        ),
    )

    system = EEGSynthIntegrationTest(settings)
    ez.run(SYSTEM=system)

    messages: typing.List[AxisArray] = [_ for _ in message_log(test_filename)]
    os.remove(test_filename)
    agg = AxisArray.concatenate(*messages, dim="time")
    assert agg.axes["time"].gain == 1 / fs
    assert agg.data.ndim == 2
