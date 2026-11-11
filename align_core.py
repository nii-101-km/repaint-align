"""Time translation only, with a fixed comparison interval and separate channels."""

from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import correlate, find_peaks


@dataclass(frozen=True)
class Candidate:
    correction_samples: int
    ncc: float
    loss: float


@dataclass(frozen=True)
class Alignment:
    sample_rate: int
    method: str
    initial_seconds: float
    correction_samples: int
    total_seconds: float
    loss: float
    ncc: float
    compared_frames: int
    candidates: tuple[Candidate, ...]
    warnings: tuple[str, ...]
    curve_samples: tuple[int, ...]
    curve_ncc: tuple[float, ...]

    def to_dict(self):
        return asdict(self)


def _audio(value):
    x = np.asarray(value, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[1] not in (1, 2) or not np.isfinite(x).all():
        raise ValueError("Expected finite mono or stereo audio.")
    return x


def _ncc(a, b):
    a = a - a.mean(axis=0)
    b = b - b.mean(axis=0)
    denominator = np.sqrt(np.sum(a * a) * np.sum(b * b))
    return float(np.sum(a * b) / denominator) if denominator > 1e-25 else -1.0


def _huber(a, b):
    # One scale per multichannel window preserves relative channel amplitudes.
    def normalize(x):
        x = x - np.median(x, axis=0)
        scale = np.median(np.abs(x)) * 1.4826022185
        if scale < 1e-12:
            scale = np.sqrt(np.mean(x * x))
        return x / max(float(scale), 1e-12)

    residual = np.abs(normalize(a) - normalize(b))
    return float(np.mean(np.where(residual <= 1, 0.5 * residual**2, residual - 0.5)))


def align(
    reference,
    target,
    sample_rate,
    *,
    reference_start=0.0,
    target_start=0.0,
    max_ms=100.0,
    method="NCC",
    candidates=5,
):
    """Positive correction moves B later: compare A[n] with B[n-correction].

    The same A[D:M-D] is used for every lag. Only the captured selections
    participate. No signal resampling, mixing, tempo change or zero padding.
    """
    a, b = _audio(reference), _audio(target)
    if a.shape[1] != b.shape[1]:
        raise ValueError("Both selections must have the same channel count.")
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, (int, np.integer))
        or sample_rate <= 0
    ):
        raise ValueError("Invalid sample rate.")
    if not np.isfinite(max_ms) or not 0 <= max_ms <= 10000:
        raise ValueError("The search range must be between 0 and 10000 ms.")
    if (
        method not in ("NCC", "Huber")
        or isinstance(candidates, bool)
        or not isinstance(candidates, (int, np.integer))
        or not 1 <= candidates <= 20
    ):
        raise ValueError("Invalid refinement method or candidate count.")
    d = int(np.floor(max_ms * sample_rate / 1000 + 1e-10))
    m = min(len(a), len(b))
    length = m - 2 * d
    if length < max(32, int(0.01 * sample_rate)):
        raise ValueError(
            "The selections are too short for a fixed comparison interval. "
            "Make the selections longer or reduce the search range."
        )
    a = a[:m]
    # Removing a full-selection DC offset leaves every window NCC unchanged
    # while avoiding cancellation in rolling sum-of-squares calculations.
    b = b[:m] - b[:m].mean(axis=0)
    core = a[d : d + length]
    centered = core - core.mean(axis=0)
    energy_a = np.sum(centered**2)
    if energy_a < 1e-20 or np.sum((b - b.mean(axis=0)) ** 2) < 1e-20:
        raise ValueError("A selection contains silence or a constant signal.")

    dot = np.zeros(2 * d + 1)
    energy_b = np.zeros_like(dot)
    for channel in range(a.shape[1]):
        x = b[:, channel]
        dot += correlate(x, centered[:, channel], mode="valid", method="fft")
        sums = np.r_[0.0, np.cumsum(x)]
        squares = np.r_[0.0, np.cumsum(x * x)]
        rolling_sum = sums[length:] - sums[:-length]
        energy_b += squares[length:] - squares[:-length] - rolling_sum**2 / length
    denominator = np.sqrt(energy_a * np.maximum(energy_b, 0))
    scores = np.divide(dot, denominator, out=np.full_like(dot, -1), where=denominator > 1e-25)[::-1]
    scores = np.clip(scores, -1, 1)
    peaks = list(find_peaks(scores)[0])
    if len(scores) == 1 or scores[0] >= scores[1]:
        peaks.append(0)
    if len(scores) > 1 and scores[-1] >= scores[-2]:
        peaks.append(len(scores) - 1)
    peaks.append(int(np.argmax(scores)))
    separation = max(1, round(0.005 * sample_rate))
    chosen = []
    for index in sorted(set(peaks), key=lambda i: (-scores[i], abs(i - d))):
        if all(abs(index - other) >= separation for other in chosen):
            chosen.append(index)
            if len(chosen) == candidates:
                break

    radius = 2 if method == "NCC" else 16
    refined = []
    for index in chosen:
        best = None
        for lag in range(max(-d, index - d - radius), min(d, index - d + radius) + 1):
            window = b[d - lag : d - lag + length]
            ncc = _ncc(core, window)
            loss = 1 - ncc if method == "NCC" else _huber(core, window)
            candidate = Candidate(lag, ncc, loss)
            if best is None or (loss, abs(lag)) < (best.loss, abs(best.correction_samples)):
                best = candidate
        if best.correction_samples not in [item.correction_samples for item in refined]:
            refined.append(best)
    refined.sort(key=lambda item: (item.loss, abs(item.correction_samples)))
    best = refined[0]
    warnings = []
    if d and abs(best.correction_samples) == d:
        warnings.append("The optimum is at the search boundary. Consider a wider range.")
    if len(refined) > 1 and refined[1].loss - best.loss <= max(0.01, abs(best.loss) * 0.05):
        warnings.append("Several candidates have similar scores. Check the alignment by listening.")
    if best.ncc < 0.3:
        warnings.append("Waveform similarity is low. Choose a more distinctive passage.")
    initial = float(reference_start - target_start)
    # Bound UI payload only. Optimization above always evaluates every integer lag.
    curve_indices = np.unique(
        np.r_[np.linspace(0, len(scores) - 1, min(1001, len(scores)), dtype=int), chosen]
    )
    return Alignment(
        sample_rate,
        method,
        initial,
        best.correction_samples,
        initial + best.correction_samples / sample_rate,
        best.loss,
        best.ncc,
        length,
        tuple(refined),
        tuple(warnings),
        tuple(int(i - d) for i in curve_indices),
        tuple(float(scores[i]) for i in curve_indices),
    )
