"""The switching pattern: which loops are on in which phase of the test.

Testing one loop at a time would take days and leave every other room cold.
Instead every loop is on in half of the phases and off in the other half, in
a pattern chosen so that no two loops are ever on or off together more than
chance allows. Each room's temperature then carries a fingerprint of the
loops that heat it, and a regression pulls the fingerprints apart.

Half of the loops are on at any moment, so the house as a whole gets roughly
the heat it usually gets.
"""

from __future__ import annotations

import math

import numpy as np

MIN_PHASES = 8


def phase_count(loops: int) -> int:
    """Phases in one block: a multiple of four, more than the number of loops."""
    return max(MIN_PHASES, 4 * math.ceil((loops + 1) / 4))


def make_design(loops: int, seed: int = 1, tries: int = 3000) -> list[list[int]]:
    """A phases × loops matrix of 0/1, each loop on in exactly half the phases.

    Among random balanced candidates the one with the largest determinant of
    the information matrix is kept (D-optimal), which is what makes the loop
    effects separable with the fewest phases. Every phase keeps between a
    quarter and three quarters of the loops on, so the heat pump always has
    somewhere to deliver heat and the house never goes entirely cold.
    """
    if loops <= 0:
        return []
    phases = phase_count(loops)
    rng = np.random.default_rng(seed)
    base = np.array([1] * (phases // 2) + [0] * (phases - phases // 2))
    best: np.ndarray | None = None
    best_score = -math.inf
    for _ in range(tries):
        matrix = np.column_stack([rng.permutation(base) for _ in range(loops)])
        share = matrix.mean(axis=1)
        # With only a few loops the bound cannot be met without making two
        # loops mirror images of each other, which would hide which is which.
        if loops >= 4 and (share.min() < 0.25 or share.max() > 0.75):
            continue
        info = np.column_stack([np.ones(phases), matrix]).astype(float)
        sign, logdet = np.linalg.slogdet(info.T @ info)
        if sign <= 0:
            continue
        # Prefer patterns where loops change state often: a loop that stays
        # on for three phases in a row says less about how fast it acts.
        changes = np.abs(np.diff(matrix, axis=0)).sum() / max(1, (phases - 1) * loops)
        score = logdet + 2.0 * changes
        if score > best_score:
            best, best_score = matrix, score
    if best is None:  # pragma: no cover - only for pathological sizes
        best = np.column_stack([rng.permutation(base) for _ in range(loops)])
    return best.astype(int).tolist()
