import argparse
import itertools
from . import benchmark
import os
from . import tensor_engine

from . import attention      # noqa: F401
from . import broadcast      # noqa: F401
# from . import conv           # noqa: F401
from . import elementwise    # noqa: F401
from . import matmul         # noqa: F401
# from . import normalization  # noqa: F401
# from . import pooling        # noqa: F401
# from . import reduction      # noqa: F401
# from . import softmax        # noqa: F401
from . import swish          # noqa: F401


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Benchmark operators in specific shapes.
Works only with Python3.\n A few examples:
  * benchmark.py: runs all the default configs with all the benchmarks.
  * benchmark.py reduce: runs all the default configs with all benchmark with a prefix 'reduce'
  * benchmark.py layernorm_fwd_cpu_128_32_128_128: run a particular benchmark in that config""",
    )
    parser.add_argument(
        "benchmark_names",
        type=str,
        default=None,
        nargs="*",
        help="name of the benchmark to run",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cpu,cuda",
        help="a comma separated list of device names",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="fwd,both",
        help="a comma separated list of running modes",
    )
    parser.add_argument(
        "--engine",
        type=str,
        default="pt",
        help="the underlying tensor engine. only pt for now",
    )
    parser.add_argument(
        "--jit_mode",
        type=str,
        default="trace",
        help="the jit mode to use: one of {trace, none}",
    )
    parser.add_argument(
        "--cuda_pointwise_loop_levels",
        type=int,
        default=None,
        help="num of loop levesl for Cuda pointwise operations: 2 or 3",
    )
    parser.add_argument(
        "--cuda_pointwise_block_count",
        type=int,
        default=None,
        help="num of block for Cuda pointwise operations",
    )
    parser.add_argument(
        "--cuda_pointwise_block_size",
        type=int,
        default=None,
        help="num of blocks for Cuda pointwise operations",
    )
    parser.add_argument(
        "--cuda_fuser",
        type=str,
        default="te",
        help="The Cuda fuser backend to use: one of {te, old, none}",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="stdout",
        help="The output format of the benchmark run {stdout[default], json}",
    )

    args = parser.parse_args()

    if args.cuda_fuser == "te":
        import torch

        torch._C._jit_set_texpr_fuser_enabled(True)

    def set_global_threads(num_threads):
        os.environ["OMP_NUM_THREADS"] = str(num_threads)
        os.environ["MKL_NUM_THREADS"] = str(num_threads)
        os.environ["TVM_NUM_THREADS"] = str(num_threads)
        os.environ["NNC_NUM_THREADS"] = str(num_threads)

    devices = args.device.split(",")
    # accept 'gpu' as an alternative as the 'cuda' device
    devices = ["cuda" if device == "gpu" else device for device in devices]
    cpu_count = 0
    for index, device in enumerate(devices):
        if device.startswith("cpu"):
            cpu_count += 1
            if cpu_count > 1:
                raise ValueError(
                    "more than one CPU device is not allowed: %d" % (cpu_count)
                )
            if device == "cpu":
                continue
            num_threads_str = device[3:]
            try:
                # see if the device is in 'cpu1' or 'cpu4' format
                num_threads = int(num_threads_str)
                set_global_threads(num_threads)
                devices[index] = "cpu"
            except ValueError:
                continue

    modes = args.mode.split(",")

    tensor_engine.set_engine_mode(args.engine)

    def run_default_configs(bench_cls, allow_skip=True):
        for mode, device, config in itertools.product(
            modes, devices, bench_cls.default_configs()
        ):
            bench = bench_cls(mode, device, *config)
            bench.output_type = args.output
            bench.jit_mode = args.jit_mode
            if not bench.is_supported():
                if allow_skip:
                    continue
                else:
                    raise ValueError(
                        "attempted to run an unsupported benchmark: %s" % (bench.desc())
                    )
            bench.run(args)

    benchmark_classes = benchmark.benchmark_classes
    if not args.benchmark_names:
        # by default, run all the benchmarks
        for benchmark_cls in benchmark_classes:
            run_default_configs(benchmark_cls, allow_skip=True)
    else:
        for name in args.benchmark_names:
            # if the name is the prefix of a benchmark class, run all the benchmarks for that class
            match_class_name = False
            for bench_cls in benchmark_classes:
                if name in bench_cls.module():
                    match_class_name = True
                    run_default_configs(bench_cls, allow_skip=True)

            if match_class_name:
                continue

            # if not a class module, parse the config and call it that way
            match_class_name = False
            for bench_cls in benchmark_classes:
                cls_module = bench_cls.module()
                if name.startswith(cls_module):
                    match_class_name = True
                    if name[len(cls_module)] != "_":
                        raise ValueError("invalid name: %s" % (name))
                    config_str = name[(len(cls_module) + 1) :]
                    config = config_str.split("_")
                    if len(config) < 2:
                        raise ValueError("invalid config: %s" % config)
                    mode, device = config[0:2]
                    # TODO: make sure virtual devices such as 'cpu1' and 'cpu4' are supported.
                    if mode not in ["fwd", "both"]:
                        raise ValueError("invalid mode: %s" % (mode))
                    for i, entry in enumerate(config):
                        try:
                            value = int(entry)
                            config[i] = value
                        except ValueError:
                            pass
                    bench = bench_cls(*config)
                    bench.jit_mode = args.jit_mode
                    bench.output_type = args.output_type
                    bench.run(args)

            if not match_class_name:
                available_classes = ", ".join(
                    [bench_cls.module() for bench_cls in benchmark_classes]
                )
                raise ValueError(
                    "invalid name: %s\nAvailable benchmark classes:\n%s"
                    % (name, available_classes)
                )


if __name__ == "__main__":
    main()
