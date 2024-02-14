from dataclasses import replace
import enum
import typing

import numpy as np
import ezmsg.core as ez
from ezmsg.util.messages.axisarray import AxisArray
from ezmsg.util.generator import consumer, GenAxisArray


class OptionsEnum(enum.Enum):
    @classmethod
    def options(cls):
        return list(map(lambda c: c.value, cls))


class WindowFunction(OptionsEnum):
    NONE = "None (Rectangular)"
    HAMMING = "Hamming"
    HANNING = "Hanning"
    BARTLETT = "Bartlett"
    BLACKMAN = "Blackman"


WINDOWS = {
    WindowFunction.NONE: np.ones,
    WindowFunction.HAMMING: np.hamming,
    WindowFunction.HANNING: np.hanning,
    WindowFunction.BARTLETT: np.bartlett,
    WindowFunction.BLACKMAN: np.blackman,
}


class SpectralTransform(OptionsEnum):
    RAW_COMPLEX = "Complex FFT Output"
    REAL = "Real Component of FFT"
    IMAG = "Imaginary Component of FFT"
    REL_POWER = "Relative Power"
    REL_DB = "Log Power (Relative dB)"


class SpectralOutput(OptionsEnum):
    FULL = "Full Spectrum"
    POSITIVE = "Positive Frequencies"
    NEGATIVE = "Negative Frequencies"


@consumer
def spectrum(
    axis: Optional[str] = None,
    out_axis: Optional[str] = "freq",
    window: WindowFunction = WindowFunction.HAMMING,
    transform: SpectralTransform = SpectralTransform.REL_DB,
    output: SpectralOutput = SpectralOutput.POSITIVE
) -> Generator[AxisArray, AxisArray, None]:

    # State variables
    axis_arr_in = AxisArray(np.array([]), dims=[""])
    axis_arr_out = AxisArray(np.array([]), dims=[""])

    axis_name = axis
    axis_idx = None
    n_time = None

    while True:
        axis_arr_in = yield axis_arr_out

        if axis_name is None:
            axis_name = message.dims[0]

        # Initial setup
        if n_time is None or axis_idx is None or message.data.shape[axis_idx] != n_time:
            axis_idx = message.get_axis_idx(axis_name)
            _axis = message.get_axis(axis_name)
            n_time = message.data.shape[axis_idx]
            freqs = np.fft.fftshift(np.fft.fftfreq(n_time, d=_axis.gain), axes=-1)
            window = WINDOWS[self.STATE.cur_settings.window](n_time)
            if transform != SpectralTransform.RAW_COMPLEX and not (transform == SpectralTransform.REAL or transform == SpectralTransform.IMAG):
                scale = np.sum(window ** 2.0) * _axis.gain
            axis_offset = freqs[0]
            if output == SpectralOutput.POSITIVE:
                axis_offset = freqs[n_time // 2]
            freq_axis = AxisArray.Axis(
                unit="Hz", gain=1.0 / (_axis.gain * n_time), offset=axis_offset
            )
            if out_axis is None:
                out_axis = axis_name
            new_dims = message.dims[:axis_idx] + [out_axis, ] + message.dims[axis_idx + 1:]
            new_axes = {**message.axes, **{out_axis: freq_axis}}
            if out_axis != axis_name:
                new_axes.pop(axis_name, None)

            f_transform = lambda x: x
            if transform != SpectralTransform.RAW_COMPLEX:
                if transform == SpectralTransform.REAL:
                    f_transform = lambda x: x.real
                elif transform == SpectralTransform.IMAG:
                    f_transform = lambda x: x.imag
                else:
                    if transform == SpectralTransform.REL_DB:
                        f_transform = lambda x: 10 * np.log10((2.0 * (np.abs(x) ** 2.0)) / scale)
                    else:
                        f_transform = lambda x: (2.0 * (np.abs(x) ** 2.0)) / scale

        # TODO: No moveaxis
        # TODO: * window on target axis
        # TODO: fft on target axis
        spectrum = np.moveaxis(message.data, axis_idx, -1)
        spectrum = np.fft.fft(spectrum * window) / n_time
        spectrum = np.fft.fftshift(spectrum, axes=-1)
        spectrum = f_transform(spectrum)

        # TODO: Use slice_along_axis
        if output == SpectralOutput.POSITIVE:
            spectrum = spectrum[..., n_time // 2:]
        elif output == SpectralOutput.NEGATIVE:
            spectrum = spectrum[..., : n_time // 2]

        spectrum = np.moveaxis(spectrum, axis_idx, -1)
        axis_arr_out = replace(message, data=spectrum, dims=new_dims, axes=new_axes)


class SpectrumSettings(ez.Settings):
    axis: Optional[str] = None
    # n: Optional[int] = None # n parameter for fft
    out_axis: Optional[str] = "freq"  # If none; don't change dim name
    window: WindowFunction = WindowFunction.HAMMING
    transform: SpectralTransform = SpectralTransform.REL_DB
    output: SpectralOutput = SpectralOutput.POSITIVE


class SpectrumState(ez.State):
    cur_settings: SpectrumSettings


class Spectrum(ez.Unit):
    SETTINGS: SpectrumSettings
    STATE: SpectrumState

    INPUT_SETTINGS = ez.InputStream(SpectrumSettings)
    INPUT_SIGNAL = ez.InputStream(AxisArray)
    OUTPUT_SIGNAL = ez.OutputStream(AxisArray)

    def initialize(self) -> None:
        self.STATE.cur_settings = self.SETTINGS

    @ez.subscriber(INPUT_SETTINGS)
    async def on_settings(self, msg: SpectrumSettings):
        self.STATE.cur_settings = msg

    @ez.subscriber(INPUT_SIGNAL)
    @ez.publisher(OUTPUT_SIGNAL)
    async def on_data(self, message: AxisArray) -> AsyncGenerator:
        axis_name = self.STATE.cur_settings.axis
        if axis_name is None:
            axis_name = message.dims[0]
        axis_idx = message.get_axis_idx(axis_name)
        axis = message.get_axis(axis_name)

        spectrum = np.moveaxis(message.data, axis_idx, -1)

        n_time = message.data.shape[axis_idx]
        window = WINDOWS[self.STATE.cur_settings.window](n_time)

        spectrum = np.fft.fft(spectrum * window) / n_time
        spectrum = np.fft.fftshift(spectrum, axes=-1)
        freqs = np.fft.fftshift(np.fft.fftfreq(n_time, d=axis.gain), axes=-1)

        if self.STATE.cur_settings.transform != SpectralTransform.RAW_COMPLEX:
            if self.STATE.cur_settings.transform == SpectralTransform.REAL:
                spectrum = spectrum.real
            elif self.STATE.cur_settings.transform == SpectralTransform.IMAG:
                spectrum = spectrum.imag
            else:
                scale = np.sum(window**2.0) * axis.gain
                spectrum = (2.0 * (np.abs(spectrum) ** 2.0)) / scale

                if self.STATE.cur_settings.transform == SpectralTransform.REL_DB:
                    spectrum = 10 * np.log10(spectrum)

        axis_offset = freqs[0]
        if self.STATE.cur_settings.output == SpectralOutput.POSITIVE:
            axis_offset = freqs[n_time // 2]
            spectrum = spectrum[..., n_time // 2 :]
        elif self.STATE.cur_settings.output == SpectralOutput.NEGATIVE:
            spectrum = spectrum[..., : n_time // 2]

        spectrum = np.moveaxis(spectrum, axis_idx, -1)

        out_axis = self.SETTINGS.out_axis
        if out_axis is None:
            out_axis = axis_name

        freq_axis = AxisArray.Axis(
            unit="Hz", gain=1.0 / (axis.gain * n_time), offset=axis_offset
        )
        new_axes = {**message.axes, **{out_axis: freq_axis}}

        new_dims = [d for d in message.dims]
        if self.SETTINGS.out_axis is not None:
            new_dims[axis_idx] = self.SETTINGS.out_axis

        out_msg = replace(message, data=spectrum, dims=new_dims, axes=new_axes)

        yield self.OUTPUT_SIGNAL, out_msg
