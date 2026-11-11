import numpy as np
import pytest

from align_core import align


@pytest.mark.parametrize("shift", [0, 1, -1, 17, -17, 100, -100, 239, -239, 240, -240])
@pytest.mark.parametrize("channels", [1, 2])
def test_known_sample_shifts(shift, channels):
    rng = np.random.default_rng(41)
    source = rng.normal(size=(7000, channels))
    a = source[600:5600]
    b = source[600 + shift : 5600 + shift]
    result = align(a, b, 48000, max_ms=5, reference_start=12.4, target_start=3.1)
    assert result.correction_samples == shift
    assert result.total_seconds == pytest.approx(9.3 + shift / 48000)
    assert result.compared_frames == 5000 - 480


@pytest.mark.parametrize("variant", ["gain", "noise", "outliers", "dense", "different_lengths"])
@pytest.mark.parametrize("method", ["NCC", "Huber"])
def test_modified_signals(variant, method):
    rng = np.random.default_rng(13)
    x = rng.normal(size=(14000, 2))
    if variant == "dense":
        t = np.arange(len(x)) / 48000
        x += sum(np.sin(2 * np.pi * f * t)[:, None] for f in [113, 419, 901, 1231])
    a, b = x[1000:12000].copy(), x[1017:12017].copy()
    if variant == "gain":
        b = 1.37 * b + np.array([0.12, -0.3])
    if variant == "noise":
        b += rng.normal(scale=0.05, size=b.shape)
    if variant == "outliers":
        b[2000:2005] += 10
    if variant == "different_lengths":
        b = b[:-700]
    assert align(a, b, 48000, max_ms=10, method=method).correction_samples == 17


def test_antiphase_stereo_not_mixed_to_silence():
    rng = np.random.default_rng(2)
    x = rng.normal(size=8000)
    x = np.column_stack([x, -x])
    assert align(x[500:7000], x[483:6983], 48000, max_ms=5).correction_samples == -17


def test_multiple_periodic_candidates():
    t = np.arange(16000)
    x = np.sin(2 * np.pi * t / 400)
    result = align(x[2000:14000], x[2000:14000], 48000, max_ms=20)
    assert len(result.candidates) >= 3
    assert result.warnings
    assert all(item.ncc > 0.99 for item in result.candidates[:3])


def test_huber_can_prefer_a_different_candidate():
    rng = np.random.default_rng(5)
    a = rng.normal(size=(16000, 2))
    b = a.copy()
    a[8000] = 2000
    b[7300] = 2000
    ncc = align(a, b, 48000, max_ms=25)
    huber = align(a, b, 48000, max_ms=25, method="Huber")
    assert ncc.correction_samples == 700
    assert huber.correction_samples == 0


def test_fixed_comparison_does_not_reward_small_overlap():
    rng = np.random.default_rng(9)
    a = rng.normal(size=(7000, 2))
    result = align(a, a, 48000, max_ms=10)
    assert result.correction_samples == 0
    assert result.compared_frames == 7000 - 960
    assert max(result.curve_ncc) == pytest.approx(1)


@pytest.mark.parametrize(
    "a,b,error",
    [
        (np.zeros(5000), np.zeros(5000), "silence"),
        (np.ones(100), np.ones(100), "too short"),
        (np.ones((5000, 2)), np.ones(5000), "channel count"),
        ([np.nan] * 5000, np.ones(5000), "finite"),
    ],
)
def test_invalid_inputs(a, b, error):
    with pytest.raises(ValueError, match=error):
        align(a, b, 48000, max_ms=5)


def test_zero_range():
    a = np.random.default_rng(1).normal(size=1000)
    assert align(a, a, 48000, max_ms=0).correction_samples == 0


@pytest.mark.parametrize("candidates", [True, 2.5, "5"])
def test_candidate_count_must_be_an_integer(candidates):
    a = np.random.default_rng(1).normal(size=4000)
    with pytest.raises(ValueError, match="candidate count"):
        align(a, a, 48000, max_ms=5, candidates=candidates)


def test_large_dc_offset_does_not_destroy_global_ncc():
    x = np.random.default_rng(31).normal(size=(10000, 2))
    a = x[1000:8000]
    b = x[1017:8017] + 1e6
    assert align(a, b, 48000, max_ms=5).correction_samples == 17
