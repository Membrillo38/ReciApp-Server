import os
from pathlib import Path

import pytest


@pytest.fixture
def ios_root() -> Path:
    configured = os.environ.get("RECIAPP_IOS_ROOT")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend(
        [
            Path(__file__).parents[1] / "IosAPP",
            Path(__file__).parents[2] / "ReciApp-iOS",
        ]
    )
    for candidate in candidates:
        if (candidate / "ReciApp").is_dir():
            return candidate
    pytest.skip("iOS source checkout not found; set RECIAPP_IOS_ROOT")
