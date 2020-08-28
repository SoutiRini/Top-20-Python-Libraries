#!/usr/bin/env python3
import unittest

from torch.testing._internal.common_distributed import MultiProcessTestCase
from torch.testing._internal.distributed.ddp_under_dist_autograd_test import (
    DdpComparisonTest,
    DdpUnderDistAutogradTest,
)
from torch.testing._internal.common_utils import TEST_WITH_ASAN, run_tests

@unittest.skipIf(
    TEST_WITH_ASAN, "Skip ASAN as torch + multiprocessing spawn have known issues"
)
class DdpUnderDistAutogradTest(DdpUnderDistAutogradTest, MultiProcessTestCase):
    def setUp(self):
        super().setUp()
        self._spawn_processes()

@unittest.skipIf(
    TEST_WITH_ASAN, "Skip ASAN as torch + multiprocessing spawn have known issues"
)
class DdpComparisonTest(DdpComparisonTest, MultiProcessTestCase):
    def setUp(self):
        super().setUp()
        self._spawn_processes()

if __name__ == "__main__":
    run_tests()
