from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals


import operator_benchmark as op_bench
import torch
import torch.nn.quantized as nnq
import torch.quantization as tq


"""Microbenchmarks for general quantization operations."""

# mode is used to show the direction of the benchmark:
# if 'Q', benchmark quantization, else dequantization

quantize_configs_short_dict = {
    'attr_names': ['C', 'M', 'N', 'dtype', 'mode'],
    'attrs': [
        [3, 512, 512, torch.quint8, 'Q'],
        [3, 512, 512, torch.quint8, 'D'],
    ],
    'tags': ['short'],
}

quantize_configs_long_dict = {
    'C': [3, 5, 8],  # this is reused for per-channel: avoid single channel test
    'M': [256, 1024],
    'N': [256, 1024],
    'dtype': [torch.quint8, torch.qint8, torch.qint32],
    'mode': ['D', 'Q'],
    'tags': ['long'],
}


quantize_per_tensor_configs_short = op_bench.config_list(
    **quantize_configs_short_dict
)

quantize_per_tensor_configs_long = op_bench.cross_product_configs(
    **quantize_configs_long_dict
)


class QuantizePerTensorBenchmark(op_bench.TorchBenchmarkBase):
    r"""Benchmarks both quantization and dequantization."""
    def init(self, C, M, N, dtype, mode):
        assert(mode in ('Q', 'D'))
        self.input = torch.rand(C, M, N)
        self.dtype = dtype
        self.op = nnq.Quantize(scale=1.0, zero_point=0, dtype=dtype)
        self.set_module_name('QuantizePerTensor')

        if mode == 'D':
            self.input = self.op(self.input)
            self.op = nnq.DeQuantize()
            self.set_module_name('DequantizePerTensor')

    def forward(self):
        return self.op(self.input)


op_bench.generate_pt_test(
    quantize_per_tensor_configs_short + quantize_per_tensor_configs_long,
    QuantizePerTensorBenchmark)

# === Per Channel quantization ===

quantize_per_channel_configs_short = op_bench.config_list(
    cross_product_configs={
        'axis': (0,)
    },
    **quantize_configs_short_dict
)

quantize_per_channel_configs_long = op_bench.cross_product_configs(
    axis=(0, 1, 2),
    **quantize_configs_long_dict
)

class QuantizePerChannelBenchmark(op_bench.TorchBenchmarkBase):
    r"""Benchmarks both quantization and dequantization."""
    def init(self, C, M, N, dtype, axis, mode):
        assert(mode in ('Q', 'D'))
        self.input = torch.rand(C, M, N)
        self.op = torch.quantize_per_channel

        channel_len = (C, M, N)[axis]

        self.kwargs = {
            'scales': torch.tensor([1.0] * channel_len),
            'zero_points': torch.tensor([0] * channel_len),
            'dtype': dtype,
            'axis': axis
        }

        self.set_module_name('QuantizePerChannel')

        if mode == 'D':
            self.input = self.op(self.input, **self.kwargs)
            # Dequantize doesn't take any arguments
            self.op = lambda x, **kwargs: x.dequantize()
            self.set_module_name('DequantizePerChannel')

    def forward(self):
        return self.op(self.input, **self.kwargs)


op_bench.generate_pt_test(
    quantize_per_channel_configs_short + quantize_per_channel_configs_long,
    QuantizePerChannelBenchmark)

# === Fake Quantization ===

fake_quantize_configs_short = op_bench.config_list(
    # mode is used to show the direction of the benchmark:
    # if 'Q', benchmark quantization, else dequantization
    attr_names=['C', 'M', 'N'],
    attrs=[
        [3, 512, 512],
        [3, 512, 512],
    ],
    tags=['short']
)

fake_quantize_configs_long = op_bench.cross_product_configs(
    C=[1, 3, 8],
    M=[256, 1024],
    N=[256, 1024],
    tags=['long']
)


class FakeQuantizeBenchmark(op_bench.TorchBenchmarkBase):
    r"""Benchmarks fake quantization with default parameters."""
    def init(self, C, M, N):
        self.input = torch.rand(C, M, N)
        self.op = tq.FakeQuantize()
        self.set_module_name('FakeQuantize')

    def forward(self):
        return self.op(self.input)


op_bench.generate_pt_test(
    fake_quantize_configs_short + fake_quantize_configs_long,
    FakeQuantizeBenchmark)


if __name__ == "__main__":
    op_bench.benchmark_runner.main()
