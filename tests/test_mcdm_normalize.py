import numpy as np
import pytest

from hazard_dss import mcdm
from hazard_dss.normalize import normalize


def test_normalize_percentile_clips_outliers():
    a = np.concatenate([np.linspace(0, 10, 1000), [1e6]]).reshape(1, -1)
    n = normalize(a, "percentile", p_low=2, p_high=98)
    assert np.nanmin(n) == 0 and np.nanmax(n) == 1
    assert n[0, 500] == pytest.approx(0.5, abs=0.05)  # not squashed by the outlier


def test_normalize_fixed_and_direction():
    a = np.array([[0.0, 22.5, 45.0, 90.0, np.nan]])
    n = normalize(a, "fixed", vmin=0, vmax=45)
    assert np.allclose(n[0, :4], [0, 0.5, 1, 1]) and np.isnan(n[0, 4])
    inv = normalize(a, "fixed", direction="lower", vmin=0, vmax=45)
    assert np.allclose(inv[0, :4], [1, 0.5, 0, 0])


def test_normalize_degenerate_classified_raster():
    a = np.ones((5, 5))
    a[0, 0] = 5
    n = normalize(a, "percentile")
    assert np.nanmax(n) <= 1 and np.nanmin(n) >= 0
    const = normalize(np.full((3, 3), 7.0), "minmax")
    assert np.all(const == 0)


def test_ahp_known_example():
    m = np.array([[1, 3, 5], [1 / 3, 1, 3], [1 / 5, 1 / 3, 1]])
    w, lam, cr = mcdm.ahp_weights(m)
    assert np.allclose(w, [0.637, 0.258, 0.105], atol=0.01)
    assert cr < 0.1 and w.sum() == pytest.approx(1)


def test_ahp_matrix_from_judgements_is_reciprocal():
    m = mcdm.ahp_matrix(["flood", "landslide", "slope"], {"flood>landslide": 2, "landslide>slope": 4})
    assert m[0, 1] == 2 and m[1, 0] == 0.5 and m[1, 2] == 4 and m[0, 2] == 1


def test_inconsistent_ahp_flagged():
    m = mcdm.ahp_matrix(["a", "b", "c"], {"a>b": 9, "b>c": 9, "c>a": 9})
    _, _, cr = mcdm.ahp_weights(m)
    assert cr > 0.1


def test_entropy_weights_sum_to_one_and_favour_variable_criterion():
    rng = np.random.default_rng(1)
    stack = np.vstack([rng.random(500) ** 6, np.full(500, 0.5) + rng.normal(0, 1e-4, 500)])
    w = mcdm.entropy_weights(np.clip(stack, 0, 1))
    assert w.sum() == pytest.approx(1) and w[0] > w[1]


def test_wlc_and_topsis_monotone():
    stack = np.stack([np.linspace(0, 1, 11).reshape(1, -1), np.linspace(0, 1, 11).reshape(1, -1)])
    w = np.array([0.6, 0.4])
    valid = np.ones((1, 11), dtype=bool)
    for idx in (mcdm.wlc(stack, w), mcdm.topsis(stack, w, valid)):
        assert np.all(np.diff(idx[0]) > 0)
        assert idx.min() >= 0 and idx.max() <= 1 + 1e-9


def test_classify_and_breaks():
    idx = np.array([0.0, 0.1, 0.3, 0.5, 0.7, 0.95, np.nan])
    cls = mcdm.classify(idx, [0.2, 0.4, 0.6, 0.8])
    assert list(cls) == [1, 1, 2, 3, 4, 5, 0]
    qb = mcdm.class_breaks(np.linspace(0, 1, 1001), "quantile", [], [0.5, 0.75, 0.9, 0.97])
    assert qb == pytest.approx([0.5, 0.75, 0.9, 0.97], abs=0.01)
