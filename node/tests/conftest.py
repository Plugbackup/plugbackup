"""Подключает node/storectl как модуль: у файла нет расширения .py."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "storectl"


def load_storectl() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("storectl", str(SCRIPT))
    spec = importlib.util.spec_from_loader("storectl", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass с отложенными аннотациями ищет модуль в sys.modules.
    sys.modules["storectl"] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sc() -> ModuleType:
    return load_storectl()
