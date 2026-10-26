"""Matrix and vector operations shared by Cartesian and linear placements."""

import math

IDENTITY_MATRIX = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def multiply(a, b):
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)) for i in range(4)
    )


def normalized(v):
    size = math.sqrt(sum(x * x for x in v))
    if not math.isfinite(size) or size < 1e-12:
        raise ValueError("Invalid direction")
    return tuple(x / size for x in v)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def orthogonal_axes(axis, reference):
    """Build the same normalized right-handed frame for both placement types."""
    z = normalized(axis)
    dot = sum(a * b for a, b in zip(reference, z))
    x = normalized(tuple(a - dot * b for a, b in zip(reference, z)))
    y = normalized(cross(z, x))
    return x, y, z
