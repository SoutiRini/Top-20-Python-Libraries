#!/usr/bin/env python3
from __future__ import absolute_import, division, print_function, unicode_literals

from torch.testing._internal.common_distributed import MultiProcessTestCase
from torch.testing._internal.common_utils import TEST_WITH_ASAN, run_tests
from torch.testing._internal.distributed.rpc.dist_optimizer_test import (
    TensorPipeRpcAgentDistOptimizerTest,
)

import unittest

@unittest.skipIf(TEST_WITH_ASAN, "Skip ASAN as torch + multiprocessing spawn have known issues")
class TensorPipeAgentDistOptimizerTestWithSpawn(MultiProcessTestCase, TensorPipeRpcAgentDistOptimizerTest):

    def setUp(self):
        super(TensorPipeAgentDistOptimizerTestWithSpawn, self).setUp()
        self._spawn_processes()

if __name__ == '__main__':
    run_tests()
