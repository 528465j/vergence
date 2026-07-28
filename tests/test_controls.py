"""Control tests. Phase 3.

One test per control with a deliberately defective fixture, plus:
  * the invariant rows_received == rows_accepted + rows_quarantined
  * a golden-dataset test asserting the full expected exception set
  * a test that resolve_columns works with llm=None
"""

import pytest


@pytest.mark.skip(reason="Phase 3")
def test_placeholder():
    pass
